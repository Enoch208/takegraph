# Phase 3 Realtime Backend Design

## Goal

Build the backend foundation for the `/demo` workspace so a scoped guest can
open the real ORBIT project, restore an authoritative build snapshot after a
refresh, and follow ordered build progress over SSE without depending on Redis
history.

## Scope

This slice includes:

- scoped demo-session issuance;
- demo-project discovery;
- build summary and graph snapshot reads;
- build-scoped SSE with PostgreSQL replay;
- Redis pub/sub as a low-latency wakeup channel;
- a durable outbox publisher for unpublished domain events;
- per-node reuse events emitted by incremental-build commit;
- durable activity events for provider submission, storage, verification, and
  quality-check phases;
- authorization and tenant/project scoping for every read and stream;
- automated tests for replay, reconnect, degraded Redis operation, snapshots,
  and guest scope.

This slice excludes:

- visual `/demo` implementation;
- release-management HTTP routes and release UI;
- TEST FAULT, fallback, retake, and evidence-frame behavior;
- project-level SSE;
- Redis Streams history;
- generated TypeScript API clients.

## Architectural Principles

PostgreSQL is the only authoritative event history. State mutations and
`domain_events` inserts remain in the same transaction. Redis never determines
whether an event exists and never supplies replay history.

Redis pub/sub carries only a build identifier and latest sequence as a wakeup.
When an SSE handler wakes, it queries PostgreSQL for rows after its last emitted
sequence. Duplicate Redis messages are harmless.

SSE remains correct when Redis is unavailable. The handler polls PostgreSQL at
a bounded interval until Redis reconnects or the request ends. Redis
unavailability degrades latency but does not block reads, writes, builds, or
replay.

The graph snapshot is authoritative after refresh. SSE updates the current
view, but a reconnecting client can always fetch the snapshot and resume from
the highest applied event sequence.

## Components

### Event Contract

A shared immutable event envelope represents a `domain_events` row:

- `schema_version`;
- `sequence`;
- `event_id`;
- `event_type`;
- `organization_id`;
- `project_id`;
- optional `build_id`;
- optional `release_id`;
- `occurred_at`;
- `correlation_id`;
- `payload`.

Unknown event types and payload fields pass through unchanged. SSE uses the
database sequence as the event `id` and the event type as the SSE `event`.

### Durable Outbox Publisher

The worker owns the outbox publisher because it is already a required
long-running process for live builds. Each cycle:

1. claims unpublished rows using PostgreSQL row locks with `SKIP LOCKED`;
2. publishes a lightweight wakeup to the build-specific Redis channel;
3. sets `realtime_published_at` only after Redis confirms publication;
4. leaves the row unpublished when Redis fails;
5. retries unpublished rows on later cycles.

At-least-once Redis publication is acceptable. PostgreSQL sequence ordering and
client deduplication make duplicates safe.

Rows without `build_id` are not part of this build-scoped stream and are left
for later project/release realtime work.

### SSE Endpoint

`GET /api/v1/builds/{build_id}/events` returns `text/event-stream`.

The endpoint:

1. authenticates the principal;
2. resolves build, project, and organization;
3. enforces `VIEW_PROJECT`, tenant equality, and `project_scope_id`;
4. parses `Last-Event-ID` as a non-negative sequence;
5. emits all later build events from PostgreSQL in ascending sequence order;
6. subscribes to the build Redis channel when Redis is configured;
7. wakes on Redis messages or a database-poll timeout;
8. re-queries PostgreSQL after every wakeup;
9. emits heartbeat comments while idle;
10. stops cleanly when the client disconnects.

An invalid `Last-Event-ID` returns a typed client error before streaming begins.
A sequence older than retained history replays all available later rows. No
history-gap response is needed because Phase 3 does not prune domain events.

### Build Summary and Graph Snapshot

`GET /api/v1/builds/{build_id}` returns build identity, status, counts, parent
lineage, revision references, timestamps, and latest domain-event sequence.

`GET /api/v1/builds/{build_id}/graph` returns:

- all 18 nodes in stable graph order;
- node status, resolution, reason code, reason, fingerprint, and timestamps;
- source build lineage for reused nodes;
- selected attempt identity and normalized provider/model fields;
- selected assets with authorized API access URLs, SHA-256, media metadata, and
  verification state;
- validations with gate key, result, policy reference, and evidence;
- attempt history and durable attempt events needed by the inspector;
- latest domain-event sequence represented by the snapshot.

Provider credentials, B2 credentials, private object keys, and signed URLs from
provider payloads never appear in the response. Asset URLs use the existing
authorized access route.

The snapshot and latest sequence are read in one database transaction so a
client can apply only events with larger sequences after rendering it.

