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
MAX_NARRATION_INPUT_BYTES = 50 * 1_048_576


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


def extract_poster_frame(
    data: bytes,
    *,
    temp_root: Path,
    at_seconds: float = 0.8,
    timeout_seconds: int = 30,
) -> bytes:
    """Decode a single frame of a clip as PNG bytes.

    Used to give a video tile something to show. Seeking a little way in rather
    than to frame zero is deliberate: several ORBIT clips open on a near-black
    frame, and a black rectangle reads as a broken tile rather than a video.

    Same shape as the other ffmpeg callers here — fixed argv, no shell, a bounded
    timeout, and output size capped so a hostile file cannot flood the log.
    """
    if not data:
        raise InvalidSourceError("Poster extraction requires clip bytes.")
    if at_seconds < 0:
        raise InvalidSourceError("Poster timestamp must not be negative.")
    root = temp_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, source_name = tempfile.mkstemp(dir=root, prefix="poster-source-", suffix=".video")
    source = Path(source_name)
    output = root / f"poster-{source.stem}.png"
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file",
            "-ss",
            f"{at_seconds:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(output),
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv and validated local paths
                command,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidSourceError("Poster extraction did not complete safely.") from exc
        if len(result.stdout) + len(result.stderr) > MAX_PROBE_OUTPUT_BYTES:
            raise InvalidSourceError("Poster extraction output exceeded the safety limit.")
        if result.returncode != 0 or not output.exists():
            raise InvalidSourceError("Clip could not be decoded into a poster frame.")
        return output.read_bytes()
    finally:
        source.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def normalize_narration_bytes(
    data: bytes,
    *,
    temp_root: Path,
    timeout_seconds: int = 60,
) -> bytes:
    """Convert a decoded local audio input into the ORBIT 48 kHz mono WAV contract."""
    if not data or len(data) > MAX_NARRATION_INPUT_BYTES:
        raise InvalidSourceError("Narration input exceeds the media safety limit.")
    if detect_mime(data) not in {"audio/wav", "audio/mpeg", "audio/flac"}:
        raise InvalidSourceError("Narration input is not a supported audio format.")
    root = temp_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, source_name = tempfile.mkstemp(
        dir=root, prefix="narration-source-", suffix=".audio"
    )
    source = Path(source_name)
    output = root / f"narration-normalized-{source.stem}.wav"
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv and validated local paths
                command,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidSourceError(
                "Narration normalization did not complete within the safety limit."
            ) from exc
        if len(result.stdout) + len(result.stderr) > MAX_PROBE_OUTPUT_BYTES:
            raise InvalidSourceError("Narration normalization output exceeded the safety limit.")
        if result.returncode != 0 or not output.is_file():
            raise InvalidSourceError("Narration audio could not be normalized.")
        normalized = output.read_bytes()
        probe = probe_media_path(output, temp_root=root, timeout_seconds=timeout_seconds)
        if probe.media_kind != "AUDIO" or probe.sample_rate != 48_000 or probe.channels != 1:
            raise InvalidSourceError("Normalized narration does not satisfy the 48 kHz mono spec.")
        return normalized
    finally:
        source.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


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
