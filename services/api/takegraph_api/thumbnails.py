"""Cached, downscaled poster images for the storyboard (PRD §18.9, §15.3).

Why this exists, concretely: the workspace shows eighteen tiles, each of which was
pointing a browser at the full-resolution original in B2 — a 1.6 MB render, or an
MP4 — to fill a box a couple of hundred pixels wide. That is wrong three times
over.

- It is slow. The storyboard sat empty for about fourteen seconds.
- It is expensive. Every page view spent a B2 Class B transaction and the object's
  egress per tile, against a daily cap, and it recurred on every reload because a
  presigned URL carries a fresh signature and can never be reused from cache.
- It is fragile. When that cap is reached B2 answers 403 with an XML error
  document, and a browser asked to render XML as an image draws a broken-image
  glyph. The dashboard looked broken while behaving correctly.

A thumbnail is derived from immutable bytes, so it is cached by the sha256 of the
asset it came from and never invalidated. The first request for an asset costs one
B2 read; every request after that, for every viewer, costs none. The response is
same-origin, which also removes the presigned-URL expiry and the cross-origin
blocking that produced the broken glyphs.
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import uuid
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from genblaze_core.exceptions import StorageError
from PIL import Image
from takegraph_domain.errors import InvalidSourceError, StorageUnavailableError
from takegraph_infrastructure.b2 import B2Settings, B2Store
from takegraph_infrastructure.media import extract_poster_frame

from takegraph_api.db.session import session_scope
from takegraph_api.projects import AssetAccessService, MemberPrincipal

router = APIRouter(prefix="/api/v1", tags=["assets"])

#: Long edge in pixels. The largest tile in the storyboard is around 300 CSS px,
#: so 640 covers a 2x display with room to spare and still lands in tens of
#: kilobytes rather than megabytes.
THUMBNAIL_EDGE = 640

#: WebP at this quality is visually indistinguishable from the source at tile size
#: and roughly an order of magnitude smaller than the PNG originals.
WEBP_QUALITY = 82

#: Seconds into a clip to grab the poster from. Several ORBIT clips open on a
#: near-black frame, so frame zero would render a black rectangle and read as a
#: failure; a little way in there is always picture.
VIDEO_POSTER_SECONDS = 0.8


def cache_root() -> Path:
    root = Path(os.environ.get("MEDIA_CACHE_DIR", ".cache/thumbnails"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_path(sha256: str) -> Path:
    """Content-addressed, so a cached thumbnail can never be stale."""
    return cache_root() / sha256[:2] / f"{sha256}.webp"


def _encode_image(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        # Flatten alpha onto the workspace surface rather than onto white, so a
        # transparent cutout does not arrive as a bright rectangle on a dark page.
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            backdrop = Image.new("RGBA", image.size, (15, 18, 22, 255))
            image = Image.alpha_composite(backdrop, image).convert("RGB")
        else:
            image = image.convert("RGB")
        image.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
        return out.getvalue()


def _encode_video_poster(data: bytes) -> bytes:
    """One decoded frame, then encoded like any other image.

    Frame extraction lives in the infrastructure package with the other ffmpeg
    callers (§7.1) rather than the API shelling out on its own.
    """
    with tempfile.TemporaryDirectory() as work:
        frame = extract_poster_frame(
            data, temp_root=Path(work), at_seconds=VIDEO_POSTER_SECONDS
        )
    return _encode_image(frame)


def _render(data: bytes, mime_type: str) -> bytes:
    if mime_type.startswith("video/"):
        return _encode_video_poster(data)
    if mime_type.startswith("image/"):
        return _encode_image(data)
    raise InvalidSourceError(f"{mime_type} has no visual representation.")


@router.get("/assets/{asset_id}/thumbnail")
async def asset_thumbnail(asset_id: uuid.UUID, principal: MemberPrincipal) -> Response:
    """A small, cacheable poster for an asset.

    Authorised exactly like the signed-URL route: viewing a thumbnail is viewing
    the asset, and this endpoint must not become a way around that check.
    """
    store = B2Store(B2Settings.from_env(dict(os.environ)))
    try:
        async with session_scope() as session:
            asset = await AssetAccessService(session, store, ttl_seconds=1).authorize(
                asset_id=asset_id, principal=principal
            )
            sha256 = asset.sha256
            b2_key = asset.b2_key
            mime_type = asset.mime_type

        headers = {
            # Immutable: the URL identifies bytes by hash, so a cached copy can
            # never be wrong. This is what makes a reload cost nothing.
            "Cache-Control": "public, max-age=31536000, immutable",
        }

        path = cache_path(sha256)
        if path.exists():
            return FileResponse(path, media_type="image/webp", headers=headers)

        try:
            # get_verified, not get_bytes: a thumbnail is derived evidence, and
            # §8.3.7 applies to it as much as to anything else the product shows.
            data = await asyncio.to_thread(store.get_verified, b2_key, expected_sha256=sha256)
        except (StorageError, ClientError, BotoCoreError, OSError) as exc:
            # A refused read is not a bug in this request. The most likely cause
            # in practice is the account's daily Class B transaction or download
            # bandwidth cap, which B2 reports as an AccessDenied 403 carrying an
            # XML body. Surfacing that as an unhandled 500 told the client nothing
            # and left the storyboard drawing broken-image glyphs.
            raise StorageUnavailableError(
                "Durable storage would not serve the asset bytes.",
                details={"reason": str(exc)[:200]},
            ) from exc
    finally:
        store.close()

    encoded = await asyncio.to_thread(_render, data, mime_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write then rename so a concurrent reader never sees a half-written file.
    staging = path.with_suffix(f".{uuid.uuid4().hex}.part")
    staging.write_bytes(encoded)
    staging.replace(path)
    return Response(content=encoded, media_type="image/webp", headers=headers)
