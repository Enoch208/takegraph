## Inspiration

A sixteen-second product film is eighteen generated assets: a brief, a product reference, a shot plan, four keyframes, four video clips, a copy pack, narration, a music bed, an end card, a poster, and a composed master.

The legal team changes four words the day before launch.

The honest answer to "what do we need to regenerate?" is **four things**. The answer every generative pipeline gives is **all eighteen** — not because the work is wrong, but because nothing in the system knows what that change actually touched.

That is a solved problem in software and an unsolved one in media. Your compiler does not rebuild an entire project when you edit one file; it knows the dependency graph. Generative pipelines don't have one. So they burn compute and provider spend regenerating work that was already correct, they burn wall-clock at exactly the moment there is none, and they silently discard the specific outputs a human already reviewed — so the approval has to happen again.

The second thing that bothered me: these pipelines are fragile in a way nobody can audit. Providers time out. Models return schema-invalid output. A worker dies mid-submission and nobody knows whether the account was billed. And when someone asks *"which model produced this frame, and did it pass review?"*, the answer lives in a Slack thread.

**TAKEGRAPH is a build system for generative media.** Rebuild only what changed. Recover without a human. Prove every byte.

## What it does

TAKEGRAPH treats a production as a **dependency graph**. Every output is content-addressed in Backblaze B2 and fingerprinted from its own recipe *and the selected output hashes of its inputs*, so the system can prove which work survived a change.

**Change one line of legal copy on a real 18-node build:**

```
>>> 4 REBUILD / 14 REUSE   (2 provider calls) <<<

   rebuild  copy.pack                  NODE_SPEC_CHANGED
   rebuild  audio.narration            UPSTREAM_FINGERPRINT_CHANGED
   rebuild  graphic.end_card           UPSTREAM_FINGERPRINT_CHANGED
   rebuild  compose.delivery_package   UPSTREAM_FINGERPRINT_CHANGED
```

**14 of 18 nodes and 10 of 12 provider calls avoided.** Every reuse carries a reason code you can audit — a rebuild is never unexplained.

**It heals itself.** I injected a provider timeout into a video node mid-build:

| attempt | mechanism | model | outcome |
|---|---|---|---|
| 1 | `PRIMARY` | `pixverse-v6-i2v` | **FAILED** — `TEST_FAULT_PROVIDER_TIMEOUT`, class `TRANSIENT` |
| 2 | `SAME_PROVIDER_RETRY` | `pixverse-v6-i2v` | **SUCCEEDED**, `parent_attempt_id` linked |

The node ended `PASSED` with reason *"Transient failure; retrying the same model (1 of 2)"*. The build finished all 18 nodes in 13m 01s. Nobody intervened, and both attempts stay on the permanent record.

**Every claim is checkable.** A published release lists the SHA-256 of every asset it shipped. Hitting *Verify* does not read a flag in a database — it re-downloads every byte from B2 and re-hashes it, live. **8 assets verified in 6.7 seconds.** That endpoint is deliberately open to guests, because a verification claim nobody can independently re-run is worth very little.

## How we built it

**Backblaze B2 is not where the output goes. It is the mechanism that makes incremental rebuild possible at all.**

Every asset is stored at a content-addressed key — `tenants/{org}/cas/sha256/aa/bb/{hash}.{ext}` — through `genblaze_s3.S3StorageBackend`. Content addressing is what gives the build system three properties it cannot work without:

- **Dedupe is free and automatic.** Two builds that produce identical bytes converge on one object. That is what makes "14 nodes reused" cheap rather than a bookkeeping fiction.
- **Reuse is provable, not assumed.** Condition six of the reuse proof re-reads the bytes from B2 and re-hashes them. A record in Postgres saying "this asset exists" is never trusted on its own.
- **A release is verifiable by a third party.** The manifest is a list of hashes; anyone can re-derive them from the bucket.

The B2 integration goes deeper than `put_object`:

- **Two buckets, two least-privilege keys.** A work bucket for build artifacts and a release bucket for published masters, each with its own scoped application key. A test asserts the work key is *refused* against the release bucket. The master key is used exactly once, as a setup key, and `scripts/b2_setup.py` **raises** rather than ever writing a key that carries `bypassGovernance`.
- **HMAC-signed event notifications** drive an upload reconciler, so an upload that completes while the worker is down is still discovered and indexed.
- **Presigned PUT/GET only**, short-lived, never persisted — signed URLs are generated on demand after authorization.
- **Object Lock retention is read back from B2** and reported honestly as `NOT_CONFIGURED` rather than assumed.

