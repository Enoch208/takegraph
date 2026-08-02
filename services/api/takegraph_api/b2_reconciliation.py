"""Periodic recovery of missed Backblaze object-created events (PRD §15.4)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_domain.enums import TriggerSource
from takegraph_infrastructure.b2 import B2Store

from takegraph_api.b2_webhooks import object_work_dedupe
from takegraph_api.db.models import B2ObjectEvent, UploadIntent
from takegraph_api.queue import WorkQueue

# One transaction-scoped leader for this reconciliation shard (§20.7). A
# database lock makes concurrent scheduler replicas harmless without Redis.
B2_RECONCILIATION_LOCK_ID = 1_446_121_073


@dataclass(frozen=True, slots=True)
class ReconciliationReceipt:
    ran: bool
    scanned: int
    discovered: int
    queued: int


class B2UploadReconciler:
    """HEAD expected presigned-upload keys and recover missing notifications.

    Upload intents are the durable list of objects TAKEGRAPH expects to exist.
    Discovery records its real trigger source and only queues a reference to the
    durable event. It does not pretend a webhook fired and does not finalize
    unverified media inside the scheduler transaction.
    """

    def __init__(self, session: AsyncSession, store: B2Store) -> None:
        self._session = session
        self._store = store

    async def run_once(
        self,
        *,
        limit: int = 200,
        grace_seconds: int = 30,
        now: datetime | None = None,
    ) -> ReconciliationReceipt:
        if limit < 1 or limit > 1000:
            raise ValueError("reconciliation limit must be between 1 and 1000")
        if grace_seconds < 0:
            raise ValueError("reconciliation grace must not be negative")

        acquired = await self._session.scalar(
            text("select pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": B2_RECONCILIATION_LOCK_ID},
        )
        if acquired is not True:
            return ReconciliationReceipt(ran=False, scanned=0, discovered=0, queued=0)

        clock = now or datetime.now(UTC)
        intents = (
            await self._session.scalars(
                select(UploadIntent)
                .where(
                    UploadIntent.status == "INITIATED",
                    UploadIntent.created_at <= clock - timedelta(seconds=grace_seconds),
                )
                .order_by(UploadIntent.created_at, UploadIntent.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()

        discovered = queued = 0
        queue = WorkQueue(self._session)
        for intent in intents:
            head = await asyncio.to_thread(self._store.head, intent.object_key)
            if head is None:
                continue
            if await self._already_confirmed(intent.object_key):
                continue

            event_id = uuid.uuid4()
            inserted = await self._session.execute(
                insert(B2ObjectEvent)
                .values(
                    id=event_id,
                    dedupe_key=f"reconciliation:upload:{intent.id}",
                    message_id=None,
                    event_type="takegraph:ObjectDiscovered",
                    bucket=self._store.bucket,
                    object_key=intent.object_key,
                    object_version_id=None,
                    object_size=head.size_bytes,
                    event_timestamp=clock,
                    status="RECEIVED",
                    trigger_source=str(TriggerSource.RECONCILIATION),
                )
                .on_conflict_do_nothing(index_elements=[B2ObjectEvent.dedupe_key])
                .returning(B2ObjectEvent.id)
            )
            if inserted.scalar_one_or_none() is None:
                continue
            discovered += 1
            work_id = await queue.enqueue(
                kind="process_b2_event",
                target_id=event_id,
                dedupe_key=object_work_dedupe(
                    bucket=self._store.bucket,
                    object_key=intent.object_key,
                    event_type="takegraph:ObjectDiscovered",
                ),
            )
            if work_id is not None:
                queued += 1

        return ReconciliationReceipt(
            ran=True,
            scanned=len(intents),
            discovered=discovered,
            queued=queued,
        )

    async def _already_confirmed(self, object_key: str) -> bool:
        event_id = await self._session.scalar(
            select(B2ObjectEvent.id)
            .where(
                B2ObjectEvent.bucket == self._store.bucket,
                B2ObjectEvent.object_key == object_key,
                B2ObjectEvent.status != "IGNORED",
                or_(
                    B2ObjectEvent.event_type.startswith("b2:ObjectCreated:"),
                    B2ObjectEvent.event_type == "takegraph:ObjectDiscovered",
                ),
            )
            .limit(1)
        )
        return event_id is not None
