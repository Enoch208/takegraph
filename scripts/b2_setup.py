"""Idempotent Backblaze B2 setup (PRD §15.1, §15.6, §15.7).

Run once per environment with the *admin* key. It creates the work and release
buckets, configures CORS and lifecycle rules, and mints two least-privilege
runtime keys.

Why this exists as a script rather than console clicks: §15.6 requires an
idempotent setup script, and Object Lock can only be enabled at bucket creation
time — discovering that after the release bucket already exists means deleting
and recreating it.

Security shape, from §15.7:

    admin key   -> this script only, never application runtime
    work key    -> work bucket, read/write/list, no delete-bucket, no governance
    release key -> release bucket, write + retention, for publishing only

§15.5 additionally forbids the runtime key from holding `bypassGovernance`. The
admin key has it; neither minted key does.

    B2_ADMIN_KEY_ID=... B2_ADMIN_APP_KEY=... uv run python scripts/b2_setup.py

Secrets are printed exactly once, to stdout, for the operator to paste into .env.
They are never written to a file by this script.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

WORK_BUCKET = os.environ.get("B2_WORK_BUCKET", "takegraph-work-005")
RELEASE_BUCKET = os.environ.get("B2_RELEASE_BUCKET", "takegraph-release-005")

# §15.7 work runtime key: enough to store, read, list and clean up its own
# prefix. Deliberately excludes every bucket-level and key-management capability.
WORK_CAPABILITIES = [
    "listBuckets",
    "readBuckets",
    "listFiles",
    "readFiles",
    "shareFiles",
    "writeFiles",
    "deleteFiles",
]

# §15.7 release publisher key: writes release objects and applies retention.
# It can read retention back (§15.5 requires reading it back after publication)
# but cannot bypass governance to shorten it.
RELEASE_CAPABILITIES = [
    "listBuckets",
    "readBuckets",
    "listFiles",
    "readFiles",
    "shareFiles",
    "writeFiles",
    "readFileRetentions",
    "writeFileRetentions",
    "readBucketRetentions",
]

ALLOWED_ORIGINS = os.environ.get("B2_CORS_ORIGINS", "http://localhost:3000").split(",")


class SetupError(RuntimeError):
    pass


def _https(url: str) -> str:
    """B2 hands us apiUrl/s3ApiUrl in its authorize response, so the value is
    externally supplied. Pinning the scheme stops a redirected or spoofed
    response steering a request at file:// or a custom handler."""
    if not url.startswith("https://"):
        raise SetupError(f"refusing a non-HTTPS endpoint: {url[:60]!r}")
    return url


