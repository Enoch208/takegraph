"""Shared Backblaze B2 object store adapter (PRD §15).

Built on `genblaze_s3.S3StorageBackend`, which §14.4 prescribes and which is
already a pinned dependency — a second S3 client would mean two auth paths and
two retry policies to reason about.

The rule that governs this module is §8.3.7: `assets.sha256` is SHA-256 of the
bytes *we* stored, never a provider-supplied claim. So `store_bytes` hashes what
it is given, `verify` re-reads and re-hashes, and nothing here accepts a caller's
word about content.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from genblaze_s3 import S3StorageBackend
from takegraph_domain.errors import AssetVerificationError, FeatureNotConfiguredError
from takegraph_domain.storage.keys import assert_sha256


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    sha256: str
    size_bytes: int
    content_type: str
    version_id: str | None
    deduplicated: bool
    """True when identical bytes were already present, so nothing was uploaded."""


@dataclass(frozen=True, slots=True)
class ObjectHead:
    key: str
    size_bytes: int
    content_type: str | None
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class B2Settings:
    key_id: str
    app_key: str
    bucket: str
    region: str
    endpoint_url: str
    prefix: str = "tenants"

    @classmethod
    def from_env(cls, env: dict[str, str], *, release: bool = False) -> B2Settings:
        """Build settings, or fail with a named missing variable.

        §24.5 forbids a missing credential from silently becoming a working
        fixture, so this raises FeatureNotConfiguredError rather than returning a
        half-configured object that fails later and further away.
        """
        prefix = "B2_RELEASE_" if release else "B2_"
        key_id = env.get(f"{prefix}KEY_ID")
        app_key = env.get(f"{prefix}APP_KEY")
        bucket = env.get(f"{prefix}BUCKET" if release else "B2_WORK_BUCKET")
        region = env.get("B2_REGION")
        endpoint = env.get("B2_ENDPOINT_URL")

        missing = [
            name
            for name, value in {
                f"{prefix}KEY_ID": key_id,
                f"{prefix}APP_KEY": app_key,
                ("B2_RELEASE_BUCKET" if release else "B2_WORK_BUCKET"): bucket,
                "B2_REGION": region,
                "B2_ENDPOINT_URL": endpoint,
            }.items()
            if not value
        ]
        if missing:
            raise FeatureNotConfiguredError(
                f"Backblaze storage is not configured: missing {', '.join(missing)}.",
                details={"missing": missing},
            )

        # `missing` above already proves these are non-empty; repeating the
        # narrowing keeps the type checker satisfied without an assert, which
        # would vanish under `python -O`.
        return cls(
            key_id=str(key_id),
            app_key=str(app_key),
            bucket=str(bucket),
            region=str(region),
            endpoint_url=str(endpoint),
            prefix=env.get(f"{prefix}PREFIX" if release else "B2_WORK_PREFIX") or "tenants",
        )


class B2Store:
    """Thin, explicit wrapper. Every method maps to one S3 operation so failures
    are attributable and timeouts land where §20.2 expects them."""

    def __init__(self, settings: B2Settings, *, preflight: bool = False) -> None:
        self._settings = settings
        # AGENTS.md gotcha: preflight=True is the SDK default and makes a live B2
        # call during construction. Off by default here so importing or
        # instantiating in a credential-less environment cannot break startup;
        # readiness checks call `probe()` deliberately instead.
        self._backend = S3StorageBackend.for_backblaze(
            settings.bucket,
            region=settings.region,
            key_id=settings.key_id,
            app_key=settings.app_key,
            preflight=preflight,
        )

    @property
    def bucket(self) -> str:
        return self._settings.bucket

    @property
    def prefix(self) -> str:
        return self._settings.prefix

    def probe(self) -> bool:
        """Cheap reachability check for /health/ready. Returns False rather than
        raising so one unavailable dependency does not take down the endpoint that
        reports on it (§20.6)."""
        try:
            self._backend.list(prefix="__probe__/", max_keys=1)
        except Exception:  # noqa: BLE001 — reported as not-ready, never as healthy
            return False
        return True

    def store_bytes(
        self, key: str, data: bytes, *, content_type: str, metadata: dict[str, str] | None = None
    ) -> StoredObject:
        """Hash, upload if absent, then confirm with a HEAD.

        Content-addressed keys mean an existing object with the same key already
        holds identical bytes, so the upload is skipped — that is the dedupe path
        in §5.7 FR-ASSET-002. The HEAD still runs either way: §8.3.6 says an asset
        is not durable until B2 confirms the expected size.
        """
        digest = hashlib.sha256(data).hexdigest()

        existing = self._backend.head(key)
        deduplicated = existing is not None and existing.size == len(data)
        if not deduplicated:
            self._backend.put(key, data, content_type=content_type, metadata=metadata or {})

        confirmed = self._backend.head(key)
        if confirmed is None:
            raise AssetVerificationError(
                f"object {key} is not present after upload; treating as not durable",
                details={"key": key},
            )
        if confirmed.size != len(data):
            raise AssetVerificationError(
                f"stored size {confirmed.size} does not match the {len(data)} bytes written",
                details={"key": key, "expected": len(data), "actual": confirmed.size},
            )

        return StoredObject(
            key=key,
            sha256=digest,
            size_bytes=len(data),
            content_type=content_type,
            version_id=(confirmed.metadata or {}).get("version_id"),
            deduplicated=deduplicated,
        )

    def verify(self, key: str, *, expected_sha256: str) -> bool:
        """Re-read the object and re-hash it (§12.3 reuse proof, §5.8 FR-REL-003).

        This is what lets a release claim its assets are verified: the bytes are
        fetched back from B2 and hashed, so the claim rests on stored content
        rather than on a record written at upload time.
        """
        assert_sha256(expected_sha256)
        data = self._backend.get(key)
        return hashlib.sha256(data).hexdigest() == expected_sha256

    def exists(self, key: str) -> bool:
        return self._backend.exists(key)

    def head_size(self, key: str) -> int | None:
        meta = self._backend.head(key)
        return None if meta is None else meta.size

    def head(self, key: str) -> ObjectHead | None:
        meta = self._backend.head(key)
        if meta is None:
            return None
        return ObjectHead(
            key=meta.key,
            size_bytes=meta.size,
            content_type=meta.content_type,
            metadata=meta.metadata,
        )

    def get_bytes(self, key: str) -> bytes:
        return self._backend.get(key)

    def key_from_url(self, url: str) -> str | None:
        """Resolve only URLs belonging to this configured backend."""
        return self._backend.key_from_url(url)

    def presign_put(self, key: str, *, content_type: str, ttl_seconds: int = 900) -> str:
        """§15.3: presigned S3 PUT, never a browser form POST, and short-lived so a
        leaked URL ages out quickly."""
        return self._backend.presigned_put_url(
            key, expires_in=ttl_seconds, content_type=content_type
        )

    def presign_get(self, key: str, *, ttl_seconds: int = 900) -> str:
        """§5.7 FR-ASSET-006: authorization happens before this is called, and the
        resulting URL is never persisted (§15.3) — it is generated on demand."""
        return self._backend.presigned_get_url(key, expires_in=ttl_seconds)

    def delete(self, key: str) -> None:
        """Only for temporary and rejected-candidate prefixes. Evidence is never
        deleted through the product (§8.1)."""
        self._backend.delete(key)

    def close(self) -> None:
        self._backend.close()
