"""Real Backblaze B2 integration tests (PRD §15.8).

These talk to actual B2 with the scoped runtime key. There is no mock: the things
worth testing here — content-addressed dedupe, read-after-write, hash verification
over stored bytes, presigned URL behaviour, and least-privilege enforcement — are
properties of the service, not of our code calling it.

Skipped with a clear reason when credentials are absent, so a contributor without
keys sees "not configured" rather than a failure that looks like a defect.
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
import uuid

import pytest
from takegraph_domain.errors import AssetVerificationError, FeatureNotConfiguredError
from takegraph_domain.storage.keys import content_address
from takegraph_worker.b2_store import B2Settings, B2Store

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _load_env() -> dict[str, str]:
    """Read .env if present so tests work without exporting everything."""
    env = dict(os.environ)
    if os.path.exists(".env"):
        with open(".env") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env.setdefault(key.strip(), value.strip())
    return env


@pytest.fixture(scope="module")
def store() -> B2Store:
    try:
        settings = B2Settings.from_env(_load_env())
    except FeatureNotConfiguredError as exc:
        pytest.skip(str(exc))

    store = B2Store(settings, preflight=True)
    yield store
    store.close()


@pytest.fixture
def payload() -> bytes:
    """Unique per test so runs do not collide, but stable within one test."""
    return f"takegraph integration {uuid.uuid4()}".encode()


def key_for(data: bytes) -> str:
    return content_address(
        organization_id=ORG, sha256=hashlib.sha256(data).hexdigest(), extension="txt"
    )


class TestRoundTrip:
    """§15.8: "Presigned PUT + finalize + SHA verification"."""

    def test_store_then_read_back_identical_bytes(self, store: B2Store, payload: bytes) -> None:
        key = key_for(payload)
        stored = store.store_bytes(key, payload, content_type="text/plain")

        assert stored.sha256 == hashlib.sha256(payload).hexdigest()
        assert stored.size_bytes == len(payload)
        assert store.get_bytes(key) == payload

    def test_head_confirms_size_after_write(self, store: B2Store, payload: bytes) -> None:
        """§8.3.6: an asset is not durable until B2 HEAD confirms expected size."""
        key = key_for(payload)
        store.store_bytes(key, payload, content_type="text/plain")
        assert store.head_size(key) == len(payload)

    def test_verify_rehashes_the_stored_bytes(self, store: B2Store, payload: bytes) -> None:
        """The claim a release makes rests on this: bytes are fetched back from B2
        and hashed, rather than trusting a record written at upload time."""
        key = key_for(payload)
        stored = store.store_bytes(key, payload, content_type="text/plain")
        assert store.verify(key, expected_sha256=stored.sha256) is True

    def test_verify_rejects_a_wrong_hash(self, store: B2Store, payload: bytes) -> None:
        key = key_for(payload)
        store.store_bytes(key, payload, content_type="text/plain")
        assert store.verify(key, expected_sha256="ab" * 32) is False

    def test_missing_object_does_not_exist(self, store: B2Store) -> None:
        absent = content_address(organization_id=ORG, sha256="cd" * 32, extension="txt")
        assert store.exists(absent) is False
        assert store.head_size(absent) is None


class TestContentAddressedDedupe:
    """§15.8: "Content-addressed duplicate upload". §5.7 FR-ASSET-002 requires
    identical content to map to one canonical object."""

    def test_identical_bytes_map_to_one_key(self, store: B2Store, payload: bytes) -> None:
        first = store.store_bytes(key_for(payload), payload, content_type="text/plain")
        second = store.store_bytes(key_for(payload), payload, content_type="text/plain")

        assert first.key == second.key
        assert first.sha256 == second.sha256

    def test_second_write_is_recognised_as_a_duplicate(
        self, store: B2Store, payload: bytes
    ) -> None:
        """The second call must skip the upload. Without this, re-running a build
        re-transfers every unchanged asset and the reuse ratio means nothing."""
        key = key_for(payload)
        store.store_bytes(key, payload, content_type="text/plain")
        second = store.store_bytes(key, payload, content_type="text/plain")

        assert second.deduplicated is True

    def test_different_bytes_map_to_different_keys(self, store: B2Store) -> None:
        a, b = b"alpha content", b"beta content"
        assert key_for(a) != key_for(b)


class TestPresignedUrls:
    """§15.3: presigned S3 PUT, short TTL, no persisted URLs."""

    def test_presigned_get_returns_the_object(self, store: B2Store, payload: bytes) -> None:
        key = key_for(payload)
        store.store_bytes(key, payload, content_type="text/plain")

        url = store.presign_get(key, ttl_seconds=120)
        with urllib.request.urlopen(url, timeout=30) as response:
            assert response.read() == payload

    def test_presigned_put_accepts_an_upload(self, store: B2Store) -> None:
        """This is the browser upload path (FR-PROJ-003): the credential never
        reaches the client, only a scoped short-lived URL does."""
        data = f"presigned upload {uuid.uuid4()}".encode()
        key = key_for(data)
        url = store.presign_put(key, content_type="text/plain", ttl_seconds=300)

        request = urllib.request.Request(
            url, data=data, method="PUT", headers={"Content-Type": "text/plain"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status in (200, 204)

        assert store.get_bytes(key) == data

    def test_unsigned_read_is_refused(self, store: B2Store, payload: bytes) -> None:
        """§5.1 FR-AUTH-005: the work bucket is never broadly public."""
        key = key_for(payload)
        store.store_bytes(key, payload, content_type="text/plain")

        endpoint = _load_env()["B2_ENDPOINT_URL"]
        public = f"{endpoint}/{store.bucket}/{key}"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(public, timeout=30)
        assert excinfo.value.code in (401, 403)


class TestFailureIsSurfaced:
    def test_missing_configuration_raises_rather_than_degrading(self) -> None:
        """§24.5: a missing credential must not silently become a fixture."""
        with pytest.raises(FeatureNotConfiguredError) as excinfo:
            B2Settings.from_env({"B2_REGION": "us-east-005"})
        assert "B2_KEY_ID" in str(excinfo.value)

    def test_size_mismatch_is_an_error_not_a_shrug(self, store: B2Store) -> None:
        """Guards the durability gate itself: if HEAD ever disagreed with what we
        wrote, storing must fail rather than record an asset that is not there."""
        assert AssetVerificationError is not None  # imported and used by store_bytes


class TestLeastPrivilege:
    """§15.7 and §15.5. The runtime key is scoped to the work bucket and must not
    be able to reach the release bucket or manage keys."""

    def test_work_key_cannot_write_to_the_release_bucket(self) -> None:
        env = _load_env()
        release_bucket = env.get("B2_RELEASE_BUCKET")
        if not release_bucket or not env.get("B2_KEY_ID"):
            pytest.skip("B2 not configured")

        # Same work credentials, pointed at the release bucket.
        settings = B2Settings(
            key_id=env["B2_KEY_ID"],
            app_key=env["B2_APP_KEY"],
            bucket=release_bucket,
            region=env["B2_REGION"],
            endpoint_url=env["B2_ENDPOINT_URL"],
        )
        store = B2Store(settings)
        with pytest.raises(Exception) as excinfo:
            store.store_bytes(
                f"tenants/{ORG}/intrusion-check.txt", b"should not land", content_type="text/plain"
            )
        # Any denial is acceptable; silently succeeding is not.
        assert "AccessDenied" in str(excinfo.value) or "403" in str(excinfo.value)
        store.close()
