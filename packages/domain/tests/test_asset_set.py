"""The selected-output identity of a build node (PRD §12.2, §12.3).

The worker writes this value when a node passes and the impact engine re-derives
it from the persisted assets when deciding reuse. If the two ever disagree the
node reports CACHE_ASSET_UNVERIFIED forever — a claim that stored bytes do not
match their hash, made about bytes that are perfectly intact.
"""

from __future__ import annotations

import pytest
from takegraph_domain.builds.asset_set import SelectedAsset, selected_asset_set_hash

MASTER = SelectedAsset(role="master_16x9", ordinal=0, sha256="a1" * 32)
AUDIO = SelectedAsset(role="final_audio", ordinal=2, sha256="b2" * 32)
REPORT = SelectedAsset(role="report", ordinal=6, sha256="c3" * 32)


class TestSingleAssetNodes:
    """Fifteen of the eighteen ORBIT nodes select exactly one asset and record its
    stored-byte SHA-256 directly. That form must survive unchanged."""

    def test_one_asset_is_identified_by_its_own_digest(self) -> None:
        assert selected_asset_set_hash([MASTER]) == MASTER.sha256

    def test_role_and_ordinal_do_not_affect_a_single_asset(self) -> None:
        moved = MASTER.model_copy(update={"role": "thumbnail_9x16", "ordinal": 4})
        assert selected_asset_set_hash([moved]) == MASTER.sha256


class TestMultiAssetNodes:
    def test_iteration_order_does_not_change_the_hash(self) -> None:
        """The worker emits artifacts in ordinal order; the impact engine reads them
        back ordered by role. Both must land on the same value."""
        by_ordinal = selected_asset_set_hash([MASTER, AUDIO, REPORT])
        by_role = selected_asset_set_hash([AUDIO, MASTER, REPORT])
        assert by_ordinal == by_role

    def test_a_changed_member_digest_changes_the_set(self) -> None:
        altered = REPORT.model_copy(update={"sha256": "d4" * 32})
        assert selected_asset_set_hash([MASTER, AUDIO, altered]) != selected_asset_set_hash(
            [MASTER, AUDIO, REPORT]
        )

    def test_a_dropped_member_changes_the_set(self) -> None:
        assert selected_asset_set_hash([MASTER, AUDIO]) != selected_asset_set_hash(
            [MASTER, AUDIO, REPORT]
        )

    def test_swapping_roles_between_digests_changes_the_set(self) -> None:
        """Role is part of the identity: the same bytes delivered as a different
        artifact is a different delivery package."""
        swapped = [
            MASTER.model_copy(update={"sha256": AUDIO.sha256}),
            AUDIO.model_copy(update={"sha256": MASTER.sha256}),
        ]
        assert selected_asset_set_hash(swapped) != selected_asset_set_hash([MASTER, AUDIO])

    def test_a_set_hash_is_not_any_member_digest(self) -> None:
        digest = selected_asset_set_hash([MASTER, AUDIO, REPORT])
        assert digest not in {MASTER.sha256, AUDIO.sha256, REPORT.sha256}


class TestEmptySelection:
    def test_no_selected_asset_fails_loudly(self) -> None:
        """§0.1 forbids a default that makes a broken node look complete."""
        with pytest.raises(ValueError, match="at least one asset"):
            selected_asset_set_hash([])
