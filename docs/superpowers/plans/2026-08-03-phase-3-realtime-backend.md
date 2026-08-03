# Phase 3 Realtime Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the refresh-safe backend for the real ORBIT demo with scoped guest access, authoritative graph snapshots, PostgreSQL-replayed SSE, and Redis wakeups.

**Architecture:** PostgreSQL `domain_events` is the sole history. A worker-owned outbox publisher sends duplicate-safe Redis pub/sub wakeups, while SSE always queries PostgreSQL by sequence and polls it when Redis is unavailable. Build snapshots and scoped demo-session routes provide authoritative restoration and access control.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL 16, redis-py asyncio, Pydantic v2, pytest, pytest-asyncio, httpx.

## Global Constraints

- Follow red-green-refactor: no production behavior before a test has failed for its absence.
- Add no code comments or docstrings.
- PostgreSQL remains authoritative; Redis must never be required for correctness.
- Never expose provider credentials, B2 credentials, private object keys, or raw signed provider URLs.
- Unknown event types and payload fields must survive serialization.
- SSE `id` is the authoritative `domain_events.sequence`.
- Guest access is limited to one real `Project.is_demo` project through `project_scope_id`.
- Missing capabilities fail truthfully and never activate fixtures.
- Do not commit unless the user explicitly requests a commit.

---

### Task 1: Shared Domain Event Contract

**Files:**
- Create: `packages/domain/takegraph_domain/events/__init__.py`
- Create: `packages/domain/takegraph_domain/events/types.py`
- Create: `packages/domain/tests/test_event_types.py`

**Interfaces:**
- Produces: `DomainEventEnvelope.from_record(...)`
- Produces: `SseEvent.encode() -> str`
- Consumes: primitive values copied from a persisted `DomainEvent`

- [ ] **Step 1: Write failing contract tests**

Add tests proving immutable validation, UTC timestamp serialization, unknown
event preservation, and exact SSE framing:

```python
def test_sse_uses_sequence_as_id_and_preserves_unknown_type() -> None:
    envelope = DomainEventEnvelope(
        sequence=42,
        event_id=uuid.UUID("00000000-0000-0000-0000-000000000042"),
        event_type="future.event",
        organization_id=None,
        project_id=None,
        build_id=None,
        occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
        correlation_id=None,
        payload={"new_field": {"nested": True}},
    )

    encoded = SseEvent(envelope=envelope).encode()

    assert encoded.startswith("id: 42\nevent: future.event\ndata: ")
    assert '"new_field":{"nested":true}' in encoded
    assert encoded.endswith("\n\n")
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest packages/domain/tests/test_event_types.py -q
```

Expected: import failure because `takegraph_domain.events.types` does not exist.

- [ ] **Step 3: Implement the minimal immutable models**

Implement:

```python
class DomainEventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    sequence: int = Field(ge=1)
    event_id: uuid.UUID
    event_type: str = Field(min_length=1, max_length=64)
    organization_id: uuid.UUID | None
    project_id: uuid.UUID | None
    build_id: uuid.UUID | None
    occurred_at: datetime
    correlation_id: uuid.UUID | None
    payload: dict[str, JsonValue]


class SseEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    envelope: DomainEventEnvelope

    def encode(self) -> str:
        data = self.envelope.model_dump_json(exclude_none=False)
        return f"id: {self.envelope.sequence}\nevent: {self.envelope.event_type}\ndata: {data}\n\n"
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest packages/domain/tests/test_event_types.py -q
```

Expected: all event-contract tests pass.

- [ ] **Step 5: Run domain type and lint checks**

Run:

```bash
uv run ruff check packages/domain
uv run mypy packages/domain/takegraph_domain
```

Expected: both pass.

---

### Task 2: Build Read Authorization and Snapshot Service

**Files:**
- Create: `services/api/takegraph_api/build_reads.py`
- Create: `services/api/takegraph_api/builds.py`
- Create: `services/api/tests/test_build_reads.py`
- Modify: `services/api/takegraph_api/main.py`

