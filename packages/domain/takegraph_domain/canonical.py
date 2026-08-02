"""Canonical JSON serialization and hashing (PRD §9.4).

Every hash the system compares — node fingerprints, graph revision hashes, plan
hashes — is SHA-256 over JCS-canonicalized JSON. JCS (RFC 8785) gives us: UTF-8,
NFC-normalized strings, object keys sorted by UTF-16 code unit, stable number
serialization, and preserved array order.

Why implement it here rather than import genblaze_core.canonical.json:
PRD §7.1 forbids the domain package from importing provider SDKs, and the domain
must stay installable with pydantic alone. The cost of a second implementation is
drift, so tests/test_canonical.py asserts byte-identical output against Genblaze's
JCS over a payload corpus. If an SDK upgrade changes canonicalization, that test
fails loudly rather than silently invalidating every stored fingerprint.

See AGENTS.md gotcha #4: Genblaze's implementation renders 1.0 as "1.0" where
RFC 8785 mandates "1". This module is correct per the RFC. Fingerprint payloads
must therefore never carry floats — decimals travel as strings ("5.000000"),
counts as ints — which keeps the two implementations byte-identical in practice
and is enforced by canonical_payload().
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class CanonicalizationError(ValueError):
    """A value cannot be canonicalized deterministically."""


def _serialize_string(value: str) -> str:
    """Escape per RFC 8785 §3.2.2.2.

    json.dumps with ensure_ascii=False produces exactly the required form: only
    '"', '\\' and control characters below 0x20 are escaped, with the short forms
    \\b \\t \\n \\f \\r preferred and \\u00xx otherwise. Non-ASCII stays literal.
    """
    return json.dumps(unicodedata.normalize("NFC", value), ensure_ascii=False)


def _serialize_number(value: int | float) -> str:
    """ECMAScript Number::toString, which RFC 8785 mandates."""
    if isinstance(value, bool):  # bool is an int subclass; caught earlier, belt and braces
        raise CanonicalizationError("bool reached number serialization")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise CanonicalizationError(f"non-finite number is not representable in JSON: {value!r}")
    # ES6 renders integral doubles without a fractional part: 1.0 -> "1"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    # repr() gives the shortest string that round-trips, matching ES6 for the
    # range this system uses. Exponent formatting diverges for extreme
    # magnitudes; canonical_payload() rejects floats outright, so it cannot bite.
    return repr(value)


def _utf16_sort_key(key: str) -> bytes:
    """JCS sorts keys by UTF-16 code unit, which differs from Python's code-point
    ordering above the BMP. Comparing UTF-16BE bytes reproduces it exactly."""
    return key.encode("utf-16-be")


def _serialize(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _serialize_number(value)
    if isinstance(value, str):
        return _serialize_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_serialize(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value, key=_utf16_sort_key):
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"object keys must be strings, got {type(key).__name__}"
                )
            parts.append(f"{_serialize_string(key)}:{_serialize(value[key])}")
        return "{" + ",".join(parts) + "}"
    raise CanonicalizationError(f"type is not JSON-canonicalizable: {type(value).__name__}")


def canonical_json(value: JsonValue) -> str:
    """Canonicalize to a JCS string. Deterministic for equal inputs, by construction."""
    return _serialize(value)


def canonical_bytes(value: JsonValue) -> bytes:
    return canonical_json(value).encode("utf-8")


def canonical_hash(value: JsonValue) -> str:
    """SHA-256 hex of the canonical form. This is what fingerprints and plan hashes are."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def canonical_payload(value: JsonValue) -> JsonValue:
    """Validate a payload is safe to hash, returning it unchanged.

    Rejects floats. PRD §8.1 already requires money as numeric(14,6) and §9.2
    carries costs as strings; scores and thresholds in §9.3 are strings too. A
    float in a fingerprint would make the hash depend on binary rounding and on
    which JCS implementation rendered it — so it is a bug, and this raises rather
    than silently hashing something unstable.
    """

    def check(node: JsonValue, path: str) -> None:
        if isinstance(node, bool) or node is None or isinstance(node, (int, str)):
            return
        if isinstance(node, float):
            raise CanonicalizationError(
                f"float at {path or '<root>'} is not permitted in a hashed payload; "
                "carry decimals as strings (e.g. '5.000000') so the hash is stable"
            )
        if isinstance(node, list):
            for index, item in enumerate(node):
                check(item, f"{path}[{index}]")
            return
        if isinstance(node, dict):
            for key, item in node.items():
                check(item, f"{path}.{key}" if path else key)
            return
        raise CanonicalizationError(
            f"type at {path or '<root>'} is not JSON: {type(node).__name__}"
        )

    check(value, "")
    return value
