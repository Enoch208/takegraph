"""Real B2 + PostgreSQL source upload finalization tests (PRD Phase 2 gate)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
import urllib.request
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from takegraph_api.auth import HmacSessionProvider, SessionClaims
from takegraph_api.db.models import Organization, Project
from takegraph_api.main import app
from takegraph_api.uploads import (
    SourceUploadService,
    UploadInitiationRequest,
)
from takegraph_domain.auth import Principal
from takegraph_domain.enums import Role
from takegraph_domain.errors import ForbiddenError, InvalidSourceError, UploadIncompleteError
from takegraph_domain.storage.keys import content_address, temporary_upload_key
from takegraph_infrastructure.b2 import B2Settings, B2Store

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _load_env() -> dict[str, str]:
    env = dict(os.environ)
    if os.path.exists(".env"):
        with open(".env") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env.setdefault(key.strip(), value.strip())
    return env


@pytest.fixture(scope="module")
def b2_store() -> B2Store:
    try:
        settings = B2Settings.from_env(_load_env())
    except Exception as exc:  # noqa: BLE001 — converted to an explicit integration skip
        pytest.skip(f"B2 integration is not configured: {type(exc).__name__}")
    store = B2Store(settings, preflight=True)
    yield store
    store.close()


@pytest_asyncio.fixture
async def project_context(session):
    organization = Organization(
        id=uuid.uuid4(), slug=f"upload-{uuid.uuid4().hex}", name="Upload Test"
    )
    project = Project(
        id=uuid.uuid4(),
        organization_id=organization.id,
        slug="orbit-upload-test",
        name="ORBIT Upload Test",
        status="ACTIVE",
        is_demo=False,
    )
    session.add_all([organization, project])
    await session.commit()
    organization_id = organization.id
    project_id = project.id
    principal = Principal(
        actor_id=uuid.uuid4(),
        subject="upload-test-owner",
        organization_id=organization.id,
        role=Role.OWNER,
    )
    yield organization, project, principal
    await session.rollback()
    await session.execute(
        text("delete from upload_intents where project_id=:id"), {"id": project_id}
    )
    await session.execute(
        text(
            "delete from source_versions where source_id in "
            "(select id from sources where project_id=:project_id)"
        ),
        {"project_id": project_id},
    )
    await session.execute(text("delete from sources where project_id=:id"), {"id": project_id})
    await session.execute(
        text("delete from project_revisions where project_id=:id"), {"id": project_id}
    )
    await session.execute(
        text("delete from assets where organization_id=:id"), {"id": organization_id}
    )
    await session.execute(text("delete from projects where id=:id"), {"id": project_id})
    await session.execute(text("delete from organizations where id=:id"), {"id": organization_id})
    await session.commit()


def _service(session, store: B2Store, tmp_path) -> SourceUploadService:
    return SourceUploadService(
        session,
        store,
        max_upload_bytes=5_000_000,
        max_video_duration_seconds=30,
        temp_root=tmp_path,
        signed_url_ttl_seconds=300,
    )


def _put(url: str, data: bytes, content_type: str) -> None:
    request = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={"Content-Type": content_type},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status in (200, 204)


class TestSourceUploadRoundTrip:
    async def test_presigned_put_finalizes_real_bytes_and_is_idempotent(
        self, session, b2_store: B2Store, project_context, tmp_path
    ) -> None:
        organization, project, principal = project_context
        service = _service(session, b2_store, tmp_path)
        digest = hashlib.sha256(PNG_1X1).hexdigest()
        initiated = await service.initiate(
            project_id=project.id,
            principal=principal,
            request=UploadInitiationRequest(
                source_stable_key="source.product_reference",
                file_name="../orbit-reference.png",
                size_bytes=len(PNG_1X1),
                mime_type="image/png",
                sha256=digest,
            ),
        )
        await session.commit()
        await asyncio.to_thread(_put, initiated.upload_url, PNG_1X1, "image/png")

        completed = await service.complete(
            project_id=project.id,
            upload_id=initiated.upload_id,
            principal=principal,
        )
        await session.commit()
        repeated = await service.complete(
            project_id=project.id,
            upload_id=initiated.upload_id,
            principal=principal,
        )
        await session.commit()

        assert completed == repeated
        assert completed.sha256 == digest
        assert completed.mime_type == "image/png"
        assert completed.media_kind == "IMAGE"
        assert await session.scalar(text("select count(*) from assets")) == 1
        assert await session.scalar(text("select count(*) from source_versions")) == 1
        assert await session.scalar(text("select count(*) from project_revisions")) == 1
        assert (
            await session.scalar(
                text("select count(*) from source_versions where revision_id is not null")
            )
            == 1
        )
        assert (
            await session.scalar(
                text(
                    "select count(*) from b2_object_events "
                    "where trigger_source='APPLICATION_COMMIT' and object_key=:key"
                ),
                {
                    "key": content_address(
                        organization_id=organization.id,
                        sha256=digest,
                        extension="png",
                    )
                },
            )
            == 1
        )
        assert (
            await session.scalar(
                text("select count(*) from work_items where kind='validate_source_upload'")
            )
            == 1
        )
        assert b2_store.verify(
            content_address(
                organization_id=organization.id,
                sha256=digest,
                extension="png",
            ),
            expected_sha256=digest,
        )

        b2_store.delete(
            temporary_upload_key(
                upload_id=initiated.upload_id,
                filename="orbit-reference.png",
            )
        )
        b2_store.delete(
            content_address(
                organization_id=organization.id,
                sha256=digest,
                extension="png",
            )
        )

    async def test_size_mismatch_stays_incomplete(
        self, session, b2_store: B2Store, project_context, tmp_path
    ) -> None:
        _organization, project, principal = project_context
        service = _service(session, b2_store, tmp_path)
        initiated = await service.initiate(
            project_id=project.id,
            principal=principal,
            request=UploadInitiationRequest(
                source_stable_key="source.product_reference",
                file_name="orbit-reference.png",
                size_bytes=len(PNG_1X1) + 1,
                mime_type="image/png",
            ),
        )
        await session.commit()
        await asyncio.to_thread(_put, initiated.upload_url, PNG_1X1, "image/png")

        with pytest.raises(UploadIncompleteError, match="size"):
            await service.complete(
                project_id=project.id,
                upload_id=initiated.upload_id,
                principal=principal,
            )
        await session.rollback()
        assert await session.scalar(text("select count(*) from assets")) == 0
        b2_store.delete(
            temporary_upload_key(
                upload_id=initiated.upload_id,
                filename="orbit-reference.png",
            )
        )

    async def test_magic_bytes_override_claimed_mime(
        self, session, b2_store: B2Store, project_context, tmp_path
    ) -> None:
        _organization, project, principal = project_context
        service = _service(session, b2_store, tmp_path)
        data = b"plain text pretending to be a png"
        initiated = await service.initiate(
            project_id=project.id,
            principal=principal,
            request=UploadInitiationRequest(
                source_stable_key="source.product_reference",
                file_name="fake.png",
                size_bytes=len(data),
                mime_type="image/png",
            ),
        )
        await session.commit()
        await asyncio.to_thread(_put, initiated.upload_url, data, "image/png")

        with pytest.raises(InvalidSourceError, match="not a supported media format"):
            await service.complete(
                project_id=project.id,
                upload_id=initiated.upload_id,
                principal=principal,
            )
        await session.rollback()
        assert await session.scalar(text("select count(*) from assets")) == 0
        b2_store.delete(temporary_upload_key(upload_id=initiated.upload_id, filename="fake.png"))


async def test_cross_tenant_principal_cannot_initiate(
    session, b2_store: B2Store, project_context, tmp_path
) -> None:
    _organization, project, _principal = project_context
    outsider = Principal(uuid.uuid4(), "outsider", uuid.uuid4(), Role.OWNER)
    with pytest.raises(ForbiddenError):
        await _service(session, b2_store, tmp_path).initiate(
            project_id=project.id,
            principal=outsider,
            request=UploadInitiationRequest(
                source_stable_key="source.product_reference",
                file_name="orbit-reference.png",
                size_bytes=len(PNG_1X1),
                mime_type="image/png",
            ),
        )


async def test_upload_route_requires_bearer_session(monkeypatch) -> None:
    monkeypatch.setenv("SESSION_SECRET", "upload-route-session-secret-long-enough")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/projects/{uuid.uuid4()}/uploads",
            json={
                "source_stable_key": "source.product_reference",
                "file_name": "orbit.png",
                "size_bytes": len(PNG_1X1),
                "mime_type": "image/png",
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_viewer_cannot_reach_upload_storage(monkeypatch) -> None:
    secret = "upload-route-session-secret-long-enough"  # noqa: S105 — synthetic test key
    monkeypatch.setenv("SESSION_SECRET", secret)
    now = int(time.time())
    claims = SessionClaims(
        subject="viewer",
        actor_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role=Role.VIEWER,
        issued_at=now - 1,
        expires_at=now + 60,
        nonce="viewer-test",
    )
    token = HmacSessionProvider(secret, clock=lambda: now).issue(claims)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/projects/{uuid.uuid4()}/uploads",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "source_stable_key": "source.product_reference",
                "file_name": "orbit.png",
                "size_bytes": len(PNG_1X1),
                "mime_type": "image/png",
            },
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