**Interfaces:**
- Produces: `BuildReadService.summary(build_id, principal)`
- Produces: `BuildReadService.graph(build_id, principal)`
- Produces: `GET /api/v1/builds/{build_id}`
- Produces: `GET /api/v1/builds/{build_id}/graph`
- Consumes: existing `Principal`, ORM build graph, attempt, asset, validation, and event rows

- [ ] **Step 1: Write failing authorization and summary tests**

Seed one demo and one non-demo project in separate organizations. Prove:

```python
async def test_scoped_guest_reads_only_its_demo_build(
    session: AsyncSession, guest: Principal
) -> None:
    expected = await seed_build_snapshot(session, guest.organization_id, is_demo=True)
    service = BuildReadService(session)

    summary = await service.summary(expected.build_id, guest)

    assert summary.id == expected.build_id
    assert summary.latest_event_sequence == expected.latest_sequence


async def test_scoped_guest_cannot_read_another_project_build(
    session: AsyncSession, guest: Principal
) -> None:
    other = await seed_build_snapshot(session, uuid.uuid4(), is_demo=True)

    with pytest.raises(ForbiddenError):
        await BuildReadService(session).summary(other.build_id, guest)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest services/api/tests/test_build_reads.py -q
```

Expected: import failure because `BuildReadService` does not exist.

- [ ] **Step 3: Implement one service-layer access check**

Implement a private resolver that loads build and project, calls
`authorize_project(..., Permission.VIEW_PROJECT)`, enforces
`project_scope_id`, and rejects guest access when `project.is_demo` is false.

- [ ] **Step 4: Implement summary response**

Return build IDs, revision IDs, status, parent lineage, counts, fixture flag,
timestamps, and:

```python
latest_event_sequence = (
    await session.scalar(
        select(func.max(DomainEvent.sequence)).where(DomainEvent.build_id == build.id)
    )
    or 0
)
```

- [ ] **Step 5: Run summary tests and verify GREEN**

Run:

```bash
uv run pytest services/api/tests/test_build_reads.py -q
```

Expected: summary and authorization tests pass.

- [ ] **Step 6: Write failing 18-node graph restoration test**

The test must assert graph order, reused lineage, selected attempt, selected
asset SHA-256 and access endpoint, validations, attempt history, and snapshot
sequence:

```python
async def test_graph_snapshot_restores_real_node_evidence(
    session: AsyncSession, guest: Principal
) -> None:
    seeded = await seed_build_snapshot(session, guest.organization_id, is_demo=True)

    graph = await BuildReadService(session).graph(seeded.build_id, guest)

    assert len(graph.nodes) == 18
    assert graph.latest_event_sequence == seeded.latest_sequence
    assert graph.nodes[0].stable_key == "source.brief"
    assert graph.nodes[-1].stable_key == "compose.delivery_package"
    assert graph.nodes[-1].selected_attempt is not None
    assert graph.nodes[-1].selected_assets[0].sha256 == seeded.delivery_sha256
```

- [ ] **Step 7: Run the graph test and verify RED**

Expected: `BuildReadService.graph` is absent.

- [ ] **Step 8: Implement graph snapshot projection**

Use bounded bulk queries rather than one query per node. Return access API paths
such as `/api/v1/assets/{asset_id}/access`, never raw B2 keys or provider URLs.
Order nodes by the persisted graph topological order or template ordinal.

- [ ] **Step 9: Mount the build router**

Add the router to `main.py` and expose typed response models for summary and
graph.

- [ ] **Step 10: Verify API boundary**

Run:

```bash
uv run pytest services/api/tests/test_build_reads.py -q
uv run mypy services/api/takegraph_api
```

Expected: tests and strict types pass.

---

### Task 3: Scoped Demo Session and Project Discovery

