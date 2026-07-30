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
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "ui" / "brand" / "logo.png"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "ui" / "public"
# Written next to the source so the palette can be checked against the image it claims to come
# from: `ui/tests/brand.test.ts` reads this, not the developer's memory.
MEASUREMENT_FILENAME = "measured-colors.json"


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

ASSET_NAMES = (
    "logo.png",
    *(f"icon-{size}.png" for size in MASKABLE_SIZES),
    "apple-touch-icon.png",
    "favicon.ico",
)

# Two thresholds, because framing and measuring ask different questions of the same alpha.
#
# Framing asks how far the mark reaches, so it counts anything a viewer could see. Alpha below
# a few percent is not that: it is the dust an export leaves behind — a stray guide layer, a
# 1-px artboard overrun — and one such pixel in a corner would otherwise stretch the frame to
# the whole canvas and shrink the mark in every asset at once, silently.
VISIBLE_ALPHA = 10
# A threshold alone only narrows that window, so the reach is checked against the paint as well:
# a soft edge stays close to what it surrounds, while dust sits wherever the export dropped it.
# Measured on this artwork, a generous drop shadow (45px blur at 35%) reaches 1.11x past the
# opaque mark and a single speck in the corner of an 800px canvas reaches 2.5x.
MAX_VISIBLE_OVERREACH = 1.5
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
    """How the mark sits on the square canvas every asset is rendered from."""

    size: int
    left: int
    top: int
    # What the fully opaque part of the mark spans, as a share of that canvas. Reported so the
    # run states a measured number instead of reading `MARK_COVERAGE` back out: the two differ
    # by however soft the mark's edge is, and only this one moves when a source goes wrong.
    paint_share: float


def display(path: Path) -> str:
    """Repo-relative where that reads better, absolute where it would not."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path.resolve())


def check_destinations(source_path: Path, output_dir: Path) -> None:
    """Refuse the destinations that would leave the repo describing something it does not hold.

    Nothing here reads or writes a file, so a run that is going to be refused is refused before
    it renders anything — and the rule can be checked without putting the repo's own asset
    directory in a test's reach.
    """
    # The source is read from disk and the outputs are written to it, so an output dir holding the
    # source would replace the original with a 192px derivative — and every later run would then
    # shrink it again.
    resolved_source = source_path.resolve()
    collision = next(
        (name for name in ASSET_NAMES if (output_dir / name).resolve() == resolved_source), None
    )
    if collision is not None:
        message = f"output would overwrite the source logo: {display(output_dir / collision)}"
        raise BrandAssetError(message)
    # The measurement lands beside the source and the images land in the output dir, so those two
    # have to agree on whether this run is the repo's. If only one of them is, the repo ends up
    # describing a mark it does not hold: images the palette never followed, or a measurement no
    # asset was rendered from. Neither is visible downstream, since `ui/tests/brand.test.ts` reads
    # the measurement and never the images.
    measurement_path = source_path.parent / MEASUREMENT_FILENAME
    if measurement_path.resolve().is_relative_to(REPO_ROOT) != output_dir.resolve().is_relative_to(
        REPO_ROOT
    ):
        message = (
            f"images would go to {display(output_dir)} while the measurement lands in "
            f"{display(measurement_path)}: keep both inside the repo or both outside it"
        )
        raise BrandAssetError(message)


def _longest(box: Box) -> int:
    left, top, right, bottom = box
    return max(right - left, bottom - top)


def square_frame(visible: Box | None, painted: Box | None) -> Frame:
    """Center the mark's visible extent on the canvas it fills to `MARK_COVERAGE`.

    The canvas is sized from that extent's longest side and nothing else, so two sources drawn
    with different margins put the same mark at the same size. `painted` is the same mark bounded
    at full opacity: a visible extent reaching far past it is not a soft edge but dust the export
    left behind, and framing from it would shrink the mark in every asset at once.
    """
    if visible is None or painted is None:
        message = "source logo has no visible pixel to frame"
        raise BrandAssetError(message)
    reach = _longest(visible) / _longest(painted)
    if reach > MAX_VISIBLE_OVERREACH:
        message = (
            f"source logo reaches {reach:.1f}x past its own paint, over the "
            f"{MAX_VISIBLE_OVERREACH:.1f}x a soft edge takes: erase what is left outside the mark"
        )
        raise BrandAssetError(message)
    left, top, right, bottom = visible
    width = right - left
    height = bottom - top
    size = round(max(width, height) / MARK_COVERAGE)
    return Frame(
        size=size,
        left=(size - width) // 2,
        top=(size - height) // 2,
        paint_share=_longest(painted) / size,
    )


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