def _post(url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        _https(url),
        data=json.dumps(payload).encode(),
        headers={"Authorization": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            raise SetupError(f"{url} -> HTTP {exc.code}: {body[:400]}") from exc
        # Callers distinguish "already exists" from real failures.
        raise SetupError(json.dumps(parsed)) from exc


def authorize(key_id: str, app_key: str) -> tuple[str, str, str, str]:
    import base64

    credentials = base64.b64encode(f"{key_id}:{app_key}".encode()).decode()
    request = urllib.request.Request(
        _https("https://api.backblazeb2.com/b2api/v3/b2_authorize_account"),
        headers={"Authorization": f"Basic {credentials}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read())

    storage = data["apiInfo"]["storageApi"]
    return (
        data["authorizationToken"],
        storage["apiUrl"],
        storage["s3ApiUrl"],
        data["accountId"],
    )


def find_bucket(token: str, api_url: str, account_id: str, name: str) -> dict[str, Any] | None:
    result = _post(
        f"{api_url}/b2api/v3/b2_list_buckets",
        token,
        {"accountId": account_id, "bucketName": name},
    )
    buckets = result.get("buckets", [])
    return buckets[0] if buckets else None


def ensure_bucket(
    token: str, api_url: str, account_id: str, name: str, *, object_lock: bool
) -> dict[str, Any]:
    existing = find_bucket(token, api_url, account_id, name)
    if existing:
        locked = existing.get("fileLockConfiguration", {}).get("value", {}).get("isFileLockEnabled")
        print(f"  bucket {name}: exists (object lock: {locked})")
        if object_lock and not locked:
            # Object Lock cannot be turned on after creation, so say so loudly
            # rather than letting a release later fail its retention readback.
            print(
                f"  !! {name} exists WITHOUT object lock. §15.5 wants retention on "
                "published evidence. Delete and recreate it, or set "
                "B2_OBJECT_LOCK_MODE=DISABLED and accept NOT_CONFIGURED in the UI."
            )
        return existing

    payload: dict[str, Any] = {
        "accountId": account_id,
        "bucketName": name,
        # §15.1: both buckets are private. The work bucket must never be broadly
        # public (FR-AUTH-005); release assets are served through signed URLs.
        "bucketType": "allPrivate",
    }
    if object_lock:
        payload["fileLockEnabled"] = True

    created = _post(f"{api_url}/b2api/v3/b2_create_bucket", token, payload)
    print(f"  bucket {name}: created (object lock: {object_lock})")
    return created


def configure_work_bucket(token: str, api_url: str, bucket: dict[str, Any]) -> None:
    """CORS for browser presigned PUT, and lifecycle cleanup for scratch prefixes."""
    _post(
        f"{api_url}/b2api/v3/b2_update_bucket",
        token,
        {
            "accountId": bucket["accountId"],
            "bucketId": bucket["bucketId"],
            "corsRules": [
                {
                    "corsRuleName": "takegraphPresignedUpload",
                    # §15.3: CORS is scoped to the frontend origins that need it.
                    "allowedOrigins": ALLOWED_ORIGINS,
                    "allowedOperations": ["s3_put", "s3_head", "s3_get"],
                    "allowedHeaders": ["*"],
                    "exposeHeaders": ["etag", "x-amz-version-id"],
                    "maxAgeSeconds": 3600,
                }
            ],
            "lifecycleRules": [
                # §15.6: temporary upload and provider-fetch objects expire
                # quickly. Nothing here touches canonical or release prefixes.
                {
                    "fileNamePrefix": "temporary/",
                    "daysFromUploadingToHiding": 1,
                    "daysFromHidingToDeleting": 1,
                },
            ],
        },
    )
    print(f"  bucket {bucket['bucketName']}: CORS + lifecycle configured")


def create_key(
    token: str,
    api_url: str,
    account_id: str,
    *,
    name: str,
    bucket_id: str,
    capabilities: list[str],
) -> dict[str, Any]:
    return _post(
        f"{api_url}/b2api/v3/b2_create_key",
        token,
        {
            "accountId": account_id,
            "keyName": name,
            "capabilities": capabilities,
            "bucketId": bucket_id,
        },
    )


def main() -> int:
    admin_id = os.environ.get("B2_ADMIN_KEY_ID")
    admin_key = os.environ.get("B2_ADMIN_APP_KEY")
    if not admin_id or not admin_key:
        print(
            "B2_ADMIN_KEY_ID and B2_ADMIN_APP_KEY are required.\n"
            "These are the master/admin credentials and are used by this script "
            "only — never by the API or worker at runtime (§15.7).",
            file=sys.stderr,
        )
        return 2

    print("authorizing with the admin key...")
    token, api_url, s3_url, account_id = authorize(admin_id, admin_key)
    print(f"  account {account_id}")
    print(f"  s3 endpoint {s3_url}")

    print("\nensuring buckets...")
    work = ensure_bucket(token, api_url, account_id, WORK_BUCKET, object_lock=False)
    release = ensure_bucket(token, api_url, account_id, RELEASE_BUCKET, object_lock=True)

    print("\nconfiguring work bucket...")
    try:
        configure_work_bucket(token, api_url, work)
    except SetupError as exc:
        print(f"  !! could not configure work bucket: {exc}")

    print("\nminting least-privilege runtime keys...")
    work_key = create_key(
        token,
        api_url,
        account_id,
        name="takegraph-work-runtime",
        bucket_id=work["bucketId"],
        capabilities=WORK_CAPABILITIES,
    )
    release_key = create_key(
        token,
        api_url,
        account_id,
        name="takegraph-release-publisher",
        bucket_id=release["bucketId"],
        capabilities=RELEASE_CAPABILITIES,
    )

    for label, key in (("work", work_key), ("release", release_key)):
        caps = key["capabilities"]
        # §15.5 forbids a runtime key holding bypassGovernance. This is a real
        # check, not an assert — asserts vanish under `python -O`, and silently
        # minting an over-privileged key is exactly the failure to avoid.
        if "bypassGovernance" in caps:
            raise SetupError(
                f"{label} key was granted bypassGovernance, which §15.5 forbids for a "
                "runtime key. Refusing to write it to .env; delete the key in B2."
            )
        print(f"  {label} key: {len(caps)} capabilities, scoped to one bucket")

    region = s3_url.split("//")[1].split(".")[1]
    settings = {
        "B2_REGION": region,
        "B2_ENDPOINT_URL": s3_url,
        "B2_WORK_BUCKET": WORK_BUCKET,
        "B2_WORK_PREFIX": "tenants",
        "B2_KEY_ID": work_key["applicationKeyId"],
        "B2_APP_KEY": work_key["applicationKey"],
        "B2_RELEASE_BUCKET": RELEASE_BUCKET,
        "B2_RELEASE_PREFIX": "tenants",
        "B2_RELEASE_KEY_ID": release_key["applicationKeyId"],
        "B2_RELEASE_APP_KEY": release_key["applicationKey"],
    }

    if "--write-env" in sys.argv:
        # Writing beats printing: a printed secret ends up in shell history, CI
        # logs and terminal scrollback. .env is gitignored (§19.6).
        path = os.environ.get("ENV_FILE", ".env")
        existing: list[str] = []
        if os.path.exists(path):
            with open(path) as handle:
                existing = [
                    line
                    for line in handle.read().splitlines()
                    if line.split("=")[0] not in settings
                ]
        with open(path, "w") as handle:
            handle.write("\n".join(existing).rstrip("\n"))
            handle.write("\n\n# Backblaze B2 — written by scripts/b2_setup.py.\n")
            handle.write("# Runtime keys, each scoped to one bucket. Not the admin key.\n")
            for key, value in settings.items():
                handle.write(f"{key}={value}\n")
        os.chmod(path, 0o600)
        print(f"\nwrote {len(settings)} settings to {path} (mode 600). Secrets were not printed.")
    else:
        print("\nRe-run with --write-env to write these into .env without printing them.")
        for key in settings:
            if "APP_KEY" not in key:
                print(f"  {key}={settings[key]}")

    print("The admin key belongs in neither .env nor any runtime process.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
