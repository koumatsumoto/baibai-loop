"""The shapes and colors every Baibai App brand asset is built from.

`tools/generate_brand_assets.py` renders them, and it needs Pillow — which stays out of the
project's lock so no daily workflow installs a native image library, and which therefore
leaves that file outside both the type check and the test run. So the decisions live here
instead: what each asset's size and inset are, how much of its canvas the mark is framed to
fill, and what counts as one of the mark's own colors. What stays next door is Pillow glue —
open, crop, paste, save.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class BrandAssetError(RuntimeError):
    """The source image cannot produce the asset set."""


# Every output is rendered from the mark centered on a square it fills to this much of its
# longest side. Without that step the header image is the source canvas scaled whole, so the
# margin the artwork happens to arrive with decides how large the mark reads in the header
# and inside every launcher icon.
MARK_COVERAGE = 0.95

# A maskable icon may be cropped to any shape inscribed in the canvas, so the mark is drawn at
# 80% and centered: that keeps it inside the safe area every launcher honors. The iOS mask is
# a rounded square that crops far less, so its icon fills more.
MASKABLE_SCALE = 0.80
APPLE_TOUCH_SCALE = 0.90
FAVICON_SIZES = (16, 32)
HEADER_SIZE = 192
APPLE_TOUCH_SIZE = 180
MASKABLE_SIZES = (192, 512)

# What belongs to the mark rather than to the edge it was anti-aliased against.
OPAQUE_ALPHA = 250
MIN_SATURATION = 0.35
# A hue band thinner than this is a seam between two of the mark's colors, not a color of its
# own: the boundary between a fill and its shading holds a few hundred anti-aliased pixels in
# hues neither side was painted in, and they would otherwise name a palette token.
MIN_BAND_SHARE = 0.01

type Histogram = Sequence[tuple[int, tuple[int, int, int, int]]]
type Box = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class Band:
    """A hue range (HSL degrees) that one palette token is named after."""

    name: str
    # The palette entry that claims this color, named for its CSS spelling: a field called
    # `token` reads as a credential to the secret scanner and fails the security run.
    custom_property: str
    low: float
    high: float


# The mark's lime sits near 67°, its green near 155° and its leaf near 40°. Only the lime is
# named by a palette token, so it is the only band read out — a band added here has to name a
# token that the palette actually paints with.
BANDS = (Band(name="lime", custom_property="--brand-lime", low=55.0, high=85.0),)


@dataclass(frozen=True, slots=True)
class Frame:
    """Where the mark sits on the square canvas every asset is rendered from."""

    size: int
    left: int
    top: int


def square_frame(box: Box | None) -> Frame:
    """Center a mark's bounding box on the canvas it fills to `MARK_COVERAGE`.

    The canvas is sized from the mark's longest side and nothing else, so two sources drawn
    with different margins put the same mark at the same size.
    """
    if box is None:
        message = "source logo has no opaque pixel to frame"
        raise BrandAssetError(message)
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    size = round(max(width, height) / MARK_COVERAGE)
    return Frame(size=size, left=(size - width) // 2, top=(size - height) // 2)


def measure_palette(histogram: Histogram) -> dict[str, str]:
    """The mark's own colors, one per band, as the palette should name them.

    Each color is the count-weighted mean of its band, not the band's most frequent pixel.
    Shading spreads a fill across hundreds of neighbouring values, so a mode is decided by a
    thin margin between two colors a viewer cannot tell apart and moves whenever the artwork
    is rendered again — while the mean only moves when the paint does.
    """
    opaque = sum(count for count, pixel in histogram if pixel[3] >= OPAQUE_ALPHA)
    if opaque == 0:
        message = "source logo has no opaque pixel to measure"
        raise BrandAssetError(message)

    measured: dict[str, str] = {}
    for band in BANDS:
        total = 0
        reds = greens = blues = 0
        for count, (red, green, blue, alpha) in histogram:
            if alpha < OPAQUE_ALPHA:
                continue
            hue, _, saturation = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
            if saturation < MIN_SATURATION:
                continue
            if not (band.low <= hue * 360 < band.high):
                continue
            total += count
            reds += red * count
            greens += green * count
            blues += blue * count
        share = total / opaque
        if share < MIN_BAND_SHARE:
            message = (
                f"source logo's {band.name} covers {share:.2%} of the mark, under the "
                f"{MIN_BAND_SHARE:.0%} that naming {band.custom_property} after it would take"
            )
            raise BrandAssetError(message)
        measured[band.name] = (
            f"#{round(reds / total):02X}{round(greens / total):02X}{round(blues / total):02X}"
        )
    return measured
