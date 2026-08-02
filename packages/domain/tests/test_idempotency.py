"""Idempotency key tests — PRD §13.2 and §22.3.

Two failure modes, both expensive and neither loud:

- Too *narrow* a key: a crashed worker resubmits and the account is billed twice
  for the same clip. §20.1 targets zero duplicate billable submissions.
- Too *broad* a key: a legitimate fallback or retake collides with the primary
  submission and is dropped, so a recoverable build stalls with no error.

Every test below pins one side or the other.
"""

from __future__ import annotations

import uuid

import pytest
from takegraph_domain.enums import AttemptMechanism
from takegraph_domain.execution.idempotency import (
    submission_idempotency_key,
    work_item_dedupe_key,
)

NODE = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_NODE = uuid.UUID("22222222-2222-2222-2222-222222222222")
FP = "ab" * 32


def key(**overrides) -> str:
    params = {
        "build_node_id": NODE,
        "fingerprint": FP,
        "mechanism": AttemptMechanism.PRIMARY,
        "provider": "gmicloud",
        "model": "video-v1",
        "logical_attempt_slot": 0,
    }
    return submission_idempotency_key(**{**params, **overrides})


class TestStability:
    """The same logical submission must key identically, or crash recovery
    resubmits and pays twice."""

    def test_identical_inputs_give_an_identical_key(self) -> None:
        assert key() == key()

    def test_key_is_a_sha256_hex_digest(self) -> None:
        result = key()
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_uuid_and_its_string_form_agree(self) -> None:
        """The worker may hold a UUID while a replayed payload holds a string. If
        those keyed differently, reconciliation would resubmit."""
        assert key(build_node_id=NODE) == key(build_node_id=str(NODE))

    def test_attempt_number_is_not_part_of_the_key(self) -> None:
        """§13.2 lists a logical attempt *slot*, not the attempt number. Keying on
        the row's attempt_no would make every retry unique and defeat the whole
        mechanism."""
        assert key() == key()


class TestDiscrimination:
    """Genuinely different billable work must key differently."""

    def test_different_node_differs(self) -> None:
        assert key(build_node_id=NODE) != key(build_node_id=OTHER_NODE)

    def test_different_fingerprint_differs(self) -> None:
        """A changed recipe or input is different work even on the same node."""
        assert key(fingerprint=FP) != key(fingerprint="cd" * 32)

    @pytest.mark.parametrize(
        "mechanism",
        [
            AttemptMechanism.SAME_PROVIDER_RETRY,
            AttemptMechanism.SAME_PROVIDER_MODEL_FALLBACK,
            AttemptMechanism.CROSS_PROVIDER_FALLBACK,
            AttemptMechanism.REPAIR_RETAKE,
            AttemptMechanism.MANUAL_RETRY,
        ],
    )
    def test_each_mechanism_differs_from_primary(self, mechanism: AttemptMechanism) -> None:
        """§5.5 FR-PROV-002 requires the attempt to identify the exact mechanism.
        A retake that collided with its primary would never be submitted."""
        assert key(mechanism=mechanism) != key(mechanism=AttemptMechanism.PRIMARY)

    def test_cross_provider_fallback_differs_from_primary_by_provider(self) -> None:
        """Routing to a second provider genuinely bills separately, so it must not
        look like a duplicate of the primary call."""
        assert key(provider="runway", model="gen-3") != key(provider="gmicloud", model="video-v1")

    def test_model_fallback_within_a_provider_differs(self) -> None:
        assert key(model="video-v1") != key(model="video-v1-fallback")

    def test_new_logical_slot_permits_a_deliberate_resubmission(self) -> None:
        """§13.2: after AMBIGUOUS_SUBMISSION an authorized user may retry with an
        explicit new slot. That has to produce a fresh key or the retry is a no-op."""
        assert key(logical_attempt_slot=0) != key(logical_attempt_slot=1)


class TestNoFieldCollisions:
    """The key is canonicalised, not concatenated. Raw concatenation would let
    field boundaries shift and two different submissions collide."""

    def test_shifting_a_boundary_between_provider_and_model_does_not_collide(self) -> None:
        assert key(provider="gmi", model="cloud-v1") != key(provider="gmicloud", model="-v1")

    def test_shifting_a_boundary_in_the_fingerprint_does_not_collide(self) -> None:
        a = submission_idempotency_key(
            build_node_id=NODE,
            fingerprint="aabb",
            mechanism=AttemptMechanism.PRIMARY,
            provider="p",
            model="m",
        )
        b = submission_idempotency_key(
            build_node_id=NODE,
            fingerprint="aab",
            mechanism=AttemptMechanism.PRIMARY,
            provider="bp",
            model="m",
        )
        assert a != b


class TestValidation:
    """These raise instead of producing a weak key, because a weak key fails
    silently and expensively."""

    def test_empty_fingerprint_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="fingerprint"):
            key(fingerprint="")

    def test_missing_provider_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider and model"):
            key(provider="")

    def test_missing_model_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider and model"):
            key(model="")

    def test_negative_slot_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            key(logical_attempt_slot=-1)


class TestWorkItemDedupeKey:
    """Deliberately distinct from the submission key: a queue item is 'work to
    consider', a submission key is 'a call I may already have paid for'."""

    def test_same_kind_and_target_dedupe(self) -> None:
        assert work_item_dedupe_key(kind="execute_node", target_id=NODE) == work_item_dedupe_key(
            kind="execute_node", target_id=NODE
        )

    def test_different_kinds_do_not_dedupe(self) -> None:
        """A node can legitimately have both an execution and a validation job."""
        assert work_item_dedupe_key(kind="execute_node", target_id=NODE) != work_item_dedupe_key(
            kind="validate_node", target_id=NODE
        )

    def test_discriminator_separates_repeat_work(self) -> None:
        assert work_item_dedupe_key(
            kind="execute_node", target_id=NODE, discriminator="attempt-2"
        ) != work_item_dedupe_key(kind="execute_node", target_id=NODE)

    def test_kind_is_required(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            work_item_dedupe_key(kind="", target_id=NODE)
