"""Graph template and compiled-graph value objects (PRD §9.1, §12.1).

A template is authored once and versioned. Compiling a template against a project
revision produces an immutable CompiledGraph whose canonical hash is independent
of insertion order (§12.1) — two compilations of the same template, revision,
policies and code version produce identical bytes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from takegraph_domain.canonical import JsonValue, canonical_hash
from takegraph_domain.enums import BuildNodeStatus, NodeType

SCHEMA_VERSION = "1"


class InputSlot(BaseModel):
    """One declared dependency edge. §9.1 `inputs`.

    Slot ordering is semantically meaningful and is preserved into the fingerprint
    (§9.4 "preserved array order"), so these are never sorted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: str
    from_key: str
    asset_role: str = "primary"
    ordinal: int = 0


class ParameterBinding(BaseModel):
    """Allow-listed mapping from a project-revision parameter into a node operation.

    PRD §12.1 step 1: "Resolve template parameters from the revision through an
    allow-listed mapping." This is that allow-list, and it is load-bearing for
    AS-01. The legal copy line binds into `copy.pack` and nowhere else, so editing
    it changes exactly one node's operation and invalidates exactly that node's
    descendants. Were the same text folded into `source.brief`, `plan.shots` would
    change too and the cascade would reach all 18 nodes. See
    docs/decisions/0002-legal-line-is-a-bound-parameter.md.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_key: str
    """Key written into the node's normalized operation."""

    parameter: str
    """Name of the parameter read from the project revision's `parameters` map."""


class TemplateNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stable_key: str
    node_type: NodeType
    required: bool = True
    inputs: tuple[InputSlot, ...] = ()
    operation: dict[str, JsonValue] = Field(default_factory=dict)
    parameter_bindings: tuple[ParameterBinding, ...] = ()
    provider_policy: str | None = None
    validation_policy: str | None = None
    output_roles: tuple[str, ...] = ("primary",)
    label: str = ""
    """Human-readable name for storyboard cards (§18.7)."""


class GraphTemplate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    version: int
    schema_version: str = SCHEMA_VERSION
    nodes: tuple[TemplateNode, ...]

    @property
    def version_label(self) -> str:
        return f"{self.key}-v{self.version}"


class CompiledNode(BaseModel):
    """A node frozen against one project revision. Immutable after compilation (§8.3.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stable_key: str
    node_type: NodeType
    required: bool
    inputs: tuple[InputSlot, ...]
    normalized_operation: dict[str, JsonValue]
    provider_policy_hash: str | None
    validation_policy_hash: str | None
    output_roles: tuple[str, ...]
    label: str

    @property
    def spec_hash(self) -> str:
        return canonical_hash(
            {
                "schema_version": SCHEMA_VERSION,
                "stable_key": self.stable_key,
                "node_type": str(self.node_type),
                "required": self.required,
                "inputs": [
                    {
                        "slot": i.slot,
                        "from": i.from_key,
                        "asset_role": i.asset_role,
                        "ordinal": i.ordinal,
                    }
                    for i in self.inputs
                ],
                "normalized_operation": self.normalized_operation,
                "provider_policy_hash": self.provider_policy_hash,
                "validation_policy_hash": self.validation_policy_hash,
                "output_roles": list(self.output_roles),
            }
        )


class CompiledGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    template_key: str
    template_version: int
    compiler_version: str
    nodes: tuple[CompiledNode, ...]
    topological_order: tuple[str, ...]
    canonical_hash: str

    @property
    def by_key(self) -> dict[str, CompiledNode]:
        return {node.stable_key: node for node in self.nodes}

    @property
    def template_version_label(self) -> str:
        return f"{self.template_key}-v{self.template_version}"

    def dependents_of(self, stable_key: str) -> set[str]:
        """Direct dependents. Transitive closure is what impact analysis walks."""
        return {n.stable_key for n in self.nodes if any(i.from_key == stable_key for i in n.inputs)}


class NodeCacheState(BaseModel):
    """What a previous build knows about one node, for the reuse proof (§12.3).

    Supplied by the persistence layer. The domain never queries B2 or the database
    itself; `assets_present` / `assets_verified` are answers handed to it, which is
    what keeps §7.1's boundary rule intact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stable_key: str
    fingerprint: str
    status: BuildNodeStatus
    selected_output_hash: str | None = None
    """SHA-256 of the selected asset, or a structured content hash for non-media nodes."""

    validations_current: bool = True
    """Required gates are current for the node's validation policy version and accepted."""

    assets_present: bool = True
    """Every selected asset still exists in B2."""

    assets_verified: bool = True
    """Stored bytes still match the recorded SHA-256 where the policy demands re-verification."""

    manually_approved: bool = False
    is_fixture: bool = False
    revoked: bool = False
    source_build_node_id: str | None = None
    validation_ids: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()
