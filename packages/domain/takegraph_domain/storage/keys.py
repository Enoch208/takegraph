"""B2 object key layout (PRD §15.2).

Keys are derived, never taken from user input. §15.2 is explicit: "Never use
unsanitized user filenames as complete object keys. Preserve the original name as
metadata after removing control characters."

Content-addressed keys give deduplication for free (§5.7 FR-ASSET-002): identical
bytes produce an identical key, so a second upload of the same file costs one HEAD
rather than a transfer, and cross-build reuse resolves to the same object.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
import uuid

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: Extensions we are willing to put in a key. Anything else is stored without one
#: — the MIME type is authoritative and §17.3 warns not to trust the extension.
SAFE_EXTENSIONS = frozenset(
    {
        "png", "jpg", "jpeg", "webp", "gif", "svg",
        "mp4", "mov", "webm", "m4v",
        "wav", "mp3", "flac", "aac", "m4a",
        "json", "txt", "vtt", "srt", "jsonl", "parquet",
    }
)  # fmt: skip


class InvalidObjectKeyError(ValueError):
    """A key could not be built safely. Raised rather than sanitised into
    something surprising, because a silently rewritten key means an object the
    database references does not exist."""


def assert_sha256(value: str) -> str:
    if not SHA256_PATTERN.match(value):
        raise InvalidObjectKeyError(
            "expected a lowercase 64-character SHA-256 hex digest; "
            "keys are content-addressed and a malformed digest would collide"
        )
    return value


def safe_extension(filename: str | None, *, mime_type: str | None = None) -> str:
    """Return a safe extension, or empty string.

    Never trusts the supplied name for anything but a hint — §19.3 requires
    filenames to be normalised and path components ignored.
    """
    candidate = ""
    if filename:
        candidate = posixpath.splitext(posixpath.basename(filename))[1].lstrip(".").lower()
    if candidate not in SAFE_EXTENSIONS:
        candidate = ""
    if not candidate and mime_type:
        guess = mime_type.split("/")[-1].split(";")[0].strip().lower()
        candidate = guess if guess in SAFE_EXTENSIONS else ""
    return candidate


def content_address(
    *, organization_id: uuid.UUID | str, sha256: str, extension: str = "", prefix: str = "tenants"
) -> str:
    """`{prefix}/{org}/cas/sha256/{h0h1}/{h2h3}/{sha256}.{ext}` (§15.2).

    The two fan-out levels keep any single directory listing small, which matters
    because B2 list operations are paged and a flat CAS prefix degrades badly.
    """
    digest = assert_sha256(sha256)
    suffix = f".{extension}" if extension else ""
    return f"{prefix}/{organization_id}/cas/sha256/{digest[0:2]}/{digest[2:4]}/{digest}{suffix}"


def source_key(
    *,
    organization_id: uuid.UUID | str,
    project_id: uuid.UUID | str,
    source_id: uuid.UUID | str,
    version_id: uuid.UUID | str,
    filename: str,
    prefix: str = "tenants",
) -> str:
    return (
        f"{prefix}/{organization_id}/projects/{project_id}/sources/"
        f"{source_id}/{version_id}/{sanitize_filename(filename)}"
    )


def build_artifact_key(
    *,
    organization_id: uuid.UUID | str,
    project_id: uuid.UUID | str,
    build_id: uuid.UUID | str,
    artifact: str,
    prefix: str = "tenants",
) -> str:
    return f"{prefix}/{organization_id}/projects/{project_id}/builds/{build_id}/{artifact}"


def release_key(
    *,
    organization_id: uuid.UUID | str,
    project_id: uuid.UUID | str,
    release_id: uuid.UUID | str,
    logical_path: str,
    prefix: str = "tenants",
) -> str:
    return (
        f"{prefix}/{organization_id}/projects/{project_id}/releases/"
        f"{release_id}/{sanitize_filename(logical_path, allow_slashes=True)}"
    )


def temporary_upload_key(*, upload_id: uuid.UUID | str, filename: str) -> str:
    """Quarantine prefix. §19.3 keeps uploads quarantined until validation
    completes, and §15.6 expires this prefix on a short lifecycle rule."""
    return f"temporary/uploads/{upload_id}/{sanitize_filename(filename)}"


def sanitize_filename(name: str, *, allow_slashes: bool = False) -> str:
    """Strip path components and control characters.

    Directory traversal is the obvious risk, but the subtler one is a control
    character or newline in a key, which corrupts logs and manifest rendering
    downstream. Both are removed here rather than at each call site.
    """
    normalized = unicodedata.normalize("NFC", name)
    normalized = "".join(c for c in normalized if unicodedata.category(c)[0] != "C")

    if allow_slashes:
        parts = [p for p in normalized.split("/") if p not in ("", ".", "..")]
        cleaned = "/".join(re.sub(r"[^A-Za-z0-9._-]", "_", p) for p in parts)
    else:
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(normalized))

    cleaned = cleaned.strip("._") or "unnamed"
    # Bound the length: B2 keys cap at 1024 bytes and long names break UI layout.
    return cleaned[:200]
