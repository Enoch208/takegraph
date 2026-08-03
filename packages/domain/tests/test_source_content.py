"""The worker and the impact engine must agree on a source hash.

They did not, and nothing failed while that was true: the worker recorded the JCS
hash of {"brief_text": ...}, the impact engine expected the whitespace-normalised
text hash, and so a resolved source node could never satisfy the reuse proof.
Every source node reported CACHE_ASSET_MISSING, which cascaded through the whole
graph — a one-word legal-line change rebuilt sixteen of eighteen nodes instead of
four. These tests exist so that can never be reintroduced quietly.
"""

from __future__ import annotations

import pytest
from takegraph_api.changes import _brief_hash
from takegraph_domain.graph.source_content import (
    brief_hash_from_spec,
    brief_text_hash,
    normalize_source_text,
)
from takegraph_worker.source_node_work import brief_content_hash

BRIEF = "ORBIT Hydration launch. Dark graphite set, crisp white bottle."


def test_worker_and_impact_engine_agree() -> None:
    """The whole point. If this fails, incremental rebuild is broken."""
    spec = {"parameters": {"brief_text": BRIEF}}
    assert brief_content_hash(spec) == _brief_hash(spec)
    assert brief_content_hash(spec) == brief_hash_from_spec(spec)


def test_reflowing_whitespace_is_not_a_content_change() -> None:
    """Rewrapping a brief must not cost a rebuild of every downstream asset."""
    assert brief_text_hash("one two  three") == brief_text_hash("one   two\n\tthree")
    assert brief_text_hash(" leading and trailing ") == brief_text_hash("leading and trailing")


def test_unicode_is_normalised_before_hashing() -> None:
    """Composed and decomposed forms read identically and must hash identically."""
    composed = "café orbit"
    decomposed = "café orbit"
    assert composed != decomposed
    assert brief_text_hash(composed) == brief_text_hash(decomposed)


def test_real_content_changes_do_change_the_hash() -> None:
    assert brief_text_hash(BRIEF) != brief_text_hash(BRIEF + " Teal accent.")


def test_normalisation_collapses_runs() -> None:
    assert normalize_source_text("a \n  b\t c ") == "a b c"


def test_a_non_string_brief_is_refused() -> None:
    from takegraph_domain.errors import InvalidSourceError

    with pytest.raises(InvalidSourceError):
        brief_hash_from_spec({"parameters": {"brief_text": 42}})