**Genblaze is the generation layer.** `genblaze_core` supplies `Pipeline`, `Run`, `RunBuilder`, `Step`, `Manifest`, `Asset`, the `Modality`/`RunStatus`/`StepStatus` enums, the observability events, and `ObjectStorageSink` + `KeyStrategy` — which is how the content-addressed key scheme plugs into B2. Provider adapters come from `genblaze_gmicloud` (`GMICloudImageProvider`, `GMICloudVideoProvider`) and `genblaze_elevenlabs` (`ElevenLabsTTSProvider`). Crucially, `genblaze_core`'s `ProviderErrorCode` is what the recovery policy keys off — retry decisions are made on a typed enum, never by string-matching an error message.

**The 18-node ORBIT graph** spans five providers:

| Node type | Count | Provider |
|---|---|---|
| `SOURCE_TEXT` / `SOURCE_IMAGE` | 2 | resolved from the project revision |
| `STRUCTURED_PLAN` / `STRUCTURED_TEXT` | 2 | Anthropic `claude-sonnet-4-6` |
| `IMAGE_GENERATION` | 4 | GMI Cloud `seedream-5.0-pro` |
| `VIDEO_GENERATION` | 4 | GMI Cloud `pixverse-v6-i2v` |
| `AUDIO_GENERATION` | 2 | ElevenLabs `eleven_multilingual_v2`, `music_v2` |
| transform / composition | 4 | local Pillow + ffmpeg |

**The rest of the stack.** A pure `packages/domain` with zero I/O — canonicalisation, fingerprints, the reuse proof, the impact engine, five state machines and the recovery ladder all live there, which is why they are testable without a database or a provider account. A FastAPI service for transport and persistence that never calls a provider. A separate worker that is the only thing that talks to providers or writes bytes. PostgreSQL 16 as a durable work queue using `FOR UPDATE SKIP LOCKED` with leases and heartbeats. Next.js 16 / React 19 / Tailwind v4 for the workspace.

Fingerprints are canonicalised with **JCS (RFC 8785)** and hashed with SHA-256. The domain implements JCS itself — with a conformance test asserting it matches `genblaze_core.canonical_json` byte-for-byte — so the domain keeps its no-I/O boundary while provably agreeing with the SDK.

## Challenges we ran into

**The bug that made the product's central claim false — silently.**

The worker recorded a source node's output as the JCS hash of `{"brief_text": …}`. The impact engine expected the whitespace-normalised text hash. Two defensible hashes that never agree — so a resolved source node could **never** satisfy the reuse proof. Every source reported `CACHE_ASSET_MISSING`, which invalidated its dependents, and theirs.

A one-word legal change rebuilt **16 of 18 nodes instead of 4.** The entire thesis was false, and *nothing failed while it was true* — 470-odd tests passed. I only found it because I checked the headline claim against a live build before filming the demo. The definition now lives once, both callers import it, and a conformance test asserts they agree.

**One handler had recovery; four didn't.**

The GMI handler grew a correct recovery branch and the other four never did, so a failed Anthropic or ElevenLabs node parked itself for recovery and then died with *"attempt is in unsupported state"*. No handler understood a human-ordered retake at all. The reason nobody noticed: **no node in the system had ever reached a second attempt**, so every path that depends on one was untested. Finding one bug there led to four more — an idempotency key that would have collided on a repeat retry, a retry denylist implemented as an allowlist, and a provider prompt limit.

**The provider's real limit was not the documented one.**

The music prompt embedded the whole shot-plan JSON and was guarded at 5,000 characters. ElevenLabs' actual limit is **4,100**, so a 4,551-character prompt passed our own check and came back `422`. Terminal — no retry policy clears a 4xx — and the build died with it. Camera bodies and focal lengths were never something a music model could act on anyway; the bed now takes the brief, the length and the shot beats, in 410 characters.

**A dashboard that looked broken while behaving correctly.**

Eighteen storyboard tiles pointed the browser at full-resolution originals — up to 1.6 MB each — for a box a couple of hundred pixels wide. Slow (fourteen seconds to fill), expensive (eighteen B2 Class B transactions *per page view*, because a presigned URL carries a fresh signature and can never be cached), and fragile: when the daily cap was reached, B2 answered `403` with an XML body, and a browser asked to render XML as an image draws a broken-image glyph.

