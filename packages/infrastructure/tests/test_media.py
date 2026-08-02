from __future__ import annotations

import base64
import io
import struct
import wave

import pytest
from takegraph_domain.errors import InvalidSourceError
from takegraph_infrastructure.image_composition import compose_orbit_end_card
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
