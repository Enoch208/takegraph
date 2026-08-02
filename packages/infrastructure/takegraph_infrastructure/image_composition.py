"""Deterministic local image composition for ORBIT delivery graphics."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from takegraph_domain.errors import InvalidSourceError

MAX_REFERENCE_BYTES = 50 * 1_048_576


def compose_orbit_end_card(
    product_reference: bytes,
    *,
    legal_line: str,
    width: int = 1_920,
    height: int = 1_080,
) -> bytes:
    """Render a fixed-layout PNG using only content-bound inputs.

    Pillow's bundled default font is pinned with the dependency, avoiding a
    machine-specific system-font lookup that would change output hashes.
    """
    if not product_reference or len(product_reference) > MAX_REFERENCE_BYTES:
        raise InvalidSourceError("Product reference exceeds the image safety limit.")
    if not legal_line or len(legal_line) > 500:
        raise InvalidSourceError("End-card legal line is invalid.")
    if (width, height) != (1_920, 1_080):
        raise InvalidSourceError("ORBIT end card must be exactly 1920x1080.")
    try:
        with Image.open(io.BytesIO(product_reference)) as source:
            source.verify()
        with Image.open(io.BytesIO(product_reference)) as source:
            product = ImageOps.contain(source.convert("RGBA"), (760, 760))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidSourceError("Product reference is not a decodable image.") from exc

    canvas = Image.new("RGBA", (width, height), "#050608")
    draw = ImageDraw.Draw(canvas)
    # Restrained VC-style signal field: fixed geometry, no random noise.
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    for radius, alpha in ((430, 18), (320, 24), (220, 30)):
        glow_draw.ellipse(
            (1_250 - radius, 540 - radius, 1_250 + radius, 540 + radius),
            fill=(255, 106, 53, alpha),
        )
    canvas = Image.alpha_composite(canvas, glow)
    draw = ImageDraw.Draw(canvas)
    product_x = 1_250 - product.width // 2
    product_y = 540 - product.height // 2
    canvas.alpha_composite(product, (product_x, product_y))

    display = ImageFont.load_default(size=92)
    body = ImageFont.load_default(size=38)
    micro = ImageFont.load_default(size=20)
    draw.text((120, 142), "TAKEGRAPH / ORBIT", font=micro, fill="#FF6A35")
    draw.text((120, 286), "ONE DETAIL.", font=display, fill="#F5F7FA")
    draw.text((120, 396), "ONLY WHAT CHANGED.", font=display, fill="#9CA6B5")
    draw.line((120, 714, 770, 714), fill="#2A3039", width=2)
    draw.text((120, 756), legal_line, font=body, fill="#F5F7FA")
    draw.text((120, 836), "VERIFIED COPY / END CARD V1", font=micro, fill="#9CA6B5")

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


__all__ = ["compose_orbit_end_card"]
