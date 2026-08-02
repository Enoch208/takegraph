"""Worker handlers for durable source-upload and B2 event work items."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from takegraph_api.db.models import Asset, B2ObjectEvent, SourceVersion, UploadIntent
from takegraph_domain.errors import AssetVerificationError, NotFoundError
from takegraph_infrastructure.b2 import B2Store


@dataclass(frozen=True, slots=True)
class SourceAssetSnapshot:
    asset_id: uuid.UUID
    sha256: str
    size_bytes: int
    bucket: str
    object_key: str


@dataclass(frozen=True, slots=True)
class ObjectEventSnapshot:
    event_id: uuid.UUID
    event_type: str
    bucket: str
    object_key: str
    object_size: int | None
    status: str
    upload_intent_id: uuid.UUID | None
    expected_size_bytes: int | None


class SourceWorkHandlers:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: B2Store,
    ) -> None:
        self._session_factory = session_factory
        self._store = store

    async def validate_source_upload(self, source_version_id: uuid.UUID) -> None:
        snapshot = await self._source_asset_snapshot(source_version_id)
        if snapshot.bucket != self._store.bucket:
            raise AssetVerificationError("Source asset belongs to an unavailable storage bucket.")

        head = await asyncio.to_thread(self._store.head, snapshot.object_key)
        if head is None:
            raise AssetVerificationError("Source asset is missing from durable storage.")
        if head.size_bytes != snapshot.size_bytes:
            raise AssetVerificationError("Source asset size no longer matches its durable record.")
        verified = await asyncio.to_thread(
            self._store.verify,
            snapshot.object_key,
            expected_sha256=snapshot.sha256,
        )
        if not verified:
            raise AssetVerificationError("Source asset bytes no longer match the recorded SHA-256.")

        async with self._session_factory() as session:
            result = await session.execute(
                update(Asset)
                .where(
                    Asset.id == snapshot.asset_id,
                    Asset.sha256 == snapshot.sha256,
                    Asset.size_bytes == snapshot.size_bytes,
                    Asset.b2_key == snapshot.object_key,
                )
                .values(verified_at=datetime.now(UTC))
                .returning(Asset.id)
            )
            if result.scalar_one_or_none() is None:
                raise AssetVerificationError("Source asset changed during verification.")
            await session.commit()

    async def process_b2_event(self, event_id: uuid.UUID) -> None:
        snapshot = await self._object_event_snapshot(event_id)
        if snapshot.status in {"CONFIRMED", "PROCESSED", "UNMATCHED"}:
            return
        if snapshot.bucket != self._store.bucket:
            raise AssetVerificationError("B2 event belongs to an unavailable storage bucket.")

        if snapshot.event_type.startswith("b2:ObjectDeleted:"):
            await self._record_deleted(snapshot)
            return

        head = await asyncio.to_thread(self._store.head, snapshot.object_key)
        if head is None:
            raise AssetVerificationError("B2 event object is not readable from storage.")
        expected = snapshot.expected_size_bytes or snapshot.object_size
        if expected is not None and head.size_bytes != expected:
            raise AssetVerificationError("B2 event object size does not match durable intent.")
        await self._record_created(snapshot)

    async def _source_asset_snapshot(self, source_version_id: uuid.UUID) -> SourceAssetSnapshot:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(SourceVersion, Asset)
                    .join(Asset, Asset.id == SourceVersion.asset_id)
                    .where(SourceVersion.id == source_version_id)
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError("Source version or its asset was not found.")
            source_version, asset = row
            if source_version.asset_id is None:
                raise AssetVerificationError("Source version has no durable asset.")
            return SourceAssetSnapshot(
                asset_id=asset.id,
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                bucket=asset.b2_bucket,
                object_key=asset.b2_key,
            )

    async def _object_event_snapshot(self, event_id: uuid.UUID) -> ObjectEventSnapshot:
        async with self._session_factory() as session:
            event = await session.get(B2ObjectEvent, event_id)
            if event is None:
                raise NotFoundError("B2 object event was not found.")
            intent = await session.scalar(
                select(UploadIntent).where(UploadIntent.object_key == event.object_key)
            )
            return ObjectEventSnapshot(
                event_id=event.id,
                event_type=event.event_type,
                bucket=event.bucket,
                object_key=event.object_key,
                object_size=event.object_size,
                status=event.status,
                upload_intent_id=None if intent is None else intent.id,
                expected_size_bytes=None if intent is None else intent.expected_size_bytes,
            )

    async def _record_created(self, snapshot: ObjectEventSnapshot) -> None:
        async with self._session_factory() as session:
            event = await session.scalar(
                select(B2ObjectEvent).where(B2ObjectEvent.id == snapshot.event_id).with_for_update()
            )
            if event is None:
                raise NotFoundError("B2 object event was not found.")
            if event.status in {"CONFIRMED", "PROCESSED", "UNMATCHED"}:
                return
            if snapshot.upload_intent_id is None:
                event.status = "UNMATCHED"
            else:
                event.status = "CONFIRMED"
                await session.execute(
                    update(UploadIntent)
                    .where(
                        UploadIntent.id == snapshot.upload_intent_id,
                        UploadIntent.status == "INITIATED",
                    )
                    .values(status="UPLOADED")
                )
            await session.commit()

    async def _record_deleted(self, snapshot: ObjectEventSnapshot) -> None:
        async with self._session_factory() as session:
            event = await session.scalar(
                select(B2ObjectEvent).where(B2ObjectEvent.id == snapshot.event_id).with_for_update()
            )
            if event is None:
                raise NotFoundError("B2 object event was not found.")
            if event.status in {"CONFIRMED", "PROCESSED", "UNMATCHED"}:
                return
            event.status = "PROCESSED"
            if snapshot.upload_intent_id is not None:
                await session.execute(
                    update(UploadIntent)
                    .where(
                        UploadIntent.id == snapshot.upload_intent_id,
                        UploadIntent.status.in_(["INITIATED", "UPLOADED"]),
                    )
                    .values(status="DELETED")
                )
            await session.commit()
