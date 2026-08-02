"""B2 webhook contract and real-Postgres dedupe tests (PRD §22.3)."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import text
from takegraph_api.b2_webhooks import B2WebhookIngestor, verify_b2_signature
from takegraph_api.main import app
from takegraph_domain.errors import B2SignatureInvalidError, InvalidSourceError

SECRET = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"  # noqa: S105 — synthetic test key


def _body(events: list[dict[str, object]]) -> bytes:
    return json.dumps({"events": events}, separators=(",", ":")).encode()


def _signature(body: bytes) -> str:
    return f"v1={hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}"


def _event(event_id: str, event_type: str = "b2:ObjectCreated:Upload") -> dict[str, object]:
    return {
        "accountId": "account-redacted",
        "bucketId": "bucket-id",
        "bucketName": "takegraph-work",
        "eventId": event_id,
        "eventTimestamp": 1_722_500_000_123,
        "eventType": event_type,
        "eventVersion": 1,
        "matchedRuleName": "takegraph-work-events",
        "objectName": f"temporary/uploads/{event_id}/source.png",
        "objectSize": 1024,
        "objectVersionId": f"version-{event_id}",
    }


class TestSignatureVerification:
    def test_valid_signature_uses_raw_body(self) -> None:
        body = _body([_event("evt-1")])
        assert verify_b2_signature(body, _signature(body), secret=SECRET).version == "v1"

    @pytest.mark.parametrize(
        "header",
        [None, "", "v2=" + "a" * 64, "v1=" + "A" * 64, "v1=short", "v1=a=b"],
    )
    def test_missing_or_malformed_signature_is_rejected(self, header: str | None) -> None:
        with pytest.raises(B2SignatureInvalidError):
            verify_b2_signature(b"{}", header, secret=SECRET)

    def test_signature_for_different_bytes_is_rejected(self) -> None:
        with pytest.raises(B2SignatureInvalidError):
            verify_b2_signature(b'{"events":[]}', _signature(b"{}"), secret=SECRET)

    async def test_http_boundary_returns_typed_401_for_bad_signature(self, monkeypatch) -> None:
        monkeypatch.setenv("B2_EVENT_SIGNING_SECRET", SECRET)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/webhooks/b2/events",
                content=b'{"events":[]}',
                headers={"X-Bz-Event-Notification-Signature": "v1=" + "0" * 64},
            )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "B2_SIGNATURE_INVALID"
        assert response.headers["X-Request-ID"]

    async def test_http_boundary_reports_unconfigured_secret(self, monkeypatch) -> None:
        monkeypatch.delenv("B2_EVENT_SIGNING_SECRET", raising=False)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/webhooks/b2/events", content=b"{}")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "FEATURE_NOT_CONFIGURED"


class TestDurableIngestion:
    async def test_multi_event_payload_is_persisted_and_queued(self, session) -> None:
        body = _body([_event("evt-a"), _event("evt-b", "b2:ObjectDeleted:Delete")])

        receipt = await B2WebhookIngestor(session).ingest(
            raw_body=body,
            signature_header=_signature(body),
            secret=SECRET,
        )
        await session.commit()

        assert receipt.received_events == 2
        assert receipt.queued_events == 2
        assert await session.scalar(text("select count(*) from b2_webhook_messages")) == 1
        assert await session.scalar(text("select count(*) from b2_object_events")) == 2
        assert await session.scalar(text("select count(*) from work_items")) == 2
        assert (
            await session.scalar(
                text("select count(*) from b2_object_events where trigger_source='B2_EVENT'")
            )
            == 2
        )

    async def test_replayed_message_creates_no_duplicate_event_or_job(self, session) -> None:
        body = _body([_event("evt-replay")])
        ingestor = B2WebhookIngestor(session)
        first = await ingestor.ingest(
            raw_body=body, signature_header=_signature(body), secret=SECRET
        )
        await session.commit()
        second = await ingestor.ingest(
            raw_body=body, signature_header=_signature(body), secret=SECRET
        )
        await session.commit()

        assert first.duplicate_message is False
        assert second.duplicate_message is True
        assert await session.scalar(text("select count(*) from b2_object_events")) == 1
        assert await session.scalar(text("select count(*) from work_items")) == 1

    async def test_event_dedupes_across_different_message_bodies(self, session) -> None:
        first_body = _body([_event("evt-shared")])
        second_event = _event("evt-shared")
        second_event["matchedRuleName"] = "redelivered-by-another-rule"
        second_body = _body([second_event])
        ingestor = B2WebhookIngestor(session)

        await ingestor.ingest(
            raw_body=first_body, signature_header=_signature(first_body), secret=SECRET
        )
        await session.commit()
        receipt = await ingestor.ingest(
            raw_body=second_body, signature_header=_signature(second_body), secret=SECRET
        )
        await session.commit()

        assert receipt.received_events == 0
        assert await session.scalar(text("select count(*) from b2_webhook_messages")) == 2
        assert await session.scalar(text("select count(*) from b2_object_events")) == 1
        assert await session.scalar(text("select count(*) from work_items")) == 1

    async def test_distinct_event_ids_for_same_object_queue_one_action(self, session) -> None:
        first_body = _body([_event("evt-object-first")])
        second_event = _event("evt-object-second")
        second_event["objectName"] = "temporary/uploads/evt-object-first/source.png"
        second_event["objectVersionId"] = "another-confirmation"
        second_body = _body([second_event])
        ingestor = B2WebhookIngestor(session)

        await ingestor.ingest(
            raw_body=first_body, signature_header=_signature(first_body), secret=SECRET
        )
        await session.commit()
        await ingestor.ingest(
            raw_body=second_body, signature_header=_signature(second_body), secret=SECRET
        )
        await session.commit()

        assert await session.scalar(text("select count(*) from b2_object_events")) == 2
        assert await session.scalar(text("select count(*) from work_items")) == 1

    async def test_test_event_is_recorded_without_work(self, session) -> None:
        body = _body([_event("evt-test", "b2:TestEvent")])
        receipt = await B2WebhookIngestor(session).ingest(
            raw_body=body, signature_header=_signature(body), secret=SECRET
        )
        await session.commit()

        assert receipt.ignored_events == 1
        assert (
            await session.scalar(
                text("select status from b2_object_events where dedupe_key='evt-test'")
            )
            == "TESTED"
        )
        assert await session.scalar(text("select count(*) from work_items")) == 0

    async def test_unknown_event_is_recorded_and_ignored(self, session) -> None:
        body = _body([_event("evt-future", "b2:ObjectFrobnicated:Future")])
        receipt = await B2WebhookIngestor(session).ingest(
            raw_body=body, signature_header=_signature(body), secret=SECRET
        )
        await session.commit()

        assert receipt.ignored_events == 1
        assert (
            await session.scalar(
                text("select status from b2_object_events where dedupe_key='evt-future'")
            )
            == "IGNORED"
        )
        assert await session.scalar(text("select count(*) from work_items")) == 0

    async def test_actionable_event_requires_object_coordinates(self, session) -> None:
        event = _event("evt-incomplete")
        event.pop("objectName")
        body = _body([event])

        with pytest.raises(InvalidSourceError):
            await B2WebhookIngestor(session).ingest(
                raw_body=body,
                signature_header=_signature(body),
                secret=SECRET,
            )

        await session.rollback()
        assert await session.scalar(text("select count(*) from b2_webhook_messages")) == 0
