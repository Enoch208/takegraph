"""Real-B2 missed event reconciliation test (PRD §22.4)."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from takegraph_api.b2_reconciliation import B2UploadReconciler
from takegraph_api.db.models import Organization, Project, UploadIntent
from takegraph_domain.storage.keys import temporary_upload_key
from takegraph_infrastructure.b2 import B2Settings, B2Store


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
    except Exception as exc:  # noqa: BLE001 — explicit integration skip
        pytest.skip(f"B2 integration is not configured: {type(exc).__name__}")
    store = B2Store(settings, preflight=True)
    yield store
    store.close()


async def test_deliberately_missed_upload_event_is_recovered(session, b2_store: B2Store) -> None:
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    upload_id = uuid.uuid4()
    key = temporary_upload_key(upload_id=upload_id, filename="missed-event.txt")
    now = datetime.now(UTC)
    session.add_all(
        [
            Organization(
                id=organization_id,
                slug=f"reconciliation-{uuid.uuid4().hex}",
                name="Reconciliation Test",
            ),
            Project(
                id=project_id,
                organization_id=organization_id,
                slug="missed-event",
                name="Missed Event",
                status="ACTIVE",
                is_demo=False,
            ),
        ]
    )
    await session.flush()
    session.add(
        UploadIntent(
            id=upload_id,
            organization_id=organization_id,
            project_id=project_id,
            source_stable_key="source.reference",
            original_file_name="missed-event.txt",
            expected_size_bytes=18,
            declared_mime_type="text/plain",
            client_sha256=None,
            object_key=key,
            status="INITIATED",
            expires_at=now + timedelta(minutes=5),
            created_by=uuid.uuid4(),
        )
    )
    await session.commit()
    await asyncio.to_thread(
        b2_store.store_bytes,
        key,
        b"missed event bytes",
        content_type="text/plain",
    )

    try:
        first = await B2UploadReconciler(session, b2_store).run_once(
            grace_seconds=0,
            now=datetime.now(UTC),
        )
        await session.commit()
        second = await B2UploadReconciler(session, b2_store).run_once(
            grace_seconds=0,
            now=datetime.now(UTC),
        )
        await session.commit()

        assert first.ran is True
        assert first.scanned == 1
        assert first.discovered == 1
        assert first.queued == 1
        assert second.discovered == 0
        assert (
            await session.scalar(
                text("select trigger_source from b2_object_events where object_key=:object_key"),
                {"object_key": key},
            )
            == "RECONCILIATION"
        )
        assert await session.scalar(text("select count(*) from work_items")) == 1
    finally:
        await asyncio.to_thread(b2_store.delete, key)
        await session.execute(text("delete from upload_intents where id=:id"), {"id": upload_id})
        await session.execute(text("delete from projects where id=:id"), {"id": project_id})
        await session.execute(
            text("delete from organizations where id=:id"), {"id": organization_id}
        )
        await session.commit()
