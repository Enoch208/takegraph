"""Worker dispatch integration tests against the real PostgreSQL queue."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from takegraph_api.db.models import (
    Asset,
    B2ObjectEvent,
    Organization,
    Project,
    Source,
    SourceVersion,
    UploadIntent,
)
from takegraph_api.queue import WorkQueue
from takegraph_infrastructure.b2 import ObjectHead
from takegraph_worker.runtime import WorkerRuntime


class StubStore:
    bucket = "takegraph-work-test"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[int, str]] = {}

    def head(self, key: str) -> ObjectHead | None:
        value = self.objects.get(key)
        if value is None:
            return None
        size, content_type = value
        return ObjectHead(key=key, size_bytes=size, content_type=content_type, metadata={})

    def verify(self, key: str, *, expected_sha256: str) -> bool:
        return key in self.objects and expected_sha256 == "a" * 64


def _runtime(session_factory, store: StubStore) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory,
        store,  # type: ignore[arg-type] — narrow in-memory storage test double
        owner="worker-test-1",
        lease_seconds=30,
        heartbeat_seconds=5,
        concurrency=2,
    )


async def _project(session) -> tuple[uuid.UUID, uuid.UUID]:
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    session.add_all(
        [
            Organization(
                id=organization_id,
                slug=f"worker-{uuid.uuid4().hex}",
                name="Worker Test",
            ),
            Project(
                id=project_id,
                organization_id=organization_id,
                slug="source-worker",
                name="Source Worker",
                status="ACTIVE",
                is_demo=False,
            ),
        ]
    )
    await session.commit()
    return organization_id, project_id


async def _cleanup_project(session, organization_id: uuid.UUID, project_id: uuid.UUID) -> None:
    await session.execute(
        text(
            "delete from source_versions where source_id in "
            "(select id from sources where project_id=:project_id)"
        ),
        {"project_id": project_id},
    )
    await session.execute(
        text("delete from sources where project_id=:project_id"), {"project_id": project_id}
    )
    await session.execute(
        text("delete from upload_intents where project_id=:project_id"),
        {"project_id": project_id},
    )
    await session.execute(
        text("delete from assets where organization_id=:organization_id"),
        {"organization_id": organization_id},
    )
    await session.execute(
        text("delete from projects where id=:project_id"), {"project_id": project_id}
    )
    await session.execute(
        text("delete from organizations where id=:organization_id"),
        {"organization_id": organization_id},
    )
    await session.commit()


async def test_worker_reverifies_finalized_source_asset(session, session_factory) -> None:
    organization_id, project_id = await _project(session)
    source_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    key = "tenants/test/assets/source.png"
    store = StubStore()
    store.objects[key] = (128, "image/png")
    session.add_all(
        [
            Source(
                id=source_id,
                project_id=project_id,
                stable_key="source.reference",
                kind="IMAGE",
            ),
            Asset(
                id=asset_id,
                organization_id=organization_id,
                sha256="a" * 64,
                size_bytes=128,
                mime_type="image/png",
                media_kind="IMAGE",
                b2_bucket=store.bucket,
                b2_key=key,
                verified_at=None,
            ),
        ]
    )
    await session.flush()
    session.add(
        SourceVersion(
            id=source_version_id,
            source_id=source_id,
            revision_id=None,
            asset_id=asset_id,
            content_hash="a" * 64,
            created_by=uuid.uuid4(),
        )
    )
    await session.flush()
    await WorkQueue(session).enqueue(
        kind="validate_source_upload",
        target_id=source_version_id,
        dedupe_key=f"validate:{source_version_id}",
    )
    await session.commit()

    try:
        receipt = await _runtime(session_factory, store).run_once()
        assert receipt.completed == 1
        assert await session.scalar(text("select status from work_items")) == "DONE"
        assert (
            await session.scalar(
                text("select verified_at is not null from assets where id=:id"),
                {"id": asset_id},
            )
            is True
        )
    finally:
        await _cleanup_project(session, organization_id, project_id)


async def test_worker_confirms_upload_event_and_advances_intent(session, session_factory) -> None:
    organization_id, project_id = await _project(session)
    upload_id = uuid.uuid4()
    event_id = uuid.uuid4()
    key = f"temporary/uploads/{upload_id}/source.png"
    store = StubStore()
    store.objects[key] = (64, "image/png")
    session.add_all(
        [
            UploadIntent(
                id=upload_id,
                organization_id=organization_id,
                project_id=project_id,
                source_stable_key="source.reference",
                original_file_name="source.png",
                expected_size_bytes=64,
                declared_mime_type="image/png",
                object_key=key,
                status="INITIATED",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                created_by=uuid.uuid4(),
            ),
            B2ObjectEvent(
                id=event_id,
                dedupe_key=f"test:{event_id}",
                message_id=None,
                event_type="b2:ObjectCreated:Upload",
                bucket=store.bucket,
                object_key=key,
                object_size=64,
                status="RECEIVED",
                trigger_source="B2_EVENT",
            ),
        ]
    )
    await session.flush()
    await WorkQueue(session).enqueue(
        kind="process_b2_event",
        target_id=event_id,
        dedupe_key=f"process:{event_id}",
    )
    await session.commit()

    try:
        receipt = await _runtime(session_factory, store).run_once()
        assert receipt.completed == 1
        assert (
            await session.scalar(
                text("select status from b2_object_events where id=:id"), {"id": event_id}
            )
            == "CONFIRMED"
        )
        assert (
            await session.scalar(
                text("select status from upload_intents where id=:id"), {"id": upload_id}
            )
            == "UPLOADED"
        )
    finally:
        await _cleanup_project(session, organization_id, project_id)


async def test_unknown_work_kind_fails_loudly(session, session_factory) -> None:
    work_id = await WorkQueue(session).enqueue(
        kind="not-a-real-handler",
        target_id=uuid.uuid4(),
        dedupe_key=f"unknown:{uuid.uuid4()}",
        max_attempts=1,
    )
    await session.commit()
    assert work_id is not None

    receipt = await _runtime(session_factory, StubStore()).run_once()

    assert receipt.failed == 1
    assert (
        await session.scalar(text("select status from work_items where id=:id"), {"id": work_id})
        == "DEAD"
    )
    assert "unsupported work item kind" in (
        await session.scalar(
            text("select last_error from work_items where id=:id"), {"id": work_id}
        )
    )