The fix is a content-addressed WebP poster cache: the **first request per asset costs one B2 read, every request after costs none.** 14s → 1.16s, 18 requests → 2, ~6 MB → ~174 KB. Video posters are extracted a little way into the clip, because several ORBIT clips open on a near-black frame and frame zero rendered a black rectangle that read as failure.

**Sequential work that looked like a server error.**

Release verification checked eight assets one after another — the sum of every round trip, 78 seconds on a degraded link. Long enough that the proxy in front hung up, so the browser reported *Internal Server Error* while the API was quietly succeeding. The checks are independent; only the verdict combines. **78s → 6.7s.**

**Tests were sharing the development database.** The suite fell back to the dev `DATABASE_URL` and truncated tables between cases — wiping in-flight demo builds, and failing a *different* pair of tests each run from leftover rows. That is what made the flakiness look random rather than stateful.

**One failed build stranded the project permanently.** Impact preview demands a successful baseline for the base revision; a failed build leaves the head revision without one, and previewing against the older revision is refused as stale. Between the two guards there was no way to build the project again, ever.

## Accomplishments that we're proud of

- **The thesis is measured, not asserted.** 4 rebuild / 14 reuse, 2 provider calls instead of 12, on a real build against real providers — with a per-node reason code for every decision.
- **Self-healing demonstrated end-to-end**, with the recovery visible in the product's own attempt record: two attempts, linked parent to child, one labelled `TEST FAULT`.
- **Fault injection that cannot lie.** Two independent guards are required before a fault can be injected — an environment flag *and* a demo-scoped project — rules are single-use with a TTL, and every injected attempt renders a red `TEST FAULT` badge. A fault this system induces can never be mistaken for one a provider caused.
- **Release verification open to guests.** The proof is worth nothing if only the author can run it.
- **485 tests**, a pure domain with no I/O, and five state machines defined as transition tables where the *absent* edges do the work — there is no `RUNNING → PASSED`, so nothing can skip storage and verification.
- **The demo video drives the real app.** Puppeteer operates the live application, ElevenLabs narrates, ffmpeg assembles — no screen recording, no mockups.

## What we learned

**Content addressing is a correctness feature, not a storage optimisation.** I went in thinking B2 was where the files go. Building the reuse proof changed that: because keys *are* hashes, "is this the same work?" and "are these the same bytes?" become the same question. Condition six of the proof — re-read from B2 and re-hash — is only cheap because of it. B2 ended up being load-bearing for the core algorithm rather than a backend detail.

**A system can be confidently wrong while every test passes.** The source-hash bug is the lesson of this build. Both hashes were reasonable, both were tested in isolation, and the contract between them existed only in a comment. Now it is one function with a conformance test — and I check headline claims against a live system before I believe them.

**Untested paths cluster.** No node had ever reached a second attempt, so *every* second-attempt path was broken at once: recovery in four of five handlers, retake handling in all five, idempotency slots, and the retry classification. When you find one bug in a region, the rest of that region is probably untested too.

**Read the installed package, not the docs.** The Genblaze modules are `genblaze_core` / `genblaze_s3` / `genblaze_gmicloud`, and `genblaze_core` has zero top-level exports — introspecting the installed package on day one saved a day of wrong assumptions. Same lesson from ElevenLabs' real 4,100-character limit.

**Fail loudly, and never fabricate a number.** Pricing data is absent, so estimates render `UNKNOWN` rather than a plausible guess. Object Lock is read back from B2 and reported as `NOT_CONFIGURED` rather than assumed. The `REAL BASELINE` badge in the UI reflects what actually ran.

## What's next for Takegraph

- **Deploy it.** It runs locally against real providers and real B2 today; the API and worker need a host with a long-running process, and the frontend is already Vercel-ready.
- **Exercise cross-provider fallback end to end.** The ladder is implemented and unit-tested, but the transient rung always recovered first — I have not seen a real cross-vendor failover fire, and I will not claim one until I have.
- **Pricing ingestion**, so the impact preview shows *"this change costs $0.42 instead of $4.90"* — the number a producer actually decides on.
- **Agent-driven repair.** When a quality gate fails, propose a bounded, human-approved parameter change rather than a blind retake.
- **Object Lock on the release bucket** for genuine immutability of published masters, with retention surfaced in the proof page.
- **Partial-node reuse** — a shot plan where only shot 3 changed should rebuild one clip, not four.

---

### Built with (tags)

