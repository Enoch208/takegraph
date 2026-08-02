"""Signed Backblaze B2 Event Notification ingestion (PRD §11.8, §15.4).

The HTTP boundary verifies the HMAC over the exact raw body before parsing JSON.
The application service then persists the message, deduplicates each event, and
queues only a reference to durable event state. No media work runs in the webhook
request, which keeps acknowledgements inside Backblaze's three-second timeout.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from takegraph_domain.enums import TriggerSource
from takegraph_domain.errors import (
    B2SignatureInvalidError,
    FeatureNotConfiguredError,
    InvalidSourceError,
)

from takegraph_api.db.models import B2ObjectEvent, B2WebhookMessage
from takegraph_api.db.session import session_scope
from takegraph_api.queue import WorkQueue

SIGNATURE_PATTERN = re.compile(r"^v1=([0-9a-f]{64})$")
MAX_WEBHOOK_BYTES = 1_048_576
MAX_EVENTS_PER_MESSAGE = 100

SUPPORTED_OBJECT_EVENTS = frozenset(
    {
        "b2:ObjectCreated:Upload",
        "b2:ObjectCreated:MultipartUpload",
        "b2:ObjectCreated:Copy",
        "b2:ObjectCreated:Replica",
        "b2:ObjectCreated:MultipartReplica",
        "b2:ObjectDeleted:Delete",
        "b2:ObjectDeleted:LifecycleRule",
    }
)


class B2EventPayload(BaseModel):
    """The stable subset TAKEGRAPH indexes; unknown future fields are ignored."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    event_id: str = Field(alias="eventId", min_length=1, max_length=100)
    event_type: str = Field(alias="eventType", min_length=1, max_length=64)
    event_version: int = Field(alias="eventVersion", ge=1)
    event_timestamp_ms: int = Field(alias="eventTimestamp", ge=0)
    bucket_name: str | None = Field(default=None, alias="bucketName", max_length=128)
    object_name: str | None = Field(default=None, alias="objectName", max_length=1024)
    object_size: int | None = Field(default=None, alias="objectSize", ge=0)
    object_version_id: str | None = Field(default=None, alias="objectVersionId", max_length=128)


class B2NotificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[B2EventPayload] = Field(
        min_length=1,
        max_length=MAX_EVENTS_PER_MESSAGE,
    )


class B2WebhookReceipt(BaseModel):
    accepted: Literal[True] = True
    duplicate_message: bool
    received_events: int
    queued_events: int
    ignored_events: int


@dataclass(frozen=True, slots=True)
class SignatureVerification:
    version: str


def signing_secret_from_env() -> str:
    secret = os.environ.get("B2_EVENT_SIGNING_SECRET", "")
    if len(secret) != 32 or not secret.isalnum():
        raise FeatureNotConfiguredError(
            "B2 Event Notifications are not configured with a valid signing secret."
        )
    return secret


def verify_b2_signature(
    raw_body: bytes, signature_header: str | None, *, secret: str
) -> SignatureVerification:
    """Verify the v1 HMAC using a constant-time digest comparison."""
    if not signature_header:
        raise B2SignatureInvalidError("B2 event signature is missing.")
    match = SIGNATURE_PATTERN.fullmatch(signature_header)
    if match is None:
        raise B2SignatureInvalidError("B2 event signature format or version is invalid.")

    received_digest = match.group(1)
    expected_digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_digest, expected_digest):
        raise B2SignatureInvalidError("B2 event signature does not match the payload.")
    return SignatureVerification(version="v1")


def parse_b2_payload(raw_body: bytes) -> B2NotificationPayload:
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        raise InvalidSourceError(
            "B2 event payload exceeds the accepted size.",
            details={"max_bytes": MAX_WEBHOOK_BYTES},
        )
    try:
        return B2NotificationPayload.model_validate_json(raw_body)
    except ValidationError as exc:
        raise InvalidSourceError(
            "B2 event payload is invalid.",
            details={"validation_errors": len(exc.errors())},
        ) from exc


