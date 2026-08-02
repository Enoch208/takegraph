"""Lease-owning TAKEGRAPH worker runtime (PRD §13.1)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from genblaze_core.exceptions import StorageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from takegraph_api.db.models import BuildNode
from takegraph_api.queue import ClaimedItem, LeaseLostError, WorkQueue, validate_lease_config
from takegraph_domain.errors import AssetVerificationError, DomainError
from takegraph_infrastructure.b2 import B2Store

from takegraph_worker.build_work import BuildWorkHandlers
from takegraph_worker.end_card_work import EndCardWorkHandlers
from takegraph_worker.narration_work import NarrationWorkHandlers
from takegraph_worker.source_work import SourceWorkHandlers


@dataclass(frozen=True, slots=True)
class WorkRunReceipt:
    claimed: int
    completed: int
    failed: int
    lease_lost: int


class WorkerRuntime:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: B2Store,
        *,
        owner: str,
        lease_seconds: int,
        heartbeat_seconds: int,
        concurrency: int,
        build_handlers: BuildWorkHandlers | None = None,
        narration_handlers: NarrationWorkHandlers | None = None,
        end_card_handlers: EndCardWorkHandlers | None = None,
    ) -> None:
        validate_lease_config(lease_seconds, heartbeat_seconds)
        if not owner or len(owner) > 128:
            raise ValueError("worker owner must contain 1 to 128 characters")
        if concurrency < 1 or concurrency > 64:
            raise ValueError("worker concurrency must be between 1 and 64")
        self._session_factory = session_factory
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._concurrency = concurrency
        self._handlers = SourceWorkHandlers(session_factory, store)
        self._build_handlers = build_handlers or BuildWorkHandlers(session_factory, store)
        self._narration_handlers = narration_handlers or NarrationWorkHandlers(
            session_factory, store
        )
        self._end_card_handlers = end_card_handlers or EndCardWorkHandlers(session_factory, store)

    async def run_once(self) -> WorkRunReceipt:
        async with self._session_factory() as session:
            items = await WorkQueue(session).claim(
                owner=self._owner,
                lease_seconds=self._lease_seconds,
                limit=self._concurrency,
            )
            await session.commit()
        if not items:
            return WorkRunReceipt(claimed=0, completed=0, failed=0, lease_lost=0)

        outcomes = await asyncio.gather(*(self._process(item) for item in items))
        return WorkRunReceipt(
            claimed=len(items),
            completed=outcomes.count("completed"),
            failed=outcomes.count("failed"),
            lease_lost=outcomes.count("lease_lost"),
        )

    async def _process(self, item: ClaimedItem) -> str:
        stop_heartbeat = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(item.id, stop=stop_heartbeat, lost=lease_lost)
        )
        failure: Exception | None = None
        try:
            await self._dispatch(item)
        except Exception as exc:  # noqa: BLE001 — classified and durably recorded below
            failure = exc
        finally:
            stop_heartbeat.set()
            await heartbeat

        if lease_lost.is_set():
            return "lease_lost"
        if failure is not None:
            await self._record_failure(item.id, failure)
            return "failed"

        async with self._session_factory() as session:
            completed = await WorkQueue(session).complete(item.id, owner=self._owner)
            await session.commit()
        if not completed:
            return "lease_lost"
        return "completed"

    async def _dispatch(self, item: ClaimedItem) -> None:
        if item.kind == "validate_source_upload":
            await self._handlers.validate_source_upload(item.target_id)
            return
        if item.kind == "process_b2_event":
            await self._handlers.process_b2_event(item.target_id)
            return
        if item.kind == "EXECUTE_BUILD_NODE":
            await self._execute_build_node(item.target_id)
            return
        raise ValueError(f"unsupported work item kind: {item.kind}")

    async def _execute_build_node(self, build_node_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            stable_key = await session.scalar(
                select(BuildNode.stable_key).where(BuildNode.id == build_node_id)
            )
        if stable_key == "copy.pack":
            await self._build_handlers.execute_build_node(build_node_id)
            return
        if stable_key == "audio.narration":
            await self._narration_handlers.execute_build_node(build_node_id)
            return
        if stable_key == "graphic.end_card":
            await self._end_card_handlers.execute_build_node(build_node_id)
            return
        if stable_key is None:
            raise ValueError("build node was not found")
        raise ValueError(f"unsupported build node: {stable_key}")

    async def _heartbeat(
        self,
        item_id: uuid.UUID,
        *,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_seconds)
                return
            except TimeoutError:
                async with self._session_factory() as session:
                    owned = await WorkQueue(session).heartbeat(
                        item_id,
                        owner=self._owner,
                        lease_seconds=self._lease_seconds,
                    )
                    await session.commit()
                if not owned:
                    lost.set()
                    return

    async def _record_failure(self, item_id: uuid.UUID, exc: Exception) -> None:
        delay = _retry_delay_seconds(exc)
        safe_error = f"{type(exc).__name__}: {_safe_message(exc)}"
        async with self._session_factory() as session:
            try:
                await WorkQueue(session).fail(
                    item_id,
                    owner=self._owner,
                    error=safe_error,
                    retry_in_seconds=delay,
                )
            except LeaseLostError:
                await session.rollback()
                return
            await session.commit()


def _retry_delay_seconds(exc: Exception) -> int | None:
    if isinstance(exc, (AssetVerificationError, StorageError, TimeoutError, OSError)):
        return 10
    return None


def _safe_message(exc: Exception) -> str:
    if isinstance(exc, DomainError):
        return exc.message[:500]
    if isinstance(exc, ValueError):
        return str(exc)[:500]
    return "work handler failed"