```
python  fastapi  pydantic  sqlalchemy  alembic  postgresql  redis
typescript  next.js  react  tailwindcss
backblaze-b2  genblaze  genblaze-s3  s3
anthropic  claude  gmi-cloud  elevenlabs
ffmpeg  pillow  puppeteer  docker  pytest  server-sent-events
```

### Try it out

- **Code:** https://github.com/Enoch208/takegraph
- *(add a second link only if you deploy — do not link a dead domain)*

---

## Providers and models

Five AI providers across the 18-node ORBIT graph. Nothing is hardcoded: every
model ID is declared in a versioned provider policy
(`packages/domain/takegraph_domain/graph/orbit_policies.py`) and resolved from
the environment at build time.

| Provider | Model | What it produces | Nodes | Policy |
|---|---|---|---|---|
| **Anthropic** | `claude-sonnet-4-6` | Shot plan (`STRUCTURED_PLAN`) and copy pack (`STRUCTURED_TEXT`) | 2 | `orbit-text-v1` |
| **GMI Cloud** | `seedream-5.0-pro` | Keyframe stills (`IMAGE_GENERATION`) | 4 | `orbit-image-v1` |
| **GMI Cloud** | `pixverse-v6-i2v` | Image-to-video clips (`VIDEO_GENERATION`) | 4 | `orbit-video-v1` |
| **GMI Cloud** | `kling-v3-image-to-video` | Same-provider fallback rung for video | — | `orbit-video-v1` |
| **ElevenLabs** | `eleven_multilingual_v2` | Narration TTS (voice `EXAVITQu4vr4xnSDxMaL`) | 1 | `orbit-tts-v1` |
| **ElevenLabs** | `music_v2` | Music bed (`mp3_48000_192`) | 1 | `orbit-audio-v1` |
| **Runway** | *credential-gated* | Cross-provider fallback rung for video | — | `orbit-video-v1` |

The remaining 6 of 18 nodes call no model at all — 2 resolved sources, one image
transform, and three composition nodes (end card, poster, delivery master) that
run locally on Pillow and ffmpeg. A build system's job is to know which work
needs a provider and which does not.

**The model is part of the identity of the output, not metadata about it.** Each
node's fingerprint includes `resolved_provider_policy_hash`
(`graph/fingerprint.py:69`), so changing a model ID changes the policy hash,
which changes the fingerprint, which forces a rebuild of that node and
everything downstream. You cannot silently swap models and keep the cache. Every
attempt row also persists the exact `model` that ran, which is what makes the
provenance panel — *"which model produced this frame, and did it pass review?"* —
answerable from the record instead of from memory.

**The fallback ladder is declarative, in priority order.** Video is the only
modality with a full ladder because it is the expensive, flaky one:
`pixverse-v6-i2v` → `kling-v3-image-to-video` (same provider, new model) →
Runway (different vendor, gated on `RUNWAYML_API_SECRET` being present). The
rungs are chosen by `genblaze_core`'s typed `ProviderErrorCode`, never by
string-matching an error message. In the filmed run the transient rung recovered
on attempt 2, so the cross-vendor rung is implemented and unit-tested but has not
fired against a live vendor outage — I am not claiming a failover I have not
watched happen.

**Configuration is fail-loud.** Every gateway resolves its credentials and model
through a `from_env` that raises `FeatureNotConfiguredError` naming the missing
variable. A missing key never degrades into a fixture that looks like a real
generation. The music gateway additionally rejects any model outside
`{music_v1, music_v2}` at construction, because the pinned SDK supports no others.

*Separate from the product:* the demo film itself is generated, not screen-recorded
— Puppeteer drives the live application, ElevenLabs `eleven_v3` narrates over the
`/v1/music` endpoint, and ffmpeg assembles.

---

## B2 and Genblaze usage

Both are load-bearing, and they interlock: **Genblaze generates the bytes and
lands them in B2 under a content-addressed key; B2 is what later proves those
bytes are still the same bytes.** Take either one out and incremental rebuild
stops being provable.

### Backblaze B2 — the mechanism, not the destination

I went in thinking B2 was where the files go. Building the reuse proof changed
that. Because keys **are** hashes, *"is this the same work?"* and *"are these the
same bytes?"* collapse into one question — and that is the whole product.

Every asset lands at `tenants/{org}/cas/sha256/aa/bb/{sha256}.{ext}`
(`domain/storage/keys.py:75`) through `takegraph_infrastructure.b2.B2Store`,
built on `genblaze_s3.S3StorageBackend` — one S3 client, so there is one auth
path and one retry policy to reason about. The governing rule is that
`assets.sha256` is the hash of the bytes **we** stored, never a provider's claim:
`store_bytes` hashes what it was handed, `verify` re-reads and re-hashes, and
nothing in the module takes a caller's word about content.

