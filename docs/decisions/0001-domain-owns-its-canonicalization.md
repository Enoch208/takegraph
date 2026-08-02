# ADR 0001 — The domain implements JCS rather than importing Genblaze's

Date: 2026-08-02
Status: Accepted

## Context

PRD §9.4 requires JSON Canonicalization Scheme behaviour for every hash the system
compares — node fingerprints, graph revision hashes, plan hashes.

`genblaze_core.canonical.json` ships a working JCS implementation, and it is the
same function that produces the canonical hash embedded in Genblaze manifests.
Importing it would guarantee that TAKEGRAPH fingerprints and Genblaze manifest
hashes agree, with no second implementation to maintain.

Against that, PRD §7.1 states: "Domain packages do not import FastAPI, React, or
concrete provider SDKs", and §14.2: "Domain code must not import provider-specific
classes." `genblaze-core` is the orchestration SDK rather than a provider adapter,
so the rule's application is arguable — but the intent is clear: the domain should
be installable and testable with nothing but its own dependencies.

## Decision

`takegraph_domain.canonical` implements JCS directly, depending on nothing beyond
the standard library. `packages/domain/tests/test_canonical.py::TestGenblazeConformance`
asserts byte-identical `canonical_json` and `canonical_hash` output against
`genblaze_core.canonical.json` over a payload corpus.

## Consequences

- The domain package keeps a single runtime dependency (pydantic), so graph and
  impact logic is testable without provider packages installed.
- Drift between the two implementations fails a test rather than silently
  invalidating stored reuse evidence. This is the point: if a Genblaze upgrade
  changed canonicalization, every persisted fingerprint would quietly stop matching,
  and reuse would collapse to zero with no error anywhere.
- We control number rendering. Genblaze renders `1.0` as `"1.0"`; RFC 8785 mandates
  ECMAScript `Number::toString`, which gives `"1"`. Ours is correct per the RFC.
  `canonical_payload()` rejects floats in hashed payloads outright, so the two
  implementations cannot disagree in practice — decimals travel as strings
  (`"5.000000"`, matching §8.1's `numeric(14,6)` and §9.2's cost fields) and counts
  as integers.
- Cost: roughly 90 lines to maintain, plus the conformance corpus.

## Alternatives rejected

**Import `genblaze_core.canonical.json` into the domain.** One implementation, but
couples the domain to the SDK and inherits the float deviation with no way to fix it.

**Implement JCS with no conformance test.** Cheaper, but the failure it invites —
fingerprints that no longer match manifests — is silent and expensive.
