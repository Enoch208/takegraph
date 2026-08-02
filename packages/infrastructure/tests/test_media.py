from __future__ import annotations

import base64

import pytest
from takegraph_domain.errors import InvalidSourceError
from takegraph_infrastructure.media import detect_mime, probe_media_bytes

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
