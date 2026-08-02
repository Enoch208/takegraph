"""Durable work queue (PRD §10.5, §13.1).

Jobs live in PostgreSQL, not Redis, so a Redis outage cannot lose queued work
(§6.1). Everything here is designed around one assumption: **the worker will die
at the worst possible moment.** Leases expire, claims are atomic, and nothing
depends on a process running its cleanup code.

Two rules that shape the whole module:

- §8.3.10 — an item is claimable only when `available_at <= now()` and its lease
  is absent or expired. Claiming is a single atomic UPDATE, never a read followed
  by a write, so two workers cannot both win.
- §10.5 — "Expired lease does not automatically imply a new external submission."
  Reclaiming a lease returns the *item* to the queue; whether the underlying
  attempt may be re-submitted is decided separately by attempt reconciliation
  against the provider. That separation is what keeps a crashed worker from
  double-billing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_domain.enums import WorkItemStatus

# §10.5: "Lease duration must exceed heartbeat interval by at least 3x."
MIN_LEASE_TO_HEARTBEAT_RATIO = 3


class LeaseConfigurationError(ValueError):
    """Raised at startup rather than letting a too-short lease cause duplicate
    execution under load."""


def validate_lease_config(lease_seconds: int, heartbeat_seconds: int) -> None:
    if heartbeat_seconds <= 0:
        raise LeaseConfigurationError("heartbeat interval must be positive")
    if lease_seconds < heartbeat_seconds * MIN_LEASE_TO_HEARTBEAT_RATIO:
        raise LeaseConfigurationError(
            f"lease of {lease_seconds}s must be at least "
            f"{MIN_LEASE_TO_HEARTBEAT_RATIO}x the {heartbeat_seconds}s heartbeat; "
            "a shorter lease lets a healthy worker lose its item mid-execution"
        )


@dataclass(frozen=True, slots=True)
class ClaimedItem:
    id: uuid.UUID
    kind: str
    target_id: uuid.UUID
    build_id: uuid.UUID | None
    attempt_count: int
    max_attempts: int
    dedupe_key: str
    payload: dict[str, Any] | None


class WorkQueue:
    """Repository for `work_items`. Holds no state of its own, so it is safe to
    construct per request or per work-loop iteration."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        kind: str,
        target_id: uuid.UUID,
        dedupe_key: str,
        build_id: uuid.UUID | None = None,
        priority: int = 0,
        delay_seconds: int = 0,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 5,
    ) -> uuid.UUID | None:
        """Insert a job, or return None if `dedupe_key` already exists.

        §5.9 FR-EVT-003 requires re-delivery to produce no duplicate downstream
        job. The unique index on `dedupe_key` is the enforcement; this method
        relies on the database to arbitrate rather than checking first and racing.
        """
        row = await self._session.execute(
            text(
                """
                insert into work_items
                    (id, kind, target_id, build_id, status, priority, available_at,
                     attempt_count, max_attempts, dedupe_key, payload_json, created_at)
                values
                    (:id, :kind, :target_id, :build_id, 'QUEUED', :priority,
                     now() + make_interval(secs => :delay), 0, :max_attempts,
                     :dedupe_key, cast(:payload as jsonb), now())
                on conflict (dedupe_key) do nothing
                returning id
                """
            ),
            {
                "id": uuid.uuid4(),
                "kind": kind,
                "target_id": target_id,
                "build_id": build_id,
                "priority": priority,
                "delay": delay_seconds,
                "max_attempts": max_attempts,
                "dedupe_key": dedupe_key,
                "payload": _json_or_null(payload),
            },
        )
        return row.scalar_one_or_none()

    async def claim(
        self,
        *,
        owner: str,
        lease_seconds: int,
        limit: int = 1,
        kinds: list[str] | None = None,
    ) -> list[ClaimedItem]:
        """Atomically lease up to `limit` eligible items.

        `FOR UPDATE SKIP LOCKED` is what makes concurrent workers safe: a row
        already locked by another transaction is skipped rather than waited on, so
        N workers claim N distinct items with no coordination and no blocking.

        The inner SELECT deliberately re-checks eligibility inside the same
        statement as the UPDATE. Splitting them would open a window where two
        workers both see the same row as free.
        """
        result = await self._session.execute(
            text(
                """
                update work_items w
                   set status           = 'LEASED',
                       lease_owner      = :owner,
                       lease_expires_at = now() + make_interval(secs => :lease),
                       attempt_count    = w.attempt_count + 1
                 where w.id in (
                       select id from work_items
                        -- This status list is written to match the partial
                        -- index predicate verbatim. Expressing it as
                        -- "QUEUED/RETRY_WAIT or (LEASED and expired)" is
                        -- logically identical but Postgres cannot prove that
                        -- implies the index, and plans a seq scan instead.
                        where status in ('QUEUED', 'RETRY_WAIT', 'LEASED')
                          and available_at <= now()
                          -- §8.3.10: claimable when the lease is absent or
                          -- expired. A crashed worker leaves its item LEASED, so
                          -- excluding that status would strand the item until
                          -- the reconciler happened to run.
                          and (status <> 'LEASED' or lease_expires_at <= now())
                          and (:all_kinds or kind = any(:kinds))
                        order by priority desc, available_at
                        for update skip locked
                        limit :limit
                 )
             returning w.id, w.kind, w.target_id, w.build_id,
                       w.attempt_count, w.max_attempts, w.dedupe_key, w.payload_json
                """
            ),
            {
                "owner": owner,
                "lease": lease_seconds,
                "limit": limit,
                "all_kinds": kinds is None,
                "kinds": kinds or [],
            },
        )
        return [
            ClaimedItem(
                id=r.id,
                kind=r.kind,
                target_id=r.target_id,
                build_id=r.build_id,
                attempt_count=r.attempt_count,
                max_attempts=r.max_attempts,
                dedupe_key=r.dedupe_key,
                payload=r.payload_json,
            )
            for r in result
        ]

    async def heartbeat(self, item_id: uuid.UUID, *, owner: str, lease_seconds: int) -> bool:
        """Extend a lease. Returns False if this worker no longer owns the item.

        A False result is meaningful, not incidental: it means the lease expired
        and someone else may now hold the item. The caller must stop work rather
        than continue and risk two workers finishing the same job.
        """
        result = await self._session.execute(
            text(
                """
                update work_items
                   set lease_expires_at = now() + make_interval(secs => :lease)
                 where id = :id
                   and lease_owner = :owner
                   and status = 'LEASED'
                   and lease_expires_at > now()
             returning id
                """
            ),
            {"id": item_id, "owner": owner, "lease": lease_seconds},
        )
        return result.scalar_one_or_none() is not None

    async def complete(self, item_id: uuid.UUID, *, owner: str) -> bool:
        result = await self._session.execute(
            text(
                """
                update work_items
                   set status = 'DONE', lease_owner = null, lease_expires_at = null
                 where id = :id and lease_owner = :owner and status = 'LEASED'
             returning id
                """
            ),
            {"id": item_id, "owner": owner},
        )
        return result.scalar_one_or_none() is not None

    async def fail(
        self,
        item_id: uuid.UUID,
        *,
        owner: str,
        error: str,
        retry_in_seconds: int | None = None,
    ) -> WorkItemStatus:
        """Record a failure and decide retry versus dead-letter.

        Exhausting `max_attempts` moves the item to DEAD rather than retrying
        forever. §10.5 requires a permanent work failure to emit a domain event
        and transition its target through domain rules — the caller does that;
        this method owns only the queue row.
        """
        retryable = retry_in_seconds is not None
        result = await self._session.execute(
            text(
                """
                update work_items
                   set status = case
                                  when :retryable and attempt_count < max_attempts
                                  then 'RETRY_WAIT'
                                  else 'DEAD'
                                end,
                       available_at = case
                                        when :retryable and attempt_count < max_attempts
                                        then now() + make_interval(secs => :delay)
                                        else available_at
                                      end,
                       lease_owner = null,
                       lease_expires_at = null,
                       last_error = :error
                 where id = :id and lease_owner = :owner
             returning status
                """
            ),
            {
                "id": item_id,
                "owner": owner,
                "retryable": retryable,
                "delay": retry_in_seconds or 0,
                # §21.2 keeps prompts and provider bodies out of stored diagnostics;
                # truncation also bounds a hostile provider error string.
                "error": error[:2000],
            },
        )
        status = result.scalar_one_or_none()
        if status is None:
            raise LeaseLostError(f"work item {item_id} is no longer owned by {owner}")
        return WorkItemStatus(status)

    async def release_expired_leases(self) -> int:
        """Return items whose lease expired to the queue (§20.4 reconciliation).

        This does not resubmit anything. It only makes the item claimable again;
        the next worker re-checks domain guards and reconciles any in-flight
        provider attempt before deciding whether a new submission is even legal.
        """
        result = await self._session.execute(
            text(
                """
                update work_items
                   set status = case
                                  when attempt_count >= max_attempts then 'DEAD'
                                  else 'QUEUED'
                                end,
                       lease_owner = null,
                       lease_expires_at = null,
                       last_error = coalesce(
                           last_error, 'lease expired; reclaimed by reconciliation')
                 where status = 'LEASED' and lease_expires_at <= now()
             returning id
                """
            )
        )
        return len(result.fetchall())

    async def cancel_for_build(self, build_id: uuid.UUID) -> int:
        """§13.4: on cancellation, stop claiming new work and mark queued work
        cancelled. Items already leased are left alone — the worker holding one
        notices the build's cancel flag and stands down on its own, which avoids
        yanking a row out from under an in-flight provider call."""
        result = await self._session.execute(
            text(
                """
                update work_items
                   set status = 'CANCELLED', lease_owner = null, lease_expires_at = null
                 where build_id = :build_id and status in ('QUEUED', 'RETRY_WAIT')
             returning id
                """
            ),
            {"build_id": build_id},
        )
        return len(result.fetchall())

    async def stats(self) -> dict[str, int]:
        """Queue depth by status, for §21.4 operational metrics."""
        result = await self._session.execute(
            text("select status, count(*) from work_items group by status")
        )
        return {row[0]: row[1] for row in result}

    async def oldest_queued_age_seconds(self) -> float | None:
        """§21.6 alerts on queue oldest age passing a threshold."""
        result = await self._session.execute(
            text(
                """
                select extract(epoch from (now() - min(available_at)))
                  from work_items
                 where status in ('QUEUED', 'RETRY_WAIT') and available_at <= now()
                """
            )
        )
        value = result.scalar_one_or_none()
        return float(value) if value is not None else None


class LeaseLostError(RuntimeError):
    """The worker tried to act on an item it no longer owns.

    Surfaced rather than swallowed: silently ignoring it would let two workers
    believe they each own the same job.
    """


def _json_or_null(payload: dict[str, Any] | None) -> str | None:
    import json

    return None if payload is None else json.dumps(payload)


__all__ = [
    "ClaimedItem",
    "LeaseConfigurationError",
    "LeaseLostError",
    "WorkQueue",
    "validate_lease_config",
]


# `IntegrityError` is imported for callers that want to distinguish a dedupe
# collision from other failures without importing SQLAlchemy themselves.
DedupeCollision = IntegrityError
