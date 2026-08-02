"""Canonicalization is load-bearing: every fingerprint, graph hash and plan hash
is SHA-256 over its output. A silent change here invalidates all stored reuse
evidence, so these tests pin both the RFC 8785 rules and agreement with Genblaze.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from takegraph_domain.canonical import (
    CanonicalizationError,
    canonical_hash,
    canonical_json,
    canonical_payload,
)


class TestJcsRules:
    def test_object_keys_are_sorted(self) -> None:
        assert canonical_json({"b": 1, "a": 2, "C": 3}) == '{"C":3,"a":2,"b":1}'

    def test_array_order_is_preserved(self) -> None:
        # PRD §9.4: "Do not sort arrays whose order is semantically meaningful."
        # Input slot ordering is meaningful, so arrays must never be reordered.
        assert canonical_json([3, 1, 2]) == "[3,1,2]"

    def test_no_insignificant_whitespace(self) -> None:
        assert canonical_json({"a": [1, {"b": 2}]}) == '{"a":[1,{"b":2}]}'

    def test_strings_are_nfc_normalized(self) -> None:
        decomposed = "Amélie"  # e + combining acute
        precomposed = "Amélie"  # precomposed e-acute
        assert decomposed != precomposed
        assert canonical_json(decomposed) == canonical_json(precomposed)
        assert canonical_hash({"n": decomposed}) == canonical_hash({"n": precomposed})

    def test_non_ascii_is_not_escaped(self) -> None:
        assert canonical_json("Amélie") == '"Amélie"'

    def test_control_characters_use_rfc8785_escapes(self) -> None:
        assert canonical_json("a\nb\tcd") == '"a\\nb\\tc\\u0001d"'

    def test_quote_and_backslash_are_escaped(self) -> None:
        assert canonical_json('a"b\\c') == '"a\\"b\\\\c"'

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(1.0, "1"), (-0.0, "0"), (1.5, "1.5"), (100.0, "100")],
    )
    def test_integral_floats_render_as_integers(self, value: float, expected: str) -> None:
        # ES6 Number::toString, which RFC 8785 mandates. Genblaze renders 1.0 as
        # "1.0" instead (AGENTS.md gotcha #4); canonical_payload() forbids floats
        # in hashed payloads so the two can never disagree in practice.
        assert canonical_json(value) == expected

    def test_non_finite_numbers_are_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(CanonicalizationError):
                canonical_json(bad)

    def test_booleans_are_not_treated_as_integers(self) -> None:
        assert canonical_json({"a": True, "b": 1}) == '{"a":true,"b":1}'

    def test_keys_sort_by_utf16_code_unit_not_code_point(self) -> None:
        # The two orders disagree above the BMP, which is the whole reason JCS
        # specifies UTF-16:
        #   U+FFFF  -> code units [FFFF]
        #   U+10000 -> code units [D800, DC00]   (surrogate pair)
        # UTF-16 compares D800 < FFFF, so U+10000 sorts first. By code point,
        # 0xFFFF < 0x10000, so U+FFFF would sort first. A naive sorted(keys)
        # gives the wrong one, and this test fails on it.
        out = canonical_json({"￿": 2, "\U00010000": 1})
        assert out == '{"\U00010000":1,"￿":2}'
        assert out.index("\U00010000") < out.index("￿")


class TestDeterminism:
    def test_insertion_order_does_not_affect_output(self) -> None:
        # PRD §12.1: "Compiler output must be independent of database insertion order."
        a = {"z": 1, "m": {"y": 2, "x": 3}, "a": [1, 2]}
        b = {"a": [1, 2], "m": {"x": 3, "y": 2}, "z": 1}
        assert canonical_json(a) == canonical_json(b)
        assert canonical_hash(a) == canonical_hash(b)

    def test_hash_is_stable_across_calls(self) -> None:
        payload = {"stable_key": "copy.pack", "inputs": [{"slot": "brief", "sha256": "ab" * 32}]}
        assert canonical_hash(payload) == canonical_hash(payload)

    @given(
        st.recursive(
            st.none()
            | st.booleans()
            | st.integers(min_value=-(2**53), max_value=2**53)
            | st.text(),
            lambda children: (
                st.lists(children, max_size=4)
                | st.dictionaries(st.text(max_size=8), children, max_size=4)
            ),
            max_leaves=12,
        )
    )
    def test_canonicalization_is_a_function_of_value_alone(self, value: object) -> None:
        import copy as copy_module

        assert canonical_json(value) == canonical_json(copy_module.deepcopy(value))


class TestPayloadGuard:
    def test_floats_are_rejected_with_a_useful_path(self) -> None:
        with pytest.raises(CanonicalizationError, match=r"budgets\.max_cost"):
            canonical_payload({"budgets": {"max_cost": 5.0}})

    def test_floats_nested_in_arrays_are_rejected(self) -> None:
        with pytest.raises(CanonicalizationError, match=r"scores\[1\]"):
            canonical_payload({"scores": ["0.80", 0.60]})

    def test_decimal_strings_and_ints_pass(self) -> None:
        payload = {"cost_usd": "5.000000", "attempts": 4, "required": True, "cost": None}
        assert canonical_payload(payload) is payload


class TestGenblazeConformance:
    """PRD §9.4 wants one canonicalization across the system. Our fingerprints and
    Genblaze's manifest canonical_hash must agree byte for byte, or provenance
    comparisons are meaningless. This is the drift alarm on SDK upgrades."""

    CORPUS: list[object] = [
        {},
        {"a": 1},
        {"b": 1, "a": 2, "C": 3},
        {"nested": {"z": [1, 2, {"k": "v"}], "a": None}},
        {"unicode": "Amélie", "emoji": "🎬"},
        {"escapes": 'quote " backslash \\ newline \n'},
        {"ordered": [3, 1, 2]},
        {"bools": [True, False], "null": None},
        {"sha256": "ab" * 32, "cost_usd": "5.000000"},
        {
            "schema_version": "1",
            "node_type": "VIDEO_GENERATION",
            "ordered_inputs": [
                {"slot": "shot_plan", "selected_asset_sha256": "cd" * 32},
                {"slot": "keyframe", "selected_asset_sha256": "ef" * 32},
            ],
            "generator_code_version": "git-sha",
            "template_version": "orbit-launch-v1",
        },
    ]

    def test_matches_genblaze_canonical_json(self) -> None:
        gb = pytest.importorskip("genblaze_core.canonical.json")
        mismatches = [
            (payload, canonical_json(payload), gb.canonical_json(payload))
            for payload in self.CORPUS
            if canonical_json(payload) != gb.canonical_json(payload)
        ]
        assert not mismatches, f"canonicalization drifted from Genblaze: {mismatches}"

    def test_matches_genblaze_canonical_hash(self) -> None:
        gb = pytest.importorskip("genblaze_core.canonical.json")
        for payload in self.CORPUS:
            assert canonical_hash(payload) == gb.canonical_hash(payload), payload
