"""Pytest fixtures for domain tests. Builders live in orbit_fixtures.py."""

from __future__ import annotations

import pytest
from orbit_fixtures import completed_build_states, orbit_graph
from takegraph_domain.graph.types import CompiledGraph, NodeCacheState


@pytest.fixture
def baseline_graph() -> CompiledGraph:
    """ORBIT v1 as published, with the original `zero sugar` legal line."""
    return orbit_graph(legal_line="zero sugar")


@pytest.fixture
def baseline_states(baseline_graph: CompiledGraph) -> dict[str, NodeCacheState]:
    return completed_build_states(baseline_graph)
