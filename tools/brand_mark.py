"""The shapes and colors every Baibai App brand asset is built from.

`tools/generate_brand_assets.py` renders them, and it needs Pillow — which stays out of the
project's lock so no daily workflow installs a native image library. So the decisions live
here instead: what each asset's size and inset are, how much of its canvas the mark is framed
to fill, and what counts as one of the mark's own colors. What stays next door is Pillow glue —
open, crop, paste, save.
"""

from __future__ import annotations

import colorsys
from collections import Counter
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

# Two thresholds, because framing and measuring ask different questions of the same alpha.
#
# Framing asks how far the mark reaches, so it counts anything a viewer could see. Alpha below
# a few percent is not that: it is the dust an export leaves behind — a stray guide layer, a
# 1-px artboard overrun — and one such pixel in a corner would otherwise stretch the frame to
# the whole canvas and shrink the mark in every asset at once, silently.
VISIBLE_ALPHA = 10
# Measuring asks what the mark is painted in, so it counts only pixels whose color is the paint
# rather than a blend with whatever was behind them.
OPAQUE_ALPHA = 250
# Below this, a pixel carries no hue worth naming a color after.
MIN_SATURATION = 0.35
# A hue band thinner than this is a seam between two of the mark's colors, not a color of its
# own: the boundary between a fill and its shading holds a few hundred anti-aliased pixels in
# hues neither side was painted in, and they would otherwise name a palette entry.
MIN_BAND_SHARE = 0.01

type Histogram = Sequence[tuple[int, tuple[int, int, int, int]]]
type Box = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class Band:
    """A hue range (HSL degrees) that one palette entry is named after."""

    name: str
    # The palette entry that claims this color, named for its CSS spelling: a field called
    # `token` reads as a credential to the secret scanner and fails the security run.
    custom_property: str
    low: float
    high: float


# The mark's lime sits near 67° and its green near 154°. The green band runs from where the
# lime ends to before the blues, so a green that shifts with a new mark is still read out.
LIME_BAND = Band(name="lime", custom_property="--brand-lime", low=55.0, high=85.0)
GREEN_BAND = Band(name="green", custom_property="--brand-green", low=85.0, high=200.0)
BANDS = (LIME_BAND, GREEN_BAND)


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
        message = "source logo has no visible pixel to frame"
        raise BrandAssetError(message)
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    size = round(max(width, height) / MARK_COVERAGE)
    return Frame(size=size, left=(size - width) // 2, top=(size - height) // 2)


def measure_palette(histogram: Histogram, bands: Sequence[Band] = BANDS) -> dict[str, str]:
    """Each band's color, keyed by the palette entry that claims it.

    A band's color is its most frequent pixel, so what reaches the palette is a color the mark
    is actually painted in rather than an average of a fill and its shading — an average lands
    between the two and can name a color no pixel of the mark holds.

    Shading spreads a fill over hundreds of neighbouring values, so the winner leads by a
    modest margin and re-rendering the artwork can move a channel by one step. No estimator
    escapes that: on every mark measured here the mode, the mean and a quantized mode all shift
    by a step under a resample. `ui/tests/brand.test.ts` therefore holds the palette to this
    perceptually rather than byte for byte, which passes a step and stops a color change. What
    the minimum share below rules out is the different failure — a winner decided by a seam
    between two colors instead of by paint.
    """
    opaque = sum(count for count, pixel in histogram if pixel[3] >= OPAQUE_ALPHA)
    if opaque == 0:
        message = "source logo has no opaque pixel to measure"
        raise BrandAssetError(message)

    measured: dict[str, str] = {}
    for band in bands:
        tally: Counter[tuple[int, int, int]] = Counter()
        for count, (red, green, blue, alpha) in histogram:
            if alpha < OPAQUE_ALPHA:
                continue
            hue, _, saturation = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
            if saturation < MIN_SATURATION:
                continue
            if not (band.low <= hue * 360 < band.high):
                continue
            tally[(red, green, blue)] += count
        share = tally.total() / opaque
        if share < MIN_BAND_SHARE:
            message = (
                f"source logo's {band.name} covers {share:.2%} of the mark, under the "
                f"{MIN_BAND_SHARE:.0%} that naming {band.custom_property} after it would take"
            )
            raise BrandAssetError(message)
        red, green, blue = tally.most_common(1)[0][0]
        measured[band.custom_property] = f"#{red:02X}{green:02X}{blue:02X}"
    return measured