class B2WebhookIngestor:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ingest(
        self,
        *,
        raw_body: bytes,
        signature_header: str | None,
        secret: str,
    ) -> B2WebhookReceipt:
        signature = verify_b2_signature(raw_body, signature_header, secret=secret)
        payload = parse_b2_payload(raw_body)
        return await self.persist(
            raw_body=raw_body,
            signature=signature,
            payload=payload,
        )

    async def persist(
        self,
        *,
        raw_body: bytes,
        signature: SignatureVerification,
        payload: B2NotificationPayload,
    ) -> B2WebhookReceipt:
        """Persist an already authenticated and structurally validated message."""
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        message_id = uuid.uuid4()

        message_result = await self._session.execute(
            insert(B2WebhookMessage)
            .values(
                id=message_id,
                signature_version=signature.version,
                body_sha256=body_sha256,
                signature_valid=True,
            )
            .on_conflict_do_nothing(index_elements=[B2WebhookMessage.body_sha256])
            .returning(B2WebhookMessage.id)
        )
        if message_result.scalar_one_or_none() is None:
            return B2WebhookReceipt(
                duplicate_message=True,
                received_events=0,
                queued_events=0,
                ignored_events=0,
            )

        received = queued = ignored = 0
        queue = WorkQueue(self._session)
        for event in payload.events:
            status, actionable = _classify_event(event)
            event_row_id = uuid.uuid4()
            inserted = await self._session.execute(
                insert(B2ObjectEvent)
                .values(
                    id=event_row_id,
                    dedupe_key=event.event_id,
                    message_id=message_id,
                    event_type=event.event_type,
                    bucket=event.bucket_name or "",
                    object_key=event.object_name or "",
                    object_version_id=event.object_version_id,
                    object_size=event.object_size,
                    event_timestamp=_event_timestamp(event.event_timestamp_ms),
                    status=status,
                    trigger_source=str(TriggerSource.B2_EVENT),
                )
                .on_conflict_do_nothing(index_elements=[B2ObjectEvent.dedupe_key])
                .returning(B2ObjectEvent.id)
            )
            if inserted.scalar_one_or_none() is None:
                continue

            received += 1
            if actionable:
                work_id = await queue.enqueue(
                    kind="process_b2_event",
                    target_id=event_row_id,
                    dedupe_key=object_work_dedupe(
                        bucket=event.bucket_name or "",
                        object_key=event.object_name or "",
                        event_type=event.event_type,
                    ),
                )
                if work_id is not None:
                    queued += 1
            else:
                ignored += 1

        await self._session.execute(
            update(B2WebhookMessage)
            .where(B2WebhookMessage.id == message_id)
            .values(processed_at=datetime.now(UTC))
        )
        return B2WebhookReceipt(
            duplicate_message=False,
            received_events=received,
            queued_events=queued,
            ignored_events=ignored,
        )


def _classify_event(event: B2EventPayload) -> tuple[str, bool]:
    if event.event_type == "b2:TestEvent":
        return "TESTED", False
    if event.event_type not in SUPPORTED_OBJECT_EVENTS:
        return "IGNORED", False
    if not event.bucket_name or not event.object_name:
        raise InvalidSourceError(
            "B2 object event is missing its bucket or object name.",
            details={"event_id": event.event_id},
        )
    return "RECEIVED", True


def _event_timestamp(milliseconds: int) -> datetime:
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidSourceError("B2 event timestamp is outside the accepted range.") from exc


def object_work_dedupe(*, bucket: str, object_key: str, event_type: str) -> str:
    """Collapse duplicate confirmations of one immutable object operation.

    B2 event IDs deduplicate event records. Work is keyed by the operation's
    object coordinates so a webhook racing the periodic reconciler still creates
    one downstream action. Creation and deletion remain distinct operations.
    """
    family = "delete" if event_type.startswith("b2:ObjectDeleted:") else "create"
    digest = hashlib.sha256(f"{bucket}\0{object_key}\0{family}".encode()).hexdigest()
    return f"b2-object:{digest}"


router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/b2/events", response_model=B2WebhookReceipt)
async def receive_b2_events(
    request: Request,
    signature: Annotated[
        str | None,
        Header(alias="X-Bz-Event-Notification-Signature"),
    ] = None,
) -> B2WebhookReceipt:
    """Verify, durably insert, and enqueue before returning the required 200."""
    raw_body = await request.body()
    secret = signing_secret_from_env()
    signature_verification = verify_b2_signature(raw_body, signature, secret=secret)
    payload = parse_b2_payload(raw_body)
    async with session_scope() as session:
        return await B2WebhookIngestor(session).persist(
            raw_body=raw_body,
            signature=signature_verification,
            payload=payload,
        )
