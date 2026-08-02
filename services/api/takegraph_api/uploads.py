"""Presigned source upload and durable finalization (PRD §11.4, §15.3)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_domain.auth import Permission, Principal, authorize_project
from takegraph_domain.enums import TriggerSource
from takegraph_domain.errors import (
    AssetVerificationError,
    InvalidSourceError,
    NotFoundError,
    UploadIncompleteError,
)
from takegraph_domain.storage.keys import (
    assert_sha256,
    content_address,
    safe_extension,
    sanitize_filename,
    temporary_upload_key,
)
from takegraph_infrastructure.b2 import B2Settings, B2Store
from takegraph_infrastructure.media import MediaProbe, detect_mime, probe_media_bytes

from takegraph_api.auth import require_permission
from takegraph_api.db.models import (
    Asset,
    B2ObjectEvent,
    Project,
    Source,
    SourceVersion,
    UploadIntent,
)
from takegraph_api.db.session import session_scope
from takegraph_api.queue import WorkQueue

SOURCE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
ALLOWED_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "audio/wav",
        "audio/mpeg",
        "audio/flac",
        "audio/mp4",
        "audio/webm",
    }
)
MAX_IMAGE_PIXELS = 50_000_000


class UploadInitiationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_stable_key: str = Field(min_length=3, max_length=128)
    file_name: str = Field(min_length=1, max_length=500)
    size_bytes: int = Field(gt=0)
    mime_type: str = Field(min_length=3, max_length=128)
    sha256: str | None = None

    @field_validator("source_stable_key")
    @classmethod
    def valid_source_key(cls, value: str) -> str:
        if SOURCE_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError("source_stable_key must use lowercase dotted identifier syntax")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_sha256(cls, value: str | None) -> str | None:
        return None if value is None else assert_sha256(value)


class UploadInitiationResponse(BaseModel):
    upload_id: uuid.UUID
    method: Literal["PUT"] = "PUT"
    upload_url: str
    required_headers: dict[str, str]
    expires_at: datetime


class UploadCompletionResponse(BaseModel):
    upload_id: uuid.UUID
    source_id: uuid.UUID
    source_version_id: uuid.UUID
    asset_id: uuid.UUID
    sha256: str
    size_bytes: int
    mime_type: str
    media_kind: str
    trigger_source: TriggerSource = TriggerSource.APPLICATION_COMMIT


class SourceUploadService:
    def __init__(
        self,
        session: AsyncSession,
        store: B2Store,
        *,
        max_upload_bytes: int,
        max_video_duration_seconds: int,
        temp_root: Path,
        signed_url_ttl_seconds: int,
    ) -> None:
        self._session = session
        self._store = store
        self._max_upload_bytes = max_upload_bytes
        self._max_video_duration_seconds = max_video_duration_seconds
        self._temp_root = temp_root
        self._signed_url_ttl_seconds = signed_url_ttl_seconds

    async def initiate(
        self,
        *,
        project_id: uuid.UUID,
        principal: Principal,
        request: UploadInitiationRequest,
    ) -> UploadInitiationResponse:
        project = await self._project(project_id)
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.EDIT_SOURCES,
        )
        if request.size_bytes > self._max_upload_bytes:
            raise InvalidSourceError(
                "Upload exceeds the configured size limit.",
                details={"max_bytes": self._max_upload_bytes},
            )
        mime_type = request.mime_type.lower().split(";", 1)[0].strip()
        if mime_type not in ALLOWED_MIME_TYPES:
            raise InvalidSourceError("Upload MIME type is not supported.")

        upload_id = uuid.uuid4()
        safe_name = sanitize_filename(request.file_name)
        object_key = temporary_upload_key(upload_id=upload_id, filename=safe_name)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._signed_url_ttl_seconds)
        self._session.add(
            UploadIntent(
                id=upload_id,
                organization_id=project.organization_id,
                project_id=project.id,
                source_stable_key=request.source_stable_key,
                original_file_name=safe_name,
                expected_size_bytes=request.size_bytes,
                declared_mime_type=mime_type,
                client_sha256=request.sha256,
                object_key=object_key,
                status="INITIATED",
                expires_at=expires_at,
                created_by=principal.actor_id,
            )
        )
        await self._session.flush()
        upload_url = await asyncio.to_thread(
            self._store.presign_put,
            object_key,
            content_type=mime_type,
            ttl_seconds=self._signed_url_ttl_seconds,
        )
        return UploadInitiationResponse(
            upload_id=upload_id,
            upload_url=upload_url,
            required_headers={"Content-Type": mime_type},
            expires_at=expires_at,
        )

    async def complete(
        self,
        *,
        project_id: uuid.UUID,
        upload_id: uuid.UUID,
        principal: Principal,
    ) -> UploadCompletionResponse:
        result = await self._session.execute(
            select(UploadIntent).where(UploadIntent.id == upload_id).with_for_update()
        )
        intent = result.scalar_one_or_none()
        if intent is None or intent.project_id != project_id:
            raise NotFoundError("Upload intent was not found.")
        project = await self._project(project_id)
        authorize_project(
            principal,
            project_id=project.id,
            project_organization_id=project.organization_id,
            permission=Permission.EDIT_SOURCES,
        )
        if intent.status == "COMPLETED":
            return await self._completed_response(intent)
        if intent.expires_at <= datetime.now(UTC):
            raise UploadIncompleteError("Upload intent has expired.")

        head = await asyncio.to_thread(self._store.head, intent.object_key)
        if head is None:
            raise UploadIncompleteError("Uploaded object is not present in quarantine storage.")
        if head.size_bytes != intent.expected_size_bytes:
            raise UploadIncompleteError(
                "Uploaded object size does not match the upload intent.",
                details={"expected": intent.expected_size_bytes, "actual": head.size_bytes},
            )
        if head.size_bytes > self._max_upload_bytes:
            raise UploadIncompleteError("Uploaded object exceeds the configured size limit.")
        stored_content_type = (head.content_type or "").lower().split(";", 1)[0].strip()
        if stored_content_type != intent.declared_mime_type:
            raise UploadIncompleteError("Stored Content-Type does not match the upload intent.")

        data = await asyncio.to_thread(self._store.get_bytes, intent.object_key)
        if len(data) != intent.expected_size_bytes:
            raise AssetVerificationError("Downloaded upload size differs from the B2 HEAD result.")
        digest = hashlib.sha256(data).hexdigest()
        if intent.client_sha256 is not None and digest != intent.client_sha256:
            raise AssetVerificationError("Uploaded bytes do not match the client SHA-256.")

        detected_mime = detect_mime(data)
        extension = safe_extension(intent.original_file_name, mime_type=detected_mime)
        probe = await asyncio.to_thread(
            probe_media_bytes,
            data,
            suffix=f".{extension}" if extension else "",
            temp_root=self._temp_root,
        )
        if not _mime_matches(intent.declared_mime_type, detected_mime, probe):
            raise InvalidSourceError("Declared MIME type does not match the uploaded media bytes.")
        self._validate_probe(probe)

        canonical_key = content_address(
            organization_id=project.organization_id,
            sha256=digest,
            extension=extension,
        )
        stored = await asyncio.to_thread(
            self._store.store_bytes,
            canonical_key,
            data,
            content_type=intent.declared_mime_type,
            metadata={"original-filename": intent.original_file_name},
        )
        verified = await asyncio.to_thread(
            self._store.verify,
            canonical_key,
            expected_sha256=digest,
        )
        if not verified:
            raise AssetVerificationError("Canonical B2 bytes failed SHA-256 verification.")

        asset_id = await self._upsert_asset(
            organization_id=project.organization_id,
            stored_key=canonical_key,
            digest=digest,
            size_bytes=len(data),
            mime_type=intent.declared_mime_type,
            probe=probe,
            storage_version_id=stored.version_id,
        )
        source_id = await self._upsert_source(
            project_id=project.id,
            stable_key=intent.source_stable_key,
            media_kind=probe.media_kind,
        )
        source_version_id = uuid.uuid4()
        self._session.add(
            SourceVersion(
                id=source_version_id,
                source_id=source_id,
                revision_id=None,
                asset_id=asset_id,
                normalized_text=None,
                content_hash=digest,
                created_by=principal.actor_id,
            )
        )
        event_id = uuid.uuid4()
        self._session.add(
            B2ObjectEvent(
                id=event_id,
                dedupe_key=f"application-upload:{upload_id}",
                message_id=None,
                event_type="takegraph:ObjectFinalized",
                bucket=self._store.bucket,
                object_key=canonical_key,
                object_version_id=stored.version_id,
                object_size=len(data),
                event_timestamp=datetime.now(UTC),
                status="RECEIVED",
                trigger_source=str(TriggerSource.APPLICATION_COMMIT),
            )
        )
        # The explicit UPDATE below carries foreign keys to pending ORM rows.
        # autoflush=False is intentional, so establish those rows first.
        await self._session.flush()
        await WorkQueue(self._session).enqueue(
            kind="validate_source_upload",
            target_id=source_version_id,
            dedupe_key=f"source-finalize:{upload_id}",
        )
        await self._session.execute(
            update(UploadIntent)
            .where(UploadIntent.id == upload_id)
            .values(
                status="COMPLETED",
                completed_asset_id=asset_id,
                completed_source_version_id=source_version_id,
                completed_at=datetime.now(UTC),
            )
        )
        return UploadCompletionResponse(
            upload_id=upload_id,
            source_id=source_id,
            source_version_id=source_version_id,
            asset_id=asset_id,
            sha256=digest,
            size_bytes=len(data),
            mime_type=intent.declared_mime_type,
            media_kind=probe.media_kind,
        )

    async def _project(self, project_id: uuid.UUID) -> Project:
        project = await self._session.get(Project, project_id)
        if project is None:
            raise NotFoundError("Project was not found.")
        return project

    async def _upsert_asset(
        self,
        *,
        organization_id: uuid.UUID,
        stored_key: str,
        digest: str,
        size_bytes: int,
        mime_type: str,
        probe: MediaProbe,
        storage_version_id: str | None,
    ) -> uuid.UUID:
        asset_id = uuid.uuid4()
        result = await self._session.execute(
            insert(Asset)
            .values(
                id=asset_id,
                organization_id=organization_id,
                sha256=digest,
                size_bytes=size_bytes,
                mime_type=mime_type,
                media_kind=probe.media_kind,
                b2_bucket=self._store.bucket,
                b2_key=stored_key,
                storage_version_id=storage_version_id,
                metadata_json=asdict(probe),
                verified_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[Asset.organization_id, Asset.sha256])
            .returning(Asset.id)
        )
        inserted_id = result.scalar_one_or_none()
        if inserted_id is not None:
            return inserted_id
        existing = await self._session.scalar(
            select(Asset).where(
                Asset.organization_id == organization_id,
                Asset.sha256 == digest,
            )
        )
        if existing is None:
            raise AssetVerificationError("Asset deduplication did not resolve a canonical row.")
        if existing.size_bytes != size_bytes or existing.b2_key != stored_key:
            raise AssetVerificationError("Existing asset metadata conflicts with stored bytes.")
        return existing.id

    async def _upsert_source(
        self, *, project_id: uuid.UUID, stable_key: str, media_kind: str
    ) -> uuid.UUID:
        source_id = uuid.uuid4()
        result = await self._session.execute(
            insert(Source)
            .values(
                id=source_id,
                project_id=project_id,
                stable_key=stable_key,
                kind=media_kind,
            )
            .on_conflict_do_nothing(index_elements=[Source.project_id, Source.stable_key])
            .returning(Source.id)
        )
        inserted_id = result.scalar_one_or_none()
        if inserted_id is not None:
            return inserted_id
        existing = await self._session.scalar(
            select(Source).where(
                Source.project_id == project_id,
                Source.stable_key == stable_key,
            )
        )
        if existing is None:
            raise InvalidSourceError("Source identity could not be resolved.")
        if existing.kind != media_kind:
            raise InvalidSourceError("Replacement media kind differs from the existing source.")
        return existing.id

    async def _completed_response(self, intent: UploadIntent) -> UploadCompletionResponse:
        if intent.completed_asset_id is None or intent.completed_source_version_id is None:
            raise AssetVerificationError("Completed upload is missing its durable references.")
        asset = await self._session.get(Asset, intent.completed_asset_id)
        source_version = await self._session.get(SourceVersion, intent.completed_source_version_id)
        if asset is None or source_version is None:
            raise AssetVerificationError("Completed upload references are not resolvable.")
        source = await self._session.get(Source, source_version.source_id)
        if source is None:
            raise AssetVerificationError("Completed source identity is not resolvable.")
        return UploadCompletionResponse(
            upload_id=intent.id,
            source_id=source.id,
            source_version_id=source_version.id,
            asset_id=asset.id,
            sha256=asset.sha256,
            size_bytes=asset.size_bytes,
            mime_type=asset.mime_type,
            media_kind=asset.media_kind,
        )

    def _validate_probe(self, probe: MediaProbe) -> None:
        if (
            probe.width is not None
            and probe.height is not None
            and probe.width * probe.height > MAX_IMAGE_PIXELS
        ):
            raise InvalidSourceError("Uploaded visual media exceeds the pixel limit.")
        if probe.media_kind == "VIDEO":
            max_duration_ms = self._max_video_duration_seconds * 1000
            if probe.duration_ms is None or probe.duration_ms > max_duration_ms:
                raise InvalidSourceError(
                    "Uploaded video exceeds the duration limit.",
                    details={"max_duration_seconds": self._max_video_duration_seconds},
                )


def _mime_matches(declared: str, detected: str, probe: MediaProbe) -> bool:
    if declared == detected:
        return True
    if detected == "video/mp4" and declared == "audio/mp4":
        return probe.media_kind == "AUDIO"
    if detected == "video/webm" and declared == "audio/webm":
        return probe.media_kind == "AUDIO"
    return False


def _positive_int_env(name: str) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except ValueError as exc:
        raise InvalidSourceError(f"{name} must be configured as a positive integer.") from exc
    if value <= 0:
        raise InvalidSourceError(f"{name} must be configured as a positive integer.")
    return value


def upload_service(session: AsyncSession, store: B2Store) -> SourceUploadService:
    return SourceUploadService(
        session,
        store,
        max_upload_bytes=_positive_int_env("MAX_UPLOAD_BYTES"),
        max_video_duration_seconds=_positive_int_env("MAX_VIDEO_DURATION_SECONDS"),
        temp_root=Path(os.environ.get("TEMP_WORK_DIR", "")),
        signed_url_ttl_seconds=_positive_int_env("B2_SIGNED_URL_TTL_SECONDS"),
    )


EditorPrincipal = Annotated[
    Principal,
    Depends(require_permission(Permission.EDIT_SOURCES)),
]
router = APIRouter(prefix="/api/v1", tags=["sources"])


@router.post(
    "/projects/{project_id}/uploads",
    response_model=UploadInitiationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def initiate_upload(
    project_id: uuid.UUID,
    request: UploadInitiationRequest,
    principal: EditorPrincipal,
) -> UploadInitiationResponse:
    settings = B2Settings.from_env(dict(os.environ))
    store = B2Store(settings)
    try:
        async with session_scope() as session:
            return await upload_service(session, store).initiate(
                project_id=project_id,
                principal=principal,
                request=request,
            )
    finally:
        store.close()


@router.post(
    "/projects/{project_id}/uploads/{upload_id}/complete",
    response_model=UploadCompletionResponse,
)
async def complete_upload(
    project_id: uuid.UUID,
    upload_id: uuid.UUID,
    principal: EditorPrincipal,
) -> UploadCompletionResponse:
    settings = B2Settings.from_env(dict(os.environ))
    store = B2Store(settings)
    try:
        async with session_scope() as session:
            return await upload_service(session, store).complete(
                project_id=project_id,
                upload_id=upload_id,
                principal=principal,
            )
    finally:
        store.close()
