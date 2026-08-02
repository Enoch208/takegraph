"""ORBIT policy definitions are complete, deterministic fingerprint inputs."""

from takegraph_domain.graph.orbit import REFERENCED_POLICIES
from takegraph_domain.graph.orbit_policies import (
    ORBIT_PROVIDER_POLICIES,
    ORBIT_VALIDATION_POLICIES,
    orbit_policy_hashes,
)


def test_every_orbit_policy_reference_resolves_once() -> None:
    all_definitions = ORBIT_PROVIDER_POLICIES | ORBIT_VALIDATION_POLICIES
    assert set(all_definitions) == set(REFERENCED_POLICIES)
    assert set(ORBIT_PROVIDER_POLICIES).isdisjoint(ORBIT_VALIDATION_POLICIES)


def test_policy_hashes_are_stable_and_sha256() -> None:
    first = orbit_policy_hashes()
    second = orbit_policy_hashes()
    assert first == second
    assert all(len(value) == 64 for value in first.values())


def test_policy_definitions_never_embed_resolved_credentials_or_models() -> None:
    for definition in ORBIT_PROVIDER_POLICIES.values():
        primary = definition["primary"]
        assert isinstance(primary, dict)
        model = primary["model"]
        assert isinstance(model, str)
        assert model.startswith("${") and model.endswith("}")
