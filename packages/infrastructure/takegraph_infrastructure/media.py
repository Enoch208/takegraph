"""Magic-byte detection and bounded ffprobe wrapper (PRD §16.2, §19.3)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from takegraph_domain.errors import InvalidSourceError

MAX_PROBE_OUTPUT_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class MediaProbe:
    media_kind: Literal["IMAGE", "VIDEO", "AUDIO"]
    format_name: str
    codec_names: tuple[str, ...]
    width: int | None
    height: int | None
    duration_ms: int | None
    frame_rate: str | None
    has_audio: bool
    sample_rate: int | None
    channels: int | None


def detect_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/quicktime" if data[8:12] == b"qt  " else "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    raise InvalidSourceError("Uploaded bytes are not a supported media format.")


def probe_media_bytes(
    data: bytes,
    *,
    suffix: str,
    temp_root: Path,
    timeout_seconds: int = 15,
) -> MediaProbe:
    root = temp_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=root, prefix="probe-", suffix=suffix)
    path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return probe_media_path(path, temp_root=root, timeout_seconds=timeout_seconds)
    finally:
        path.unlink(missing_ok=True)


def probe_media_path(
    path: Path,
    *,
    temp_root: Path,
    timeout_seconds: int = 15,
) -> MediaProbe:
    root = temp_root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or path.is_symlink() or not resolved.is_file():
        raise InvalidSourceError("Media probe path is outside the configured temporary root.")

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        (
            "format=format_name,duration:"
            "stream=codec_type,codec_name,width,height,avg_frame_rate,sample_rate,channels,nb_frames"
        ),
        "-of",
        "json",
        str(resolved),
    ]
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv; path is validated above
            command,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidSourceError("Media probe did not complete within the safety limit.") from exc
    if len(result.stdout) + len(result.stderr) > MAX_PROBE_OUTPUT_BYTES:
        raise InvalidSourceError("Media probe output exceeded the safety limit.")
    if result.returncode != 0:
        raise InvalidSourceError("Uploaded media could not be decoded.")
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidSourceError("Media probe returned an invalid result.") from exc
    return _parse_probe(payload)


def _parse_probe(payload: Any) -> MediaProbe:
    if not isinstance(payload, dict):
        raise InvalidSourceError("Media probe returned an invalid result.")
    streams = payload.get("streams")
    format_data = payload.get("format")
    if not isinstance(streams, list) or not streams or not isinstance(format_data, dict):
        raise InvalidSourceError("Uploaded media has no decodable streams.")
    typed_streams = [stream for stream in streams if isinstance(stream, dict)]
    video_streams = [stream for stream in typed_streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in typed_streams if stream.get("codec_type") == "audio"]
    if video_streams:
        primary = video_streams[0]
        frame_count = _optional_int(primary.get("nb_frames"))
        duration_ms = _duration_ms(format_data.get("duration"))
        is_image = duration_ms is None and frame_count in (None, 1)
        media_kind: Literal["IMAGE", "VIDEO", "AUDIO"] = "IMAGE" if is_image else "VIDEO"
    elif audio_streams:
        primary = audio_streams[0]
        duration_ms = _duration_ms(format_data.get("duration"))
        media_kind = "AUDIO"
    else:
        raise InvalidSourceError("Uploaded media has no supported audio or video stream.")

    if media_kind in {"VIDEO", "AUDIO"} and (duration_ms is None or duration_ms <= 0):
        raise InvalidSourceError("Uploaded media has no positive duration.")
    width = _optional_int(primary.get("width"))
    height = _optional_int(primary.get("height"))
    if media_kind in {"IMAGE", "VIDEO"} and (
        width is None or height is None or width <= 0 or height <= 0
    ):
        raise InvalidSourceError("Uploaded visual media has no decodable frame dimensions.")
    if media_kind == "VIDEO" and _optional_int(primary.get("nb_frames")) == 0:
        raise InvalidSourceError("Uploaded video has no frames.")
    return MediaProbe(
        media_kind=media_kind,
        format_name=str(format_data.get("format_name") or "unknown"),
        codec_names=tuple(
            str(stream["codec_name"])
            for stream in typed_streams
            if stream.get("codec_name") is not None
        ),
        width=width,
        height=height,
        duration_ms=duration_ms,
        frame_rate=_optional_string(primary.get("avg_frame_rate")),
        has_audio=bool(audio_streams),
        sample_rate=_optional_int(audio_streams[0].get("sample_rate")) if audio_streams else None,
        channels=_optional_int(audio_streams[0].get("channels")) if audio_streams else None,
    )


def _duration_ms(value: Any) -> int | None:
    if value in (None, "N/A"):
        return None
    try:
        return int(Decimal(str(value)) * 1000)
    except (InvalidOperation, ValueError) as exc:
        raise InvalidSourceError("Media duration is invalid.") from exc


def _optional_int(value: Any) -> int | None:
    if value in (None, "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidSourceError("Media stream metadata is invalid.") from exc


def _optional_string(value: Any) -> str | None:
    return None if value in (None, "N/A", "0/0") else str(value)
