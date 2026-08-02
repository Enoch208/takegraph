"""Deterministic local image composition for ORBIT delivery graphics."""

from __future__ import annotations

import io
import math

from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFilter,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)
from takegraph_domain.errors import InvalidSourceError

MAX_REFERENCE_BYTES = 50 * 1_048_576


def compose_product_cutout(
    product_reference: bytes,
    *,
    feather_px: int = 2,
) -> bytes:
    """Remove an edge-connected near-uniform background deterministically.

    This is intentionally a bounded local transform, not a claim of semantic
    segmentation. The four corner colours establish the background reference;
    only pixels connected to the image edge and within the colour tolerance are
    removed, so similarly coloured label details enclosed by the product survive.
    """
    if not product_reference or len(product_reference) > MAX_REFERENCE_BYTES:
        raise InvalidSourceError("Product reference exceeds the image safety limit.")
    if feather_px < 0 or feather_px > 8:
        raise InvalidSourceError("Cutout feather must be between 0 and 8 pixels.")
    try:
        with Image.open(io.BytesIO(product_reference)) as source:
            source.verify()
        with Image.open(io.BytesIO(product_reference)) as source:
            image = source.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidSourceError("Product reference is not a decodable image.") from exc
    if image.width < 2 or image.height < 2:
        raise InvalidSourceError("Product reference is too small for background removal.")

    pixels = image.load()
    if pixels is None:
        raise InvalidSourceError("Product reference pixels could not be decoded.")
    corners = (
        pixels[0, 0],
        pixels[image.width - 1, 0],
        pixels[0, image.height - 1],
        pixels[image.width - 1, image.height - 1],
    )
    background = tuple(sorted(pixel[channel] for pixel in corners)[1:3][0] for channel in range(3))
    tolerance = 42.0

    def distance(x: int, y: int) -> float:
        pixel = pixels[x, y]
        return math.sqrt(sum((pixel[channel] - background[channel]) ** 2 for channel in range(3)))

    candidates: set[tuple[int, int]] = set()
    frontier = [(x, 0) for x in range(image.width)] + [
        (x, image.height - 1) for x in range(image.width)
    ]
    frontier += [(0, y) for y in range(1, image.height - 1)] + [
        (image.width - 1, y) for y in range(1, image.height - 1)
    ]
    while frontier:
        x, y = frontier.pop()
        if (x, y) in candidates or distance(x, y) > tolerance:
            continue
        candidates.add((x, y))
        if x:
            frontier.append((x - 1, y))
        if x + 1 < image.width:
            frontier.append((x + 1, y))
        if y:
            frontier.append((x, y - 1))
        if y + 1 < image.height:
            frontier.append((x, y + 1))

    if not candidates or len(candidates) == image.width * image.height:
        raise InvalidSourceError("Product cutout could not separate foreground from background.")
    alpha = Image.new("L", image.size, 255)
    alpha_pixels = alpha.load()
    if alpha_pixels is None:
        raise InvalidSourceError("Product cutout alpha channel could not be allocated.")
    for x, y in candidates:
        alpha_pixels[x, y] = 0
    if feather_px:
        alpha = alpha.filter(ImageFilter.GaussianBlur(radius=feather_px))
    source_alpha = image.getchannel("A")
    alpha = ImageChops.multiply(alpha, source_alpha)
    image.putalpha(alpha)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def compose_orbit_poster(
    product_reference: bytes,
    keyframe: bytes,
    *,
    width: int = 1_080,
    height: int = 1_350,
) -> bytes:
    """Create the deterministic 4:5 ORBIT poster from selected stored inputs."""
    if (width, height) != (1_080, 1_350):
        raise InvalidSourceError("ORBIT poster must be exactly 1080x1350.")
    images: list[Image.Image] = []
    for label, payload in (("product", product_reference), ("keyframe", keyframe)):
        if not payload or len(payload) > MAX_REFERENCE_BYTES:
            raise InvalidSourceError(f"Poster {label} input exceeds the image safety limit.")
        try:
            with Image.open(io.BytesIO(payload)) as source:
                source.verify()
            with Image.open(io.BytesIO(payload)) as source:
                images.append(source.convert("RGBA"))
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise InvalidSourceError(f"Poster {label} input is not a decodable image.") from exc

    product, keyframe_image = images
    background = ImageOps.fit(
        keyframe_image.convert("RGB"),
        (width, height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    ).convert("RGBA")
    shade = Image.new("RGBA", (width, height), (5, 6, 8, 0))
    shade_alpha = Image.new("L", (width, height))
    shade_alpha.putdata(
        [min(220, 50 + int(170 * y / height)) for y in range(height) for _ in range(width)]
    )
    shade.putalpha(shade_alpha)
    canvas = Image.alpha_composite(background, shade)
    product = ImageOps.contain(product, (600, 760), method=Image.Resampling.LANCZOS)
    canvas.alpha_composite(
        product,
        (width - product.width - 70, height - product.height - 90),
    )
    draw = ImageDraw.Draw(canvas)
    micro = ImageFont.load_default(size=24)
    display = ImageFont.load_default(size=84)
    body = ImageFont.load_default(size=34)
    draw.text((70, 72), "TAKEGRAPH / ORBIT", font=micro, fill="#FF6A35")
    draw.text((70, 176), "CHANGE ONE", font=display, fill="#F5F7FA")
    draw.text((70, 274), "DETAIL.", font=display, fill="#F5F7FA")
    draw.line((70, 392, 620, 392), fill="#F5F7FA", width=2)
    draw.text((70, 430), "REBUILD ONLY WHAT CHANGED.", font=body, fill="#F5F7FA")
    draw.text((70, 1_250), "VERIFIED MEDIA GRAPH / POSTER V1", font=micro, fill="#D5D9DF")
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


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


__all__ = ["compose_orbit_end_card", "compose_orbit_poster", "compose_product_cutout"]