**Files:**
- Create: `services/api/takegraph_api/demo.py`
- Create: `services/api/tests/test_demo_session.py`
- Modify: `services/api/takegraph_api/main.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `POST /api/v1/demo/session`
- Produces: `GET /api/v1/demo/project`
- Consumes: `HmacSessionProvider`, `SessionClaims`, demo project/build/release rows

- [ ] **Step 1: Write failing session issuance tests**

Prove demo mode, exact project scope, role, TTL, and no fixture fallback:

```python
async def test_demo_session_is_scoped_to_real_seeded_project(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await seed_demo_project(session)
    configure_demo(monkeypatch)

    issued = await DemoService(session).issue_session()
    principal = HmacSessionProvider(TEST_SECRET).authenticate(f"Bearer {issued.token}")

    assert principal.role is Role.GUEST
    assert principal.project_scope_id == project.id
    assert issued.expires_at > issued.issued_at
```

- [ ] **Step 2: Run the tests and verify RED**

Expected: import failure because `DemoService` does not exist.

- [ ] **Step 3: Implement demo-mode validation and token issuance**

Require `AUTH_MODE=demo`, a secret of at least 32 characters,
`DEMO_PROJECT_SLUG`, and one real `Project.is_demo` row. Issue a unique nonce
and TTL from `DEMO_SESSION_TTL_SECONDS`.

- [ ] **Step 4: Write failing project discovery tests**

Prove the route returns the real demo project, latest successful non-fixture
build, and published `v1`; prove another guest scope is denied.

- [ ] **Step 5: Implement project discovery**

Use the authenticated scoped principal. Return typed IDs and statuses only;
do not create data from this read route.

- [ ] **Step 6: Mount router and verify**

Run:

```bash
uv run pytest services/api/tests/test_demo_session.py services/api/tests/test_session_auth.py -q
```

Expected: all demo and auth tests pass.

---

### Task 4: PostgreSQL-Replayed SSE

**Files:**
- Create: `services/api/takegraph_api/realtime/__init__.py`
- Create: `services/api/takegraph_api/realtime/store.py`
- Create: `services/api/takegraph_api/realtime/sse.py`
- Create: `services/api/tests/test_build_events.py`
- Modify: `services/api/takegraph_api/builds.py`

**Interfaces:**
- Produces: `DomainEventStore.after(build_id, sequence, limit)`
- Produces: `BuildEventStream.iter_events(...)`
- Produces: `GET /api/v1/builds/{build_id}/events`
- Consumes: `DomainEventEnvelope`, build authorization from Task 2

- [ ] **Step 1: Write failing ordered replay tests**

Prove event ordering and exact cursor semantics:

```python
async def test_store_replays_only_sequences_after_cursor(
    session: AsyncSession, guest: Principal
) -> None:
    seeded = await seed_event_stream(session, guest.organization_id, sequences=3)

    events = await DomainEventStore(session).after(seeded.build_id, seeded.sequences[0])

    assert [event.sequence for event in events] == seeded.sequences[1:]
```

- [ ] **Step 2: Run and verify RED**

Expected: import failure because `DomainEventStore` does not exist.

- [ ] **Step 3: Implement bounded PostgreSQL replay**

Query by build ID and `sequence > cursor`, order ascending, and cap each page.
Convert each ORM row to `DomainEventEnvelope` without event-type filtering.

- [ ] **Step 4: Run store tests and verify GREEN**

- [ ] **Step 5: Write failing SSE HTTP tests**

Use the authenticated ASGI client. Assert:

- `content-type` starts with `text/event-stream`;
- SSE IDs equal database sequences;
- `Last-Event-ID` excludes already applied rows;
- unknown event type survives;
- invalid negative/non-integer cursor returns a typed 400 response;
- unauthorized requests fail before streaming.

- [ ] **Step 6: Run HTTP tests and verify RED**

Expected: route returns 404.

- [ ] **Step 7: Implement stream response**

Parse and validate the header before creating `StreamingResponse`. The generator
repeatedly drains all available PostgreSQL pages, emits heartbeats when idle,
and checks `request.is_disconnected()`.

- [ ] **Step 8: Verify replay and reconnect**

Run:

```bash
uv run pytest services/api/tests/test_build_events.py -q
```

Expected: ordered replay and reconnect tests pass.

---

### Task 5: Redis Wakeups and Durable Outbox Publisher

**Files:**
- Create: `services/api/takegraph_api/realtime/redis.py`
- Create: `services/api/takegraph_api/realtime/publisher.py`
- Create: `services/api/tests/test_realtime_publisher.py`
- Modify: `services/api/takegraph_api/realtime/sse.py`
- Modify: `services/worker/takegraph_worker/__main__.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `RedisWakeupBus.publish(build_id, sequence)`
- Produces: `RedisWakeupBus.subscribe(build_id)`
- Produces: `OutboxPublisher.publish_once(limit=100)`
- Consumes: `REDIS_URL`, unpublished `DomainEvent` rows

- [ ] **Step 1: Write failing publisher success test**

Inject a recording wakeup bus:

```python
async def test_publisher_marks_event_after_success(
    session: AsyncSession, recording_bus: RecordingWakeupBus
) -> None:
    event = await seed_unpublished_event(session)

    receipt = await OutboxPublisher(session, recording_bus).publish_once()

    await session.refresh(event)
    assert receipt.published == 1
    assert recording_bus.messages == [(event.build_id, event.sequence)]
    assert event.realtime_published_at is not None
```

- [ ] **Step 2: Run and verify RED**

Expected: import failure because `OutboxPublisher` does not exist.

- [ ] **Step 3: Implement claim/publish/mark**

Claim build-scoped unpublished events in ascending sequence using
`FOR UPDATE SKIP LOCKED`. Publish each wakeup, set its timestamp only after
success, and commit the batch in the caller.

- [ ] **Step 4: Write failing Redis-error retry test**

Prove a failed publish leaves `realtime_published_at` null and the next call can
publish it.

- [ ] **Step 5: Implement failure behavior and verify GREEN**

Do not convert Redis failure into a successful receipt. The worker loop logs a
safe degraded event and retries later.

- [ ] **Step 6: Write failing concurrent publisher test**

Run two sessions concurrently and assert each event sequence is claimed by at
most one publisher in that cycle.

- [ ] **Step 7: Implement redis-py adapter**

Use `redis.asyncio.Redis.from_url`. Channel names are
`takegraph:build:{build_id}:events`. Publish compact canonical JSON containing
`build_id` and `sequence`.

- [ ] **Step 8: Add worker publishing cycle**

Create the bus once in `run()`. Invoke `publish_once` every worker loop before
claiming work. Close Redis during shutdown. A missing or failing `REDIS_URL`
must not stop queue execution.

- [ ] **Step 9: Add Redis wakeups to SSE**

After initial PostgreSQL drain, race a Redis notification against the poll
timeout. On either result, query PostgreSQL. If subscribe fails, continue
polling and periodically retry the subscription.

- [ ] **Step 10: Verify publisher and degraded SSE**

Run:

```bash
uv run pytest services/api/tests/test_realtime_publisher.py services/api/tests/test_build_events.py -q
```

Expected: success, retry, concurrency, and degraded polling tests pass.

---

### Task 6: Reused-Node and Activity Domain Events

**Files:**
- Create: `services/worker/takegraph_worker/domain_events.py`
- Create: `services/worker/tests/test_domain_events.py`
- Modify: `services/api/takegraph_api/changes.py`
- Modify: `services/api/tests/test_changes.py`
- Modify: worker handlers that transition attempts and nodes
- Modify: relevant worker tests

**Interfaces:**
- Produces: 14 `build.node.status_changed` events during AS-01 commit
- Produces: `build.node.activity_changed` events at persisted phase boundaries
- Consumes: existing transaction/session, build, node, attempt, provider/model

- [ ] **Step 1: Write failing 14-reuse-event test**

Extend the real PostgreSQL AS-01 commit test:

```python
events = (
    await session.scalars(
        select(DomainEvent).where(
            DomainEvent.build_id == result.build_id,
            DomainEvent.event_type == "build.node.status_changed",
        )
    )
).all()

assert len(events) == 14
assert {event.payload_json["to"] for event in events} == {"REUSED"}
assert all(event.payload_json["source_build_id"] for event in events)
assert all(event.payload_json["selected_attempt_id"] for event in events)
```

- [ ] **Step 2: Run and verify RED**

Expected: zero reused-node transition events.

- [ ] **Step 3: Emit reuse events in commit transaction**

Insert each event while constructing the reused `BuildNode`. Include stable key,
lineage, reason code, and selected attempt. Preserve the existing aggregate
`build.created` event.

- [ ] **Step 4: Run change tests and verify GREEN**

- [ ] **Step 5: Write failing activity-event tests**

For one provider node and one local node, assert the exact durable phase order:

```python
assert activities == [
    "SUBMITTED",
    "GENERATING",
    "STORING",
    "VERIFYING",
    "QUALITY_CHECK",
]
```

Local nodes omit provider/model only when no provider submission occurs, while
still emitting real storage, verification, and quality-check phases.

- [ ] **Step 6: Run and verify RED**

Expected: no `build.node.activity_changed` events exist.

- [ ] **Step 7: Implement a shared worker event writer**

Centralize event construction so all handlers use one payload contract. Call it
only in the transaction that persists the corresponding attempt/node state.

- [ ] **Step 8: Wire all required handlers**

Cover copy, plan, narration, music, GMI image/video, local image, end card, and
delivery. Do not infer phases from elapsed time.

- [ ] **Step 9: Verify worker event tests**

Run:

```bash
uv run pytest services/worker/tests/test_domain_events.py services/worker/tests/test_build_work.py services/worker/tests/test_gmi_gateway.py -q
```

Expected: phase ordering and existing worker behavior pass.

---

### Task 7: End-to-End Backend Verification and Working Notes

**Files:**
- Create: `services/api/tests/test_realtime_live.py`
- Modify: `.env.example`
- Modify: `AGENTS.md`

**Interfaces:**
- Produces: a repeatable integration test proving PostgreSQL, Redis, auth,
  snapshot, SSE wakeup, reconnect, and degraded operation together

- [ ] **Step 1: Write the live integration test**

The test uses the existing real-PostgreSQL cleanup fixture and a live Redis
connection. It:

1. seeds an isolated demo project and 18-node build;
2. obtains a demo session through the HTTP route;
3. discovers that project and build through the HTTP route;
4. fetches the graph snapshot and records its sequence;
5. opens SSE from that sequence;
6. commits a new `DomainEvent` for the isolated build;
7. runs one outbox publication cycle against live Redis;
8. observes the event through SSE;
9. reconnects with `Last-Event-ID`;
10. proves no duplicate replay;
11. truncates the isolated rows through the normal test cleanup fixture.

- [ ] **Step 2: Run focused backend suites**

Run:

```bash
uv run pytest packages/domain/tests/test_event_types.py services/api/tests/test_build_reads.py services/api/tests/test_demo_session.py services/api/tests/test_build_events.py services/api/tests/test_realtime_publisher.py services/api/tests/test_changes.py services/worker/tests/test_domain_events.py -q
```

Expected: all pass.

- [ ] **Step 3: Run full repository gate**

Run:

```bash
make check
```

Expected: Ruff, format, strict mypy, web TypeScript, and full pytest pass.

- [ ] **Step 4: Run live local smoke**

Run the integration test against local PostgreSQL and Redis:

```bash
uv run pytest services/api/tests/test_realtime_live.py -q
```

Expected: one live integration test passes.

- [ ] **Step 5: Verify Redis degraded mode**

The same file includes a second test with a closed Redis adapter. It commits a
new event, lets the SSE polling deadline expire, and asserts the event arrives
from PostgreSQL exactly once.

```bash
uv run pytest services/api/tests/test_realtime_live.py::test_sse_recovers_when_redis_is_unavailable -q
```

Expected: the degraded integration test passes.

- [ ] **Step 6: Update durable working notes**

Record only command-verified facts in `AGENTS.md`: endpoint contracts, replay
semantics, degraded behavior, test counts, and any package/runtime gotchas.

- [ ] **Step 7: Final review**

Confirm:

- no raw B2 keys or credentials in responses;
- no fixture activation;
- no swallowed Redis/database errors;
- no code comments or docstrings added;
- no unrequested commit created.
