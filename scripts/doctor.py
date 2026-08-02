"""Truthful local readiness checks for the TAKEGRAPH demo dependencies.

The command never prints credential values, signed URLs, provider bodies, or
connection strings. A configured-but-rejected credential is a failure; it never
falls back to a fixture (PRD §24.5).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import httpx
import psycopg
import redis
from takegraph_domain.errors import FeatureNotConfiguredError
from takegraph_infrastructure.b2 import B2Settings, B2Store


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def load_local_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def command_check(name: str, command: list[str]) -> Check:
    if shutil.which(command[0]) is None:
        return Check(name, False, "not installed")
    try:
        result = subprocess.run(  # noqa: S603 — every argv list is defined in this module
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Check(name, False, "probe failed")
    first_line = (result.stdout or result.stderr).splitlines()[0][:100]
    return Check(name, result.returncode == 0, first_line or "available")


def database_check() -> Check:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return Check("PostgreSQL", False, "NOT_CONFIGURED")
    sync_url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        with psycopg.connect(sync_url, connect_timeout=5) as connection:
            major = int(connection.info.server_version // 10000)
            minor = connection.info.server_version // 100 % 100
            detail = f"server {major}.{minor}"
            return Check("PostgreSQL", major >= 16, detail)
    except psycopg.Error:
        return Check("PostgreSQL", False, "unreachable or rejected")


def redis_check() -> Check:
    url = os.environ.get("REDIS_URL", "")
    if not url:
        return Check("Redis", False, "NOT_CONFIGURED")
    client: redis.Redis | None = None
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
        ok = bool(client.ping())
        return Check("Redis", ok, "PING ok" if ok else "PING failed")
    except redis.RedisError:
        return Check("Redis", False, "unreachable or rejected")
    finally:
        if client is not None:
            client.close()


def storage_check() -> Check:
    try:
        settings = B2Settings.from_env(dict(os.environ))
    except FeatureNotConfiguredError:
        return Check("Backblaze work storage", False, "NOT_CONFIGURED")
    store = B2Store(settings)
    try:
        ok = store.probe()
        return Check("Backblaze work storage", ok, "list probe ok" if ok else "probe failed")
    finally:
        store.close()


def gmi_checks() -> list[Check]:
    key = os.environ.get("GMI_API_KEY", "")
    image_model = os.environ.get("GMI_IMAGE_MODEL", "")
    video_models = [
        os.environ.get("GMI_VIDEO_MODEL", ""),
        os.environ.get("GMI_VIDEO_FALLBACK_MODEL", ""),
    ]
    if not key:
        return [Check("GMI Cloud", False, "NOT_CONFIGURED")]
    try:
        response = httpx.get(
            "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
    except httpx.HTTPError:
        return [Check("GMI Cloud auth", False, "credential rejected or service unavailable")]
    if response.status_code in (401, 403):
        return [Check("GMI Cloud auth", False, "credential rejected")]
    if not response.is_success:
        return [Check("GMI Cloud auth", False, f"HTTP {response.status_code}")]
    payload = response.json()
    model_ids = payload.get("model_ids") if isinstance(payload, dict) else None
    if not isinstance(model_ids, list) or not all(isinstance(value, str) for value in model_ids):
        return [Check("GMI Cloud auth", False, "unexpected catalog response")]
    available = set(model_ids)

    checks: list[Check] = [Check("GMI Cloud auth", True, "accepted")]
    for label, model_id in (
        ("GMI image model", image_model),
        ("GMI video model", video_models[0]),
        ("GMI fallback model", video_models[1]),
    ):
        if not model_id:
            checks.append(Check(label, False, "NOT_CONFIGURED"))
            continue
        present = model_id in available
        checks.append(
            Check(label, present, "ok_authoritative" if present else "configured model missing")
        )
    return checks


def provider_catalog_check(
    *,
    name: str,
    url: str,
    headers: dict[str, str],
    model_env: str,
    list_key: str | None,
    id_key: str,
) -> Check:
    if not all(headers.values()):
        return Check(name, False, "NOT_CONFIGURED")
    try:
        response = httpx.get(url, headers=headers, timeout=10)
    except httpx.HTTPError:
        return Check(name, False, "unreachable")
    if response.status_code in (401, 403):
        return Check(name, False, "credential rejected")
    if not response.is_success:
        return Check(name, False, f"HTTP {response.status_code}")
    payload = response.json()
    rows = payload.get(list_key, []) if list_key else payload
    if not isinstance(rows, list):
        return Check(name, False, "unexpected catalog response")
    configured_model = os.environ.get(model_env, "")
    if not configured_model:
        return Check(name, False, f"{model_env} is empty")
    ids = {row.get(id_key) for row in rows if isinstance(row, dict)}
    return Check(
        name,
        configured_model in ids,
        "configured model available" if configured_model in ids else "configured model missing",
    )


def elevenlabs_check() -> Check:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    return provider_catalog_check(
        name="ElevenLabs TTS",
        url="https://api.elevenlabs.io/v1/models",
        headers={"xi-api-key": key},
        model_env="ELEVENLABS_TTS_MODEL",
        list_key=None,
        id_key="model_id",
    )


def elevenlabs_music_check() -> Check:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    configured_model = os.environ.get("ELEVENLABS_MUSIC_MODEL", "")
    if not key or not configured_model:
        return Check("ElevenLabs music", False, "NOT_CONFIGURED")

    # ElevenLabs serves Music through a dedicated API and does not include its
    # model IDs in GET /v1/models. The pinned SDK's generated Music contract
    # accepts these two IDs; validate that contract separately, then use the
    # subscription endpoint as a non-billable live credential probe. A real
    # generation smoke test remains the authoritative capability proof.
    supported_models = {"music_v1", "music_v2"}
    if configured_model not in supported_models:
        return Check("ElevenLabs music", False, "configured model unsupported by SDK")
    try:
        response = httpx.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": key},
            timeout=10,
        )
    except httpx.HTTPError:
        return Check("ElevenLabs music", False, "unreachable")
    if response.status_code in (401, 403):
        return Check("ElevenLabs music", False, "credential rejected")
    if not response.is_success:
        return Check("ElevenLabs music", False, f"HTTP {response.status_code}")
    return Check("ElevenLabs music", True, f"{configured_model} live-probed")


def anthropic_check() -> Check:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return provider_catalog_check(
        name="Anthropic evaluator",
        url="https://api.anthropic.com/v1/models?limit=100",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        model_env="EVALUATOR_MODEL",
        list_key="data",
        id_key="id",
    )


def main() -> int:
    load_local_env()
    checks = [
        Check("Python", sys.version_info[:2] == (3, 12), sys.version.split()[0]),
        command_check("Node", ["node", "--version"]),
        command_check("pnpm", ["pnpm", "--version"]),
        command_check("uv", ["uv", "--version"]),
        command_check("FFmpeg", ["ffmpeg", "-version"]),
        command_check("ffprobe", ["ffprobe", "-version"]),
        Check("genblaze-core", True, version("genblaze-core")),
        Check("genblaze-s3", True, version("genblaze-s3")),
        Check("genblaze-gmicloud", True, version("genblaze-gmicloud")),
        database_check(),
        redis_check(),
        storage_check(),
        *gmi_checks(),
        elevenlabs_check(),
        elevenlabs_music_check(),
        anthropic_check(),
    ]
    for check in checks:
        label = "PASS" if check.ok else ("INFO" if not check.required else "FAIL")
        print(f"{label:4}  {check.name:<28} {check.detail}")

    b2_events = os.environ.get("B2_EVENT_NOTIFICATIONS_STATUS", "unavailable").upper()
    object_lock = os.environ.get("B2_OBJECT_LOCK_MODE", "DISABLED").upper()
    print(f"INFO  {'B2 Event Notifications':<28} {b2_events}")
    print(f"INFO  {'B2 Object Lock':<28} {object_lock}")

    failed = [check.name for check in checks if check.required and not check.ok]
    if failed:
        print(f"\nReadiness failed: {', '.join(failed)}")
        return 1
    print("\nReadiness passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
