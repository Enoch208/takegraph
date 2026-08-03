"""The content hash of a text source node (PRD §12.2).

There is exactly one correct answer to "what is the hash of this brief", and it
has to be computed identically in two places that never see each other: the
worker, when it resolves a source node and records what it produced, and the
impact engine, when it later decides whether that recorded output can be reused.

They were computed differently. The worker hashed the JCS encoding of
``{"brief_text": ...}``; the impact engine hashed the whitespace-normalised text.
Both are defensible hashes and neither is wrong on its own, but they never agree,
so a resolved source node could never satisfy the reuse proof. Every source node
reported CACHE_ASSET_MISSING, which invalidated its dependents, which invalidated
theirs — a one-word change to the legal line rebuilt sixteen of eighteen nodes
when it should have rebuilt four. The product's central claim quietly stopped
being true, and nothing failed while it happened.

So the definition lives here, once, and both callers import it.

Normalisation is deliberate and is the reason this beats hashing the raw string:
NFC folds visually identical Unicode into one encoding, and collapsing runs of
whitespace means reflowing a brief across different line lengths is not treated
as new content. Neither changes what the brief says, and neither should cost a
rebuild of every downstream asset.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any

from takegraph_domain.errors import InvalidSourceError
from takegraph_domain.graph.orbit import DEFAULT_BRIEF_TEXT, PARAM_BRIEF_TEXT


def normalize_source_text(value: str) -> str:
    """NFC, with runs of whitespace collapsed to single spaces."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def brief_text_hash(value: str) -> str:
    """Hash a brief's text directly."""
    return hashlib.sha256(normalize_source_text(value).encode()).hexdigest()


def brief_hash_from_spec(spec: dict[str, Any]) -> str:
    """Hash the brief carried by a project revision's spec.

    Falls back to the template default when the parameter is absent, which is the
    same value the compiler binds, so an unset brief hashes to what was actually
    built rather than raising.
    """
    parameters = spec.get("parameters", {})
    value = (
        parameters.get(PARAM_BRIEF_TEXT, DEFAULT_BRIEF_TEXT)
        if isinstance(parameters, dict)
        else None
    )
    if not isinstance(value, str):
        raise InvalidSourceError("brief_text must be a string.")
    return brief_text_hash(value)


__all__ = ["brief_hash_from_spec", "brief_text_hash", "normalize_source_text"]