### Demo Session

`POST /api/v1/demo/session` issues a short-lived HMAC bearer token with:

- role `GUEST`;
- the demo organization;
- `project_scope_id` set to the seeded ORBIT project;
- expiry from `DEMO_SESSION_TTL_SECONDS`.

The route is available only when `AUTH_MODE=demo`. It fails readiness and
returns a configuration error if `SESSION_SECRET`, `DEMO_PROJECT_SLUG`, or the
real seeded demo project is unavailable.

`GET /api/v1/demo/project` requires that scoped token and returns the project,
the latest successful baseline build, and published `v1` release identifiers.
It does not create or silently substitute fixture data.

### Build Progress Events

Incremental commit inserts one durable `build.node.status_changed` event for
each reused node in the same transaction as the build and node rows. The
payload includes:

- build-node and stable-key identity;
- `from: PENDING`;
- `to: REUSED`;
- source build and source build-node identity;
- reason code;
- selected attempt identity.

The worker persists `build.node.activity_changed` events for UI-relevant
activity boundaries:

- `SUBMITTED`, with normalized provider and model;
- `GENERATING`;
- `STORING`;
- `VERIFYING`;
- `QUALITY_CHECK`.

These events describe real persisted transitions only. They do not estimate
percent completion or manufacture provider progress. Existing
`build.node.status_changed` events continue to carry lifecycle state.

## Authorization

All build reads and streams use one service-layer authorization function. It
requires:

- principal organization equals project organization;
- principal has `VIEW_PROJECT`;
- when `project_scope_id` exists, it equals the build project;
- guest principals may access only `Project.is_demo = true`.

SSE authorization happens before response creation. Redis channel names contain
build UUIDs but no tenant secrets or credentials.

## Ordering and Recovery

The database `sequence` is the global ordering key. Each build stream filters by
build ID and preserves increasing sequence order.

Clients deduplicate by sequence. Reconnecting with `Last-Event-ID=N` receives
only rows with `sequence > N`.

A browser refresh uses this order:

1. fetch the graph snapshot;
2. record `latest_event_sequence`;
3. render the snapshot;
4. open SSE with that sequence;
5. apply only larger events.

An event committed between the snapshot transaction and SSE connection is
returned by PostgreSQL replay.

## Failure Behavior

- Redis unavailable: publisher leaves rows unpublished; SSE polls PostgreSQL.
- Redis duplicate publication: SSE query emits each sequence once per
  connection.
- SSE disconnect: server releases database and Redis resources; client resumes
  with `Last-Event-ID`.
- Unknown event type: preserved and emitted.
- Unauthorized build: return the existing not-found/forbidden contract without
  leaking cross-tenant existence.
- Missing demo baseline: report the real unavailable reason.
- Database failure: terminate the stream rather than emit a synthetic success
  or empty terminal state.

## Testing Strategy

Implementation follows red-green-refactor. Each production behavior begins
with a failing test.

Domain tests cover event-envelope validation and serialization.

API integration tests with real PostgreSQL cover:

- build authorization and guest project scope;
- summary and complete 18-node snapshot restoration;
- snapshot latest-sequence consistency;
- SSE envelope and content type;
- ordered initial replay;
- `Last-Event-ID` reconnect without duplicates;
- invalid cursor rejection;
- unknown event preservation;
- per-node reused events from commit;
- scoped demo session issuance and expiry;
- demo mode/configuration failures.

Publisher tests cover:

- unpublished event publication;
- publication timestamp only after Redis success;
- retry after Redis failure;
- duplicate-safe publication;
- concurrent publishers using `SKIP LOCKED`;
- build-channel isolation.

Degraded-mode SSE tests use a failing Redis adapter and prove PostgreSQL polling
still emits committed events.

The slice exits only when Ruff, formatting, strict mypy, web TypeScript, the
full pytest suite, and a live local Redis/PostgreSQL SSE smoke test pass.

## Acceptance Criteria

- A scoped guest can discover only the seeded ORBIT demo.
- A guest can read the baseline build and all 18 graph nodes with real selected
  assets and evidence.
- Connecting with no cursor replays ordered persisted events.
- Reconnecting with `Last-Event-ID` emits every later event exactly once on
  that connection.
- Redis loss does not lose events or block the stream permanently.
- Incremental commit produces 14 durable reused-node events.
- Rebuilt nodes expose real submitted, generating, storing, verifying, and
  quality-check activity.
- Refreshing during a build restores the snapshot and resumes without state
  regression or duplicate application.
- No provider or B2 credentials reach the browser.
- No fixture fallback activates because a credential or live baseline is
  missing.
