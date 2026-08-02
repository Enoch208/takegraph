from __future__ import annotations

import base64
import io
import json
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from takegraph_domain.errors import InvalidSourceError
from takegraph_infrastructure.delivery import DeliveryInput, compose_delivery_package
from takegraph_infrastructure.image_composition import (
    compose_orbit_end_card,
    compose_orbit_poster,
    compose_product_cutout,
)
from takegraph_infrastructure.media import (
    detect_mime,
    normalize_narration_bytes,
    probe_media_bytes,
)

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_detects_png_from_magic_bytes() -> None:
    assert detect_mime(PNG_1X1) == "image/png"


def test_extension_does_not_override_unknown_bytes() -> None:
    with pytest.raises(InvalidSourceError):
        detect_mime(b"not an image despite its filename.png")


def test_ffprobe_reports_typed_image_metadata(tmp_path) -> None:
    probe = probe_media_bytes(PNG_1X1, suffix=".png", temp_root=tmp_path)

    assert probe.media_kind == "IMAGE"
    assert probe.width == 1
    assert probe.height == 1
    assert "png" in probe.codec_names


def test_probe_rejects_undecodable_media(tmp_path) -> None:
    with pytest.raises(InvalidSourceError, match="no decodable frame dimensions"):
        probe_media_bytes(b"not media", suffix=".png", temp_root=tmp_path)


def test_narration_is_normalized_to_48khz_mono_wav(tmp_path) -> None:
    source = io.BytesIO()
    with wave.open(source, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(22_050)
        audio.writeframes(b"".join(struct.pack("<hh", 1000, -1000) for _ in range(2_205)))

    normalized = normalize_narration_bytes(source.getvalue(), temp_root=tmp_path)
    probe = probe_media_bytes(normalized, suffix=".wav", temp_root=tmp_path)

    assert detect_mime(normalized) == "audio/wav"
    assert probe.media_kind == "AUDIO"
    assert probe.sample_rate == 48_000
    assert probe.channels == 1


def test_end_card_is_deterministic_1920x1080_png(tmp_path) -> None:
    first = compose_orbit_end_card(PNG_1X1, legal_line="no added sugar")
    second = compose_orbit_end_card(PNG_1X1, legal_line="no added sugar")
    probe = probe_media_bytes(first, suffix=".png", temp_root=tmp_path)

    assert first == second
    assert detect_mime(first) == "image/png"
    assert probe.width == 1_920
    assert probe.height == 1_080


def _product_reference() -> bytes:
    image = Image.new("RGB", (240, 320), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((80, 30, 160, 290), radius=24, fill="#111820")
    draw.rectangle((93, 118, 147, 205), fill="#F5F7FA")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_product_cutout_removes_only_edge_connected_background(tmp_path) -> None:
    first = compose_product_cutout(_product_reference(), feather_px=0)
    second = compose_product_cutout(_product_reference(), feather_px=0)
    with Image.open(io.BytesIO(first)) as output:
        alpha = output.getchannel("A")
        assert alpha.getpixel((0, 0)) == 0
        assert alpha.getpixel((120, 160)) == 255
    probe = probe_media_bytes(first, suffix=".png", temp_root=tmp_path)

    assert first == second
    assert probe.width == 240
    assert probe.height == 320


def test_poster_is_deterministic_1080x1350_png(tmp_path) -> None:
    cutout = compose_product_cutout(_product_reference(), feather_px=0)
    first = compose_orbit_poster(cutout, _product_reference())
    second = compose_orbit_poster(cutout, _product_reference())
    probe = probe_media_bytes(first, suffix=".png", temp_root=tmp_path)

    assert first == second
    assert detect_mime(first) == "image/png"
    assert probe.width == 1_080
    assert probe.height == 1_350


def _ffmpeg_fixture(path: Path, kind: str) -> bytes:
    if kind == "video":
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=#111820:s=320x180:r=30:d=0.25",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    else:
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    subprocess.run(command, check=True, capture_output=True)  # noqa: S603
    return path.read_bytes()


def test_delivery_composer_emits_two_masters_and_proof_artifacts(tmp_path) -> None:
    clip = _ffmpeg_fixture(tmp_path / "fixture.mp4", "video")
    narration = _ffmpeg_fixture(tmp_path / "narration.wav", "audio")
    music = _ffmpeg_fixture(tmp_path / "music.wav", "audio")
    artifacts = compose_delivery_package(
        DeliveryInput(
            clips=(clip, clip, clip, clip),
            narration=narration,
            music=music,
            end_card=compose_orbit_end_card(PNG_1X1, legal_line="no added sugar"),
            captions=("ORBIT hydration", "Built for motion"),
            legal_line="no added sugar",
        ),
        temp_root=tmp_path / "delivery",
        width=320,
        height=180,
    )

    by_role = {artifact.role: artifact for artifact in artifacts}
    assert set(by_role) == {
        "master_16x9",
        "master_9x16",
        "final_audio",
        "thumbnail_16x9",
        "thumbnail_9x16",
        "captions",
        "report",
    }
    assert by_role["master_16x9"].metadata["width"] == 320
    assert by_role["master_16x9"].metadata["height"] == 180
    assert by_role["master_9x16"].metadata["width"] == 180
    assert by_role["master_9x16"].metadata["height"] == 320
    assert by_role["final_audio"].metadata["sample_rate"] == 48_000
    assert by_role["captions"].data.startswith(b"WEBVTT")
    report = json.loads(by_role["report"].data)
    assert report["network_inputs"] is False
    assert report["clip_count"] == 4
