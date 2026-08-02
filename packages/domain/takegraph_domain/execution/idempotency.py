"""External-submission idempotency (PRD §13.2).

The key answers one question: "is this submission the same billable work as one I
may already have sent?" Get it wrong in one direction and a crashed worker
double-charges the account; wrong in the other and a legitimate retake is
silently rejected as a duplicate.

§13.2 defines it as:

    SHA256(build_node_id + fingerprint + mechanism + provider + model
           + logical_attempt_slot)

Every component earns its place:

- `build_node_id` scopes the key to one node in one build.
- `fingerprint` means a changed recipe or input is different work.
- `mechanism` separates a primary submission from a retry, a model fallback, a
  cross-provider fallback and a repair retake — §5.5 FR-PROV-002 requires the
  attempt record to identify which mechanism ran.
- `provider` and `model` mean routing to a fallback is a distinct submission,
  because it genuinely bills separately.
- `logical_attempt_slot` is what lets an authorized user deliberately re-run
  after an ambiguous submission (§13.2) without colliding with the earlier key.

Note what is absent: attempt number, timestamps, and any database row id other
than the node. Including those would make every key unique and defeat the point.
"""

from __future__ import annotations

import uuid

from takegraph_domain.canonical import canonical_hash
from takegraph_domain.enums import AttemptMechanism


def submission_idempotency_key(
    *,
    build_node_id: uuid.UUID | str,
    fingerprint: str,
    mechanism: AttemptMechanism,
    provider: str,
    model: str,
    logical_attempt_slot: int = 0,
) -> str:
    """Derive the key for one external submission.

    Canonicalised rather than concatenated: a raw `a + b` join would let
    ("ab", "c") and ("a", "bc") collide, and a collision here means two different
    submissions share a key and one is silently dropped.
    """
    if not fingerprint:
        raise ValueError("fingerprint is required; an empty one would collide across nodes")
    if not provider or not model:
        raise ValueError(
            "provider and model are required; a fallback must key differently "
            "from the primary submission or it will look like a duplicate"
        )
    if logical_attempt_slot < 0:
        raise ValueError("logical_attempt_slot must be non-negative")

    return canonical_hash(
        {
            "build_node_id": str(build_node_id),
            "fingerprint": fingerprint,
            "mechanism": str(mechanism),
            "provider": provider,
            "model": model,
            "logical_attempt_slot": logical_attempt_slot,
        }
    )


def work_item_dedupe_key(*, kind: str, target_id: uuid.UUID | str, discriminator: str = "") -> str:
    """Dedupe key for the durable queue (§5.9 FR-EVT-003).

    Distinct from the submission key on purpose. A queue item is "work to
    consider doing", which may be enqueued by a B2 event, an application commit
    and a reconciliation pass all at once; the submission key is "an external
    call I may already have paid for". Conflating them would either drop
    legitimate re-scheduling or permit duplicate billing.
    """
    if not kind:
        raise ValueError("kind is required")
    parts = [kind, str(target_id)]
    if discriminator:
        parts.append(discriminator)
    return ":".join(parts)
