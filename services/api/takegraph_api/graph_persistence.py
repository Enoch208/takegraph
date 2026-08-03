"""Persist deterministic compiled graph snapshots and immutable policy versions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_domain.canonical import JsonValue, canonical_hash, canonical_payload
from takegraph_domain.errors import InvalidSourceError
from takegraph_domain.graph.compiler import compile_graph
from takegraph_domain.graph.orbit import (
    DEFAULT_BRIEF_TEXT,
    DEFAULT_LEGAL_LINE,
    ORBIT_TEMPLATE,
    PARAM_BRIEF_TEXT,
    PARAM_LEGAL_LINE,
)
from takegraph_domain.graph.orbit_policies import (
    ORBIT_PROVIDER_POLICIES,
    ORBIT_VALIDATION_POLICIES,
    orbit_policy_hashes,
)

from takegraph_api.db.models import (
    GraphEdge,
    GraphNode,
    GraphRevision,
    GraphTemplateRow,
    ProjectRevision,
    ProviderPolicy,
    ValidationPolicy,
)

ORBIT_CATALOG_LOCK_ID = 1_416_488_084


@dataclass(frozen=True, slots=True)
class PersistedGraph:
    id: uuid.UUID
    canonical_hash: str
    node_count: int
    edge_count: int


class OrbitGraphRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def compile_revision(self, project_revision_id: uuid.UUID) -> PersistedGraph:
        revision = await self._session.get(ProjectRevision, project_revision_id)
        if revision is None:
            raise InvalidSourceError("Project revision cannot be compiled because it is missing.")

        template_id, provider_ids, validation_ids = await self._ensure_catalog()
        graph = compile_graph(
            ORBIT_TEMPLATE,
            parameters=_parameters(revision.spec_json),
            policy_hashes=orbit_policy_hashes(),
        )
        existing = await self._session.scalar(
            select(GraphRevision).where(GraphRevision.project_revision_id == project_revision_id)
        )
        if existing is not None:
            if (
                existing.canonical_hash != graph.canonical_hash
                or existing.compiler_version != graph.compiler_version
            ):
                raise InvalidSourceError(
                    "Persisted graph revision conflicts with deterministic compilation."
                )
            node_count = (
                await self._session.scalar(
                    select(func.count(GraphNode.id)).where(
                        GraphNode.graph_revision_id == existing.id
                    )
                )
                or 0
            )
            edge_count = (
                await self._session.scalar(
                    select(func.count(GraphEdge.id)).where(
                        GraphEdge.graph_revision_id == existing.id
                    )
                )
                or 0
            )
            return PersistedGraph(existing.id, existing.canonical_hash, node_count, edge_count)

        graph_revision_id = uuid.uuid4()
        self._session.add(
            GraphRevision(
                id=graph_revision_id,
                project_revision_id=project_revision_id,
                template_id=template_id,
                canonical_hash=graph.canonical_hash,
                compiler_version=graph.compiler_version,
            )
        )
        await self._session.flush()

        template_nodes = {node.stable_key: node for node in ORBIT_TEMPLATE.nodes}
        node_ids = {node.stable_key: uuid.uuid4() for node in graph.nodes}
        for node in graph.nodes:
            source = template_nodes[node.stable_key]
            self._session.add(
                GraphNode(
                    id=node_ids[node.stable_key],
                    graph_revision_id=graph_revision_id,
                    stable_key=node.stable_key,
                    node_type=str(node.node_type),
                    spec_json=node.model_dump(mode="json"),
                    spec_hash=node.spec_hash,
                    provider_policy_id=(
                        None
                        if source.provider_policy is None
                        else provider_ids[source.provider_policy]
                    ),
                    validation_policy_id=(
                        None
                        if source.validation_policy is None
                        else validation_ids[source.validation_policy]
                    ),
                    required=node.required,
                    label=node.label,
                )
            )
        await self._session.flush()

        edge_count = 0
        for node in graph.nodes:
            for slot in node.inputs:
                self._session.add(
                    GraphEdge(
                        id=uuid.uuid4(),
                        graph_revision_id=graph_revision_id,
                        from_node_id=node_ids[slot.from_key],
                        to_node_id=node_ids[node.stable_key],
                        input_slot=slot.slot,
                        ordinal=slot.ordinal,
                        asset_role=slot.asset_role,
                    )
                )
                edge_count += 1
        await self._session.flush()
        return PersistedGraph(
            id=graph_revision_id,
            canonical_hash=graph.canonical_hash,
            node_count=len(graph.nodes),
            edge_count=edge_count,
        )

    async def _ensure_catalog(
        self,
    ) -> tuple[uuid.UUID, dict[str, uuid.UUID], dict[str, uuid.UUID]]:
        await self._session.execute(
            text("select pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": ORBIT_CATALOG_LOCK_ID},
        )
        template_definition = ORBIT_TEMPLATE.model_dump(mode="json")
        canonical_payload(template_definition)
        template = await self._session.scalar(
            select(GraphTemplateRow).where(
                GraphTemplateRow.key == ORBIT_TEMPLATE.key,
                GraphTemplateRow.version == ORBIT_TEMPLATE.version,
            )
        )
        if template is None:
            template = GraphTemplateRow(
                id=uuid.uuid4(),
                key=ORBIT_TEMPLATE.key,
                version=ORBIT_TEMPLATE.version,
                schema_version=ORBIT_TEMPLATE.schema_version,
                definition_json=template_definition,
            )
            self._session.add(template)
            await self._session.flush()
        elif template.definition_json != template_definition:
            raise InvalidSourceError("Stored ORBIT template version is not immutable.")

        provider_ids: dict[str, uuid.UUID] = {}
        for key, definition in ORBIT_PROVIDER_POLICIES.items():
            provider_ids[key] = await self._ensure_provider_policy(key, definition)
        validation_ids: dict[str, uuid.UUID] = {}
        for key, definition in ORBIT_VALIDATION_POLICIES.items():
            validation_ids[key] = await self._ensure_validation_policy(key, definition)
        return template.id, provider_ids, validation_ids

    async def _ensure_provider_policy(
        self, key: str, definition: dict[str, JsonValue]
    ) -> uuid.UUID:
        """Resolve this definition to an immutable policy version, creating one if
        the definition is new.

        §5.5 FR-PROV-001 requires a policy change to change node fingerprints,
        and §8.1 requires each stored version to be immutable. Both hold only if a
        changed definition becomes a *new version* rather than an edit: pinning
        the lookup to version 1 satisfied immutability by making change
        impossible, which is not the same thing.

        An existing version whose hash matches is reused, so recompiling an
        unchanged policy is idempotent and does not inflate the version number.
        Older versions stay exactly as they were — builds that referenced them
        keep resolving to the definition they actually ran against.
        """
        digest = canonical_hash(definition)
        rows = (
            await self._session.scalars(
                select(ProviderPolicy)
                .where(
                    ProviderPolicy.organization_id.is_(None),
                    ProviderPolicy.key == key,
                )
                .order_by(ProviderPolicy.version)
            )
        ).all()

        for row in rows:
            if row.canonical_hash == digest:
                if row.definition_json != definition:
                    # Same hash, different bytes: canonicalisation and storage
                    # have diverged. Failing loudly beats silently trusting a
                    # hash that no longer describes what is stored.
                    raise InvalidSourceError(
                        f"Provider policy {key} v{row.version} hashes to its stored digest "
                        "but its definition differs; canonicalisation has drifted."
                    )
                return row.id

        row = ProviderPolicy(
            id=uuid.uuid4(),
            organization_id=None,
            key=key,
            version=max((r.version for r in rows), default=0) + 1,
            definition_json=definition,
            canonical_hash=digest,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def _ensure_validation_policy(
        self, key: str, definition: dict[str, JsonValue]
    ) -> uuid.UUID:
        """Same versioning contract as provider policies.

        §5.6 FR-QA-006 expects a validator-policy change to be able to force
        fresh QA without regenerating media, which requires the change to be
        expressible at all.
        """
        digest = canonical_hash(definition)
        rows = (
            await self._session.scalars(
                select(ValidationPolicy)
                .where(
                    ValidationPolicy.organization_id.is_(None),
                    ValidationPolicy.key == key,
                )
                .order_by(ValidationPolicy.version)
            )
        ).all()

        for row in rows:
            if row.canonical_hash == digest:
                if row.definition_json != definition:
                    raise InvalidSourceError(
                        f"Validation policy {key} v{row.version} hashes to its stored digest "
                        "but its definition differs; canonicalisation has drifted."
                    )
                return row.id

        row = ValidationPolicy(
            id=uuid.uuid4(),
            organization_id=None,
            key=key,
            version=max((r.version for r in rows), default=0) + 1,
            definition_json=definition,
            canonical_hash=digest,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id


def _parameters(spec: dict[str, JsonValue]) -> dict[str, JsonValue]:
    raw = spec.get("parameters", {})
    if not isinstance(raw, dict):
        raise InvalidSourceError("Project revision parameters field must be an object.")
    parameters: dict[str, JsonValue] = dict(raw)
    parameters.setdefault(PARAM_LEGAL_LINE, DEFAULT_LEGAL_LINE)
    parameters.setdefault(PARAM_BRIEF_TEXT, DEFAULT_BRIEF_TEXT)
    canonical_payload(parameters)
    return parameters
