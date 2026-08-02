"""Durable work queue tests — PRD §22.3 mandatory queue and idempotency cases.

The queue is where a hackathon demo dies quietly: a worker restarts, two workers
grab the same node, and the build either duplicates a billable provider call or
stalls forever with nothing in the UI to explain it. Every test here is about one
of those two outcomes.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from takegraph_api.queue import (
    LeaseConfigurationError,
    LeaseLostError,
    WorkQueue,
    validate_lease_config,
)
from takegraph_domain.enums import WorkItemStatus


async def enqueue(session, *, key: str, **kw) -> uuid.UUID | None:
    queue = WorkQueue(session)
    item_id = await queue.enqueue(
        kind=kw.pop("kind", "execute_node"),
        target_id=kw.pop("target_id", uuid.uuid4()),
        dedupe_key=key,
        **kw,
    )
    await session.commit()
    return item_id


class TestLeaseConfiguration:
    """§10.5: lease duration must exceed the heartbeat interval by at least 3x."""

    def test_accepts_a_safe_ratio(self) -> None:
        validate_lease_config(lease_seconds=120, heartbeat_seconds=30)

    def test_rejects_a_lease_too_short_for_its_heartbeat(self) -> None:
        with pytest.raises(LeaseConfigurationError, match="at least 3x"):
            validate_lease_config(lease_seconds=60, heartbeat_seconds=30)

    def test_rejects_nonpositive_heartbeat(self) -> None:
        with pytest.raises(LeaseConfigurationError):
            validate_lease_config(lease_seconds=120, heartbeat_seconds=0)

    def test_boundary_ratio_is_allowed(self) -> None:
        validate_lease_config(lease_seconds=90, heartbeat_seconds=30)


class TestEnqueueDedupe:
    """§5.9 FR-EVT-003: re-delivery must not produce a duplicate downstream job."""

    async def test_enqueue_returns_an_id(self, session) -> None:
        assert await enqueue(session, key="node-a") is not None

    async def test_duplicate_dedupe_key_is_ignored(self, session) -> None:
        first = await enqueue(session, key="node-a")
        second = await enqueue(session, key="node-a")
        assert first is not None
        assert second is None, "a repeated dedupe key must not create a second job"

        count = await session.scalar(text("select count(*) from work_items"))
        assert count == 1

    async def test_distinct_keys_create_distinct_items(self, session) -> None:
        await enqueue(session, key="node-a")
        await enqueue(session, key="node-b")
        assert await session.scalar(text("select count(*) from work_items")) == 2


class TestClaiming:
    async def test_claim_leases_an_available_item(self, session) -> None:
        await enqueue(session, key="node-a")
        claimed = await WorkQueue(session).claim(owner="w1", lease_seconds=60)
        await session.commit()

        assert len(claimed) == 1
        assert claimed[0].attempt_count == 1, "claiming counts as an attempt"

    async def test_claimed_item_is_not_claimable_again(self, session) -> None:
        await enqueue(session, key="node-a")
        queue = WorkQueue(session)
        assert len(await queue.claim(owner="w1", lease_seconds=60)) == 1
        await session.commit()
        assert await queue.claim(owner="w2", lease_seconds=60) == []

    async def test_delayed_item_is_not_claimable_yet(self, session) -> None:
        await enqueue(session, key="node-a", delay_seconds=60)
        assert await WorkQueue(session).claim(owner="w1", lease_seconds=60) == []

    async def test_higher_priority_is_claimed_first(self, session) -> None:
        await enqueue(session, key="low", priority=0)
        await enqueue(session, key="high", priority=10)
        claimed = await WorkQueue(session).claim(owner="w1", lease_seconds=60)
        assert claimed[0].dedupe_key == "high"

    async def test_claim_can_filter_by_kind(self, session) -> None:
        """The worker pool is not homogeneous — a media worker with FFmpeg should
        not pick up an evaluator job it cannot run."""
        await enqueue(session, key="a", kind="execute_node")
        await enqueue(session, key="b", kind="compose_delivery")
        claimed = await WorkQueue(session).claim(
            owner="w1", lease_seconds=60, kinds=["compose_delivery"]
        )
        assert [c.kind for c in claimed] == ["compose_delivery"]

    async def test_claim_limit_is_respected(self, session) -> None:
        for i in range(5):
            await enqueue(session, key=f"node-{i}")
        claimed = await WorkQueue(session).claim(owner="w1", lease_seconds=60, limit=3)
        assert len(claimed) == 3


class TestConcurrentClaiming:
    """§22.3: "Concurrent workers claim one work item once."

    This is the test that justifies FOR UPDATE SKIP LOCKED. It uses real separate
    connections; a single session could not expose the race at all.
    """

    async def test_two_workers_racing_for_one_item_produce_one_winner(
        self, session, session_factory
    ) -> None:
        await enqueue(session, key="contested")

        async def try_claim(owner: str) -> int:
            async with session_factory() as s:
                claimed = await WorkQueue(s).claim(owner=owner, lease_seconds=60)
                await s.commit()
                return len(claimed)

        results = await asyncio.gather(*(try_claim(f"w{i}") for i in range(8)))
        assert sum(results) == 1, f"exactly one worker must win, got {results}"

    async def test_n_workers_over_n_items_each_get_exactly_one(
        self, session, session_factory
    ) -> None:
        item_count = 12
        for i in range(item_count):
            await enqueue(session, key=f"node-{i}")

        async def claim_one(owner: str) -> list[str]:
            async with session_factory() as s:
                claimed = await WorkQueue(s).claim(owner=owner, lease_seconds=60)
                await s.commit()
                return [c.dedupe_key for c in claimed]

        batches = await asyncio.gather(*(claim_one(f"w{i}") for i in range(item_count)))
        keys = [key for batch in batches for key in batch]

        assert len(keys) == item_count, "every item should be claimed exactly once"
        assert len(set(keys)) == item_count, f"an item was claimed twice: {keys}"

    async def test_concurrent_enqueue_of_same_key_creates_one_item(
        self, session, session_factory
    ) -> None:
        """Two events arriving at once for the same node must not both enqueue."""

        async def try_enqueue() -> uuid.UUID | None:
            async with session_factory() as s:
                item_id = await WorkQueue(s).enqueue(
                    kind="execute_node",
                    target_id=uuid.uuid4(),
                    dedupe_key="same-node",
                )
                await s.commit()
                return item_id

        results = await asyncio.gather(*(try_enqueue() for _ in range(6)))
        created = [r for r in results if r is not None]
        assert len(created) == 1, f"expected one insert to win, got {len(created)}"


class TestHeartbeatAndLeaseExpiry:
    async def test_heartbeat_extends_an_owned_lease(self, session) -> None:
        await enqueue(session, key="node-a")
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]
        await session.commit()

        assert await queue.heartbeat(claimed.id, owner="w1", lease_seconds=120) is True

    async def test_heartbeat_from_the_wrong_owner_fails(self, session) -> None:
        await enqueue(session, key="node-a")
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]
        await session.commit()

        assert await queue.heartbeat(claimed.id, owner="w2", lease_seconds=120) is False

    async def test_expired_lease_makes_the_item_claimable_again(self, session) -> None:
        """§22.3 lease expiry. The worker died without releasing anything, which
        is the normal case rather than an exotic one."""
        await enqueue(session, key="node-a")
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="dead-worker", lease_seconds=60))[0]
        await session.commit()

        # Simulate the lease ageing out without touching the queue's own logic.
        await session.execute(
            text(
                "update work_items set lease_expires_at = now() - interval '1 second' where id=:i"
            ),
            {"i": claimed.id},
        )
        await session.commit()

        retaken = await queue.claim(owner="live-worker", lease_seconds=60)
        assert len(retaken) == 1
        assert retaken[0].id == claimed.id
        assert retaken[0].attempt_count == 2, "the retake counts as a further attempt"

    async def test_heartbeat_after_expiry_fails_so_the_worker_stands_down(self, session) -> None:
        """A worker whose lease lapsed must learn about it. Returning True here
        would let two workers finish the same job."""
        await enqueue(session, key="node-a")
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]
        await session.commit()
        await session.execute(
            text(
                "update work_items set lease_expires_at = now() - interval '1 second' where id=:i"
            ),
            {"i": claimed.id},
        )
        await session.commit()

        assert await queue.heartbeat(claimed.id, owner="w1", lease_seconds=60) is False

    async def test_reconciliation_releases_expired_leases(self, session) -> None:
        """§20.4: a periodic job reclaims leases whose owner vanished."""
        await enqueue(session, key="node-a")
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="dead", lease_seconds=60))[0]
        await session.commit()
        await session.execute(
            text(
                "update work_items set lease_expires_at = now() - interval '1 second' where id=:i"
            ),
            {"i": claimed.id},
        )
        await session.commit()

        assert await queue.release_expired_leases() == 1
        await session.commit()
        status = await session.scalar(
            text("select status from work_items where id=:i"), {"i": claimed.id}
        )
        assert status == WorkItemStatus.QUEUED

    async def test_reconciliation_dead_letters_an_exhausted_item(self, session) -> None:
        """An item that has burned its attempts must not loop forever."""
        await enqueue(session, key="node-a", max_attempts=1)
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="dead", lease_seconds=60))[0]
        await session.commit()
        await session.execute(
            text(
                "update work_items set lease_expires_at = now() - interval '1 second' where id=:i"
            ),
            {"i": claimed.id},
        )
        await session.commit()

        await queue.release_expired_leases()
        await session.commit()
        status = await session.scalar(
            text("select status from work_items where id=:i"), {"i": claimed.id}
        )
        assert status == WorkItemStatus.DEAD

    async def test_reconciliation_leaves_live_leases_alone(self, session) -> None:
        await enqueue(session, key="node-a")
        queue = WorkQueue(session)
        await queue.claim(owner="healthy", lease_seconds=300)
        await session.commit()

        assert await queue.release_expired_leases() == 0


class TestCompletionAndFailure:
    async def test_complete_marks_done(self, session) -> None:
        await enqueue(session, key="node-a")
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]
        assert await queue.complete(claimed.id, owner="w1") is True
        await session.commit()

        status = await session.scalar(
            text("select status from work_items where id=:i"), {"i": claimed.id}
        )
        assert status == WorkItemStatus.DONE

    async def test_complete_from_the_wrong_owner_is_refused(self, session) -> None:
        await enqueue(session, key="node-a")
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]
        assert await queue.complete(claimed.id, owner="w2") is False

    async def test_retryable_failure_schedules_a_retry(self, session) -> None:
        await enqueue(session, key="node-a", max_attempts=3)
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]

        status = await queue.fail(
            claimed.id, owner="w1", error="provider timeout", retry_in_seconds=30
        )
        assert status is WorkItemStatus.RETRY_WAIT

    async def test_retry_is_not_immediately_claimable(self, session) -> None:
        """Backoff must actually hold the item back, or a failing provider gets
        hammered (§13.3 exponential backoff)."""
        await enqueue(session, key="node-a", max_attempts=3)
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]
        await queue.fail(claimed.id, owner="w1", error="boom", retry_in_seconds=60)
        await session.commit()

        assert await queue.claim(owner="w2", lease_seconds=60) == []

    async def test_nonretryable_failure_dead_letters_immediately(self, session) -> None:
        """§13.3: invalid input, auth failure and policy denial are never retried
        as the same attempt."""
        await enqueue(session, key="node-a", max_attempts=5)
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]

        status = await queue.fail(claimed.id, owner="w1", error="PROVIDER_AUTH_FAILED")
        assert status is WorkItemStatus.DEAD

    async def test_exhausting_max_attempts_dead_letters(self, session) -> None:
        await enqueue(session, key="node-a", max_attempts=1)
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]

        status = await queue.fail(claimed.id, owner="w1", error="boom", retry_in_seconds=1)
        assert status is WorkItemStatus.DEAD, "attempt budget exhausted, so no further retry"

    async def test_failing_an_item_you_do_not_own_raises(self, session) -> None:
        """Surfaced, not swallowed — silently ignoring this would let two workers
        each believe they own the job."""
        await enqueue(session, key="node-a")
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]

        with pytest.raises(LeaseLostError):
            await queue.fail(claimed.id, owner="impostor", error="boom", retry_in_seconds=5)

    async def test_error_text_is_truncated(self, session) -> None:
        """A hostile or enormous provider error must not become an unbounded write."""
        await enqueue(session, key="node-a", max_attempts=3)
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]
        await queue.fail(claimed.id, owner="w1", error="x" * 10_000, retry_in_seconds=5)
        await session.commit()

        stored = await session.scalar(
            text("select last_error from work_items where id=:i"), {"i": claimed.id}
        )
        assert len(stored) == 2000


class TestCancellation:
    """§13.4 cancellation semantics."""

    async def test_cancel_stops_queued_work_for_the_build(self, session) -> None:
        build_id = uuid.uuid4()
        await enqueue(session, key="a", build_id=build_id)
        await enqueue(session, key="b", build_id=build_id)
        await enqueue(session, key="other", build_id=uuid.uuid4())

        queue = WorkQueue(session)
        assert await queue.cancel_for_build(build_id) == 2
        await session.commit()

        remaining = await queue.claim(owner="w1", lease_seconds=60, limit=10)
        assert [c.dedupe_key for c in remaining] == ["other"]

    async def test_cancel_leaves_in_flight_work_to_stand_down_itself(self, session) -> None:
        """Yanking a leased row would orphan an in-flight provider call. The
        worker holding it notices the build's cancel flag instead."""
        build_id = uuid.uuid4()
        await enqueue(session, key="a", build_id=build_id)
        queue = WorkQueue(session)
        claimed = (await queue.claim(owner="w1", lease_seconds=60))[0]
        await session.commit()

        assert await queue.cancel_for_build(build_id) == 0
        status = await session.scalar(
            text("select status from work_items where id=:i"), {"i": claimed.id}
        )
        assert status == WorkItemStatus.LEASED


class TestObservability:
    async def test_stats_report_depth_by_status(self, session) -> None:
        await enqueue(session, key="a")
        await enqueue(session, key="b")
        queue = WorkQueue(session)
        await queue.claim(owner="w1", lease_seconds=60)
        await session.commit()

        stats = await queue.stats()
        assert stats.get("QUEUED") == 1
        assert stats.get("LEASED") == 1

    async def test_oldest_age_is_none_when_idle(self, session) -> None:
        assert await WorkQueue(session).oldest_queued_age_seconds() is None

    async def test_oldest_age_reports_a_waiting_item(self, session) -> None:
        await enqueue(session, key="a")
        age = await WorkQueue(session).oldest_queued_age_seconds()
        assert age is not None and age >= 0