That buys three things the build system cannot work without:

- **Dedupe is free and automatic.** Two builds producing identical bytes converge
  on one object, and `StoredObject.deduplicated` reports it. That is what makes
  "14 nodes reused" cheap rather than a bookkeeping fiction.
- **Reuse is proven, not assumed.** Two of the reuse proof's conditions are B2
  reads: `assets_present` (*"a selected asset is no longer present in B2"*) and
  `assets_verified` (*"stored bytes no longer match the recorded SHA-256"*). A
  Postgres row asserting an asset exists is never trusted on its own.
- **A release is verifiable by a third party.** The manifest is a list of hashes;
  anyone can re-derive them from the bucket.

The integration goes well past `put_object`:

- **Two buckets, two least-privilege keys** — a work bucket for build artifacts, a
  release bucket for published masters, each with its own scoped application key.
  A test asserts the work key is *refused* against the release bucket. The master
  key is used exactly once as a setup key, and `scripts/b2_setup.py` **raises**
  rather than ever minting a key carrying `bypassGovernance`.
- **HMAC-signed B2 Event Notifications.** The webhook verifies the HMAC over the
  exact raw body *before* parsing JSON, persists and deduplicates the message, and
  queues only a reference — no media work runs in the request, which keeps the
  acknowledgement inside Backblaze's three-second timeout. A periodic reconciler
  sweeps up object-created events missed while the worker was down.
- **Presigned PUT/GET only**, generated on demand after authorization, 900-second
  TTL, never persisted.
- **Object Lock retention is read back from B2** and reported honestly as
  `NOT_CONFIGURED` rather than assumed.
- **Live release verification, open to guests.** Hitting *Verify* does not read a
  flag — it re-downloads every byte from B2 and re-hashes it. 8 assets in 6.7s
  (78s before the checks were parallelised). A proof only the author can run is
  worth very little.
- **A content-addressed WebP poster cache.** Eighteen storyboard tiles were
  pointing browsers at full-resolution originals — 18 Class B transactions *per
  page view*, uncacheable because a presigned URL carries a fresh signature. Since
  a thumbnail derives from immutable bytes, it is keyed by the source asset's
  SHA-256 and never invalidated: first request costs one B2 read, every request
  after costs none. 14s → 1.16s, 18 requests → 2, ~6 MB → ~174 KB.

### Genblaze — the generation layer and the typed contracts

Four pinned packages: `genblaze-core 0.3.8`, `genblaze-s3 0.3.6`,
`genblaze-gmicloud 0.3.5`, `genblaze-elevenlabs 0.3.3` (all `<0.4`).

- **Execution.** `Pipeline`, `RunBuilder`, `Run`, `Step`, `Manifest`, `Asset`,
  `RunnableConfig`, the `Modality` / `RunStatus` / `StepStatus` /
  `PromptVisibility` enums, and the `observability.events` stream that the
  workspace renders live.
- **The B2 seam.** `ObjectStorageSink` + `KeyStrategy.CONTENT_ADDRESSABLE` is
  exactly where generation output becomes a content-addressed object. That single
  line in the GMI and ElevenLabs gateways is what wires the SDK's storage
  abstraction into the key scheme the whole build system depends on.
- **Provider adapters.** `GMICloudImageProvider` and `GMICloudVideoProvider` from
  `genblaze_gmicloud`; `ElevenLabsTTSProvider` from `genblaze_elevenlabs`.
- **Typed failure.** `ProviderErrorCode`, `ProviderError`, `PipelineError` and
  `StorageError` are what the recovery ladder keys off. Every retry, fallback and
  terminal decision is made on an enum — never on the text of an error message.
  That is the difference between self-healing and guessing.
- **Canonicalisation, cross-checked.** The pure domain implements JCS (RFC 8785)
  itself so it can keep its zero-I/O boundary, and a conformance test asserts its
  output matches `genblaze_core.canonical.json` byte-for-byte. The domain stays
  dependency-free *and* provably agrees with the SDK.

One thing worth passing on: `genblaze_core` has zero top-level exports, so
everything imports from its real module paths (`genblaze_core.models.enums`,
`genblaze_core.storage.sink`, …). Introspecting the installed package on day one
instead of guessing from docs saved a day of wrong assumptions.
