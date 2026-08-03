"""The selected-output identity of a build node (PRD §12.2, §12.3).

A node whose attempt selects exactly one asset is identified by that asset's
stored-byte SHA-256. A node that selects several — the delivery package selects
seven — needs a single value standing for the whole set, and the worker that
writes it and the impact engine that later re-derives it must agree byte for
byte or legitimate reuse silently degrades into a rebuild.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict

from takegraph_domain.canonical import JsonValue, canonical_hash


class SelectedAsset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    ordinal: int
    sha256: str


def selected_asset_set_hash(assets: Iterable[SelectedAsset]) -> str:
    ordered: Sequence[SelectedAsset] = sorted(assets, key=lambda item: (item.role, item.ordinal))
    if not ordered:
        raise ValueError("A selected asset set must contain at least one asset.")
    if len(ordered) == 1:
        return ordered[0].sha256
    payload: JsonValue = [
        {"role": item.role, "ordinal": item.ordinal, "sha256": item.sha256} for item in ordered
    ]
    return canonical_hash(payload)
