"""Local-only FFmpeg composition for the ORBIT delivery package."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from takegraph_domain.errors import InvalidSourceError

from takegraph_infrastructure.media import MediaProbe, probe_media_path

MAX_INPUT_BYTES = 500 * 1024 * 1024
MAX_PROCESS_OUTPUT_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class DeliveryInput:
    clips: tuple[bytes, bytes, bytes, bytes]
    narration: bytes
    music: bytes
    end_card: bytes
    captions: tuple[str, ...]
    legal_line: str


@dataclass(frozen=True, slots=True)
class DeliveryArtifact:
    role: str
    filename: str
    mime_type: str
    media_kind: str
    data: bytes
    metadata: dict[str, object]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def compose_delivery_package(
    source: DeliveryInput,
    *,
    temp_root: Path,
    width: int = 1920,
    height: int = 1080,
    timeout_seconds: int = 300,
) -> tuple[DeliveryArtifact, ...]:
    """Create both masters and proof artifacts without allowing network inputs."""
    if len(source.clips) != 4:
        raise InvalidSourceError("Delivery requires exactly four video clips.")
    if width < 320 or height < 180 or width % 2 or height % 2:
        raise InvalidSourceError("Delivery dimensions must be even and at least 320x180.")
    blobs = (*source.clips, source.narration, source.music, source.end_card)
    if any(not blob or len(blob) > MAX_INPUT_BYTES for blob in blobs):
        raise InvalidSourceError("Delivery input is empty or exceeds the media safety limit.")
    root = temp_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="delivery-", dir=root) as temporary:
        work = Path(temporary).resolve()
        clip_paths = tuple(work / f"clip-{index}.mp4" for index in range(1, 5))
        for path, data in zip(clip_paths, source.clips, strict=True):
            _write_private(path, data)
        narration_path = work / "narration.audio"
        music_path = work / "music.audio"
        end_card_path = work / "end-card.png"
        _write_private(narration_path, source.narration)
        _write_private(music_path, source.music)
        _write_private(end_card_path, source.end_card)

        normalized: list[Path] = []
        for index, clip in enumerate(clip_paths, start=1):
            output = work / f"normalized-{index}.mp4"
            _run_ffmpeg(
                [
                    "-i",
                    str(clip),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-vf",
                    _fit_filter(width, height),
                    "-r",
                    "30",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    str(output),
                ],
                work=work,
                timeout_seconds=timeout_seconds,
            )
            normalized.append(output)
        end_video = work / "normalized-end-card.mp4"
        _run_ffmpeg(
            [
                "-loop",
                "1",
                "-i",
                str(end_card_path),
                "-t",
                "2",
                "-vf",
                _fit_filter(width, height),
                "-r",
                "30",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                str(end_video),
            ],
            work=work,
            timeout_seconds=timeout_seconds,
        )
        normalized.append(end_video)
        concat_list = work / "concat.txt"
        _write_private(
            concat_list,
            "".join(f"file '{path.name}'\n" for path in normalized).encode(),
        )
        picture = work / "picture.mp4"
        _run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                str(picture),
            ],
            work=work,
            timeout_seconds=timeout_seconds,
        )
        picture_probe = probe_media_path(picture, temp_root=root)
        if picture_probe.duration_ms is None or picture_probe.duration_ms <= 0:
            raise InvalidSourceError("Composed picture has no positive duration.")
        duration_seconds = picture_probe.duration_ms / 1000
        final_audio = work / "final_audio.wav"
        _run_ffmpeg(
            [
                "-i",
                str(music_path),
                "-i",
                str(narration_path),
                "-filter_complex",
                (
                    "[0:a]aresample=48000,volume=0.25,apad[music];"
                    "[1:a]aresample=48000,volume=1.0,apad[voice];"
                    "[music][voice]amix=inputs=2:duration=longest:normalize=0,"
                    "loudnorm=I=-16:TP=-1.5:LRA=11[mix]"
                ),
                "-map",
                "[mix]",
                "-t",
                f"{duration_seconds:.3f}",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(final_audio),
            ],
            work=work,
            timeout_seconds=timeout_seconds,
        )
        master_16x9 = work / "master_16x9.mp4"
        _run_ffmpeg(
            [
                "-i",
                str(picture),
                "-i",
                str(final_audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(master_16x9),
            ],
            work=work,
            timeout_seconds=timeout_seconds,
        )
        portrait_width, portrait_height = height, width
        master_9x16 = work / "master_9x16.mp4"
        _run_ffmpeg(
            [
                "-i",
                str(master_16x9),
                "-vf",
                (
                    f"scale={portrait_width}:{portrait_height}:"
                    "force_original_aspect_ratio=increase,"
                    f"crop={portrait_width}:{portrait_height}"
                ),
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                str(master_9x16),
            ],
            work=work,
            timeout_seconds=timeout_seconds,
        )
        thumb_16x9 = work / "thumbnail_16x9.jpg"
        thumb_9x16 = work / "thumbnail_9x16.jpg"
        for master, thumbnail in (
            (master_16x9, thumb_16x9),
            (master_9x16, thumb_9x16),
        ):
            _run_ffmpeg(
                ["-i", str(master), "-frames:v", "1", "-q:v", "2", str(thumbnail)],
                work=work,
                timeout_seconds=timeout_seconds,
            )
        captions_path = work / "captions.vtt"
        _write_private(
            captions_path,
            _webvtt(source.captions, source.legal_line, picture_probe.duration_ms).encode(),
        )
        media_specs = (
            ("master_16x9", master_16x9, "video/mp4", "VIDEO"),
            ("master_9x16", master_9x16, "video/mp4", "VIDEO"),
            ("final_audio", final_audio, "audio/wav", "AUDIO"),
            ("thumbnail_16x9", thumb_16x9, "image/jpeg", "IMAGE"),
            ("thumbnail_9x16", thumb_9x16, "image/jpeg", "IMAGE"),
        )
        artifacts: list[DeliveryArtifact] = []
        report_outputs: list[dict[str, object]] = []
        for role, path, mime, kind in media_specs:
            probe = probe_media_path(path, temp_root=root)
            data = path.read_bytes()
            metadata = _probe_metadata(probe)
            artifacts.append(DeliveryArtifact(role, path.name, mime, kind, data, metadata))
            report_outputs.append(
                {"role": role, "sha256": hashlib.sha256(data).hexdigest(), **metadata}
            )
        caption_data = captions_path.read_bytes()
        artifacts.append(
            DeliveryArtifact(
                "captions",
                captions_path.name,
                "text/vtt",
                "DOCUMENT",
                caption_data,
                {"cue_count": len(source.captions) + 1},
            )
        )
        report = {
            "schema": "takegraph.delivery_report.v1",
            "ffmpeg_version": _ffmpeg_version(work),
            "network_inputs": False,
            "clip_count": 4,
            "timeline_duration_ms": picture_probe.duration_ms,
            "outputs": report_outputs,
        }
        report_data = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        artifacts.append(
            DeliveryArtifact(
                "report",
                "report.json",
                "application/json",
                "DOCUMENT",
                report_data,
                {"schema": "takegraph.delivery_report.v1"},
            )
        )
        return tuple(artifacts)


def _fit_filter(width: int, height: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )


def _run_ffmpeg(arguments: list[str], *, work: Path, timeout_seconds: int) -> None:
    for value in arguments:
        if "://" in value:
            raise InvalidSourceError("FFmpeg network inputs are forbidden.")
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        *arguments,
    ]
    try:
        result = subprocess.run(  # noqa: S603 — fixed executable and validated argv
            command,
            cwd=work,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidSourceError("FFmpeg composition did not complete safely.") from exc
    if len(result.stdout) + len(result.stderr) > MAX_PROCESS_OUTPUT_BYTES:
        raise InvalidSourceError("FFmpeg diagnostic output exceeded the safety limit.")
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace")[-500:]
        raise InvalidSourceError(f"FFmpeg composition failed: {detail}")


def _ffmpeg_version(work: Path) -> str:
    command = ["ffmpeg", "-version"]
    try:
        result = subprocess.run(  # noqa: S603 — fixed executable and argv
            command,
            cwd=work,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidSourceError("FFmpeg version could not be read.") from exc
    if result.returncode != 0 or len(result.stdout) > MAX_PROCESS_OUTPUT_BYTES:
        raise InvalidSourceError("FFmpeg version could not be verified.")
    return result.stdout.decode(errors="replace").splitlines()[0]


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _probe_metadata(probe: MediaProbe) -> dict[str, object]:
    return {
        "format": probe.format_name,
        "width": probe.width,
        "height": probe.height,
        "duration_ms": probe.duration_ms,
        "has_audio": probe.has_audio,
        "sample_rate": probe.sample_rate,
        "channels": probe.channels,
    }


def _webvtt(captions: tuple[str, ...], legal_line: str, duration_ms: int) -> str:
    phrases = (*captions, legal_line)
    if not phrases:
        raise InvalidSourceError("Delivery captions cannot be empty.")
    interval = max(duration_ms // len(phrases), 1)
    lines = ["WEBVTT", ""]
    for index, phrase in enumerate(phrases):
        start = index * interval
        end = duration_ms if index == len(phrases) - 1 else (index + 1) * interval
        lines.extend((str(index + 1), f"{_timestamp(start)} --> {_timestamp(end)}", phrase, ""))
    return "\n".join(lines)


def _timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


__all__ = [
    "DeliveryArtifact",
    "DeliveryInput",
    "compose_delivery_package",
]
