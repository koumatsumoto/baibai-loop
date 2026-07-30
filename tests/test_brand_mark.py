from __future__ import annotations

from pathlib import Path

import pytest
from tools.brand_mark import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SOURCE,
    LIME_BAND,
    MARK_COVERAGE,
    REPO_ROOT,
    BrandAssetError,
    check_destinations,
    measure_palette,
    square_frame,
)

# The mark's two colors. Every neighbour used below reads at the same hue and well past the
# saturation floor, so a case that means to exercise one filter is not carried by another.
LIME = (199, 222, 36)
GREEN = (9, 171, 101)
# Opaque, saturated, and in neither band: filler that decides how large a share of the mark a
# band covers.
RED = (200, 30, 30)
# Inside the lime band by hue, but too washed out to be paint.
DULL = (130, 132, 120)


def _pixels(color, count, alpha=255):
    return (count, (*color, alpha))


# The destination rules read paths and never the filesystem, so these run against fabricated ones
# — which is the only way to state what happens to the repo's own asset directory without a test
# that could write into it.
OUTSIDE = Path("/tmp/elsewhere/logo.png")


@pytest.mark.parametrize(
    ("source", "output_dir", "refusal"),
    [
        (DEFAULT_SOURCE, DEFAULT_OUTPUT_DIR, None),
        (OUTSIDE, OUTSIDE.parent / "out", None),
        (DEFAULT_SOURCE, Path("/tmp/preview"), "while the measurement lands in"),
        # The half that goes unnoticed: the repo would carry images of a mark that its own
        # logo.png, its measurement and its palette all disagree with.
        (OUTSIDE, DEFAULT_OUTPUT_DIR, "while the measurement lands in"),
        (REPO_ROOT / "ui" / "public" / "logo.png", DEFAULT_OUTPUT_DIR, "would overwrite"),
    ],
)
def test_check_destinations_takes_the_repos_pair_and_refuses_a_split_one(
    source, output_dir, refusal
):
    if refusal is None:
        check_destinations(source, output_dir)
        return
    with pytest.raises(BrandAssetError, match=refusal):
        check_destinations(source, output_dir)


@pytest.mark.parametrize(
    ("visible", "size", "offset"),
    [
        ((0, 0, 95, 95), 100, (2, 2)),
        # Same mark, more margin around it in the source: the frame comes out identical.
        ((40, 300, 135, 395), 100, (2, 2)),
        # Sized from the longest side, so the short axis takes the rest of the padding.
        ((0, 0, 50, 95), 100, (25, 2)),
    ],
)
def test_square_frame_pads_the_mark_out_to_the_coverage_share(visible, size, offset):
    frame = square_frame(visible, (0, 0, 10, 10))

    assert size == round(95 / MARK_COVERAGE)
    assert (frame.size, frame.left, frame.top) == (size, *offset)


def test_square_frame_reports_what_the_paint_spans_rather_than_the_coverage_constant():
    # 80px of paint on a canvas sized from the 100px visible extent, which is 105 wide. Reading
    # MARK_COVERAGE back out would tell the operator 95% of whatever they passed.
    frame = square_frame((0, 0, 100, 100), (10, 10, 90, 90))

    assert frame.paint_share == pytest.approx(80 / 105)


def test_square_frame_rejects_a_source_with_nothing_visible_on_it():
    with pytest.raises(BrandAssetError, match="no visible pixel to frame"):
        square_frame(None, None)


def test_measure_palette_names_each_band_after_the_palette_entry_that_claims_it():
    histogram = [_pixels(LIME, 1_000), _pixels(GREEN, 1_000)]

    assert measure_palette(histogram) == {"--brand-lime": "#C7DE24", "--brand-green": "#09AB65"}


def test_measure_palette_reports_a_color_the_mark_is_painted_in():
    # A fill at 1,100px over a shading gradient that runs away from it — 13 values at 500px
    # each. The reported color is the fill; the band's average is blue 41, a shade the fill and
    # the gradient share the canvas with but neither is painted in.
    counts = dict.fromkeys(range(36, 49), 500)
    counts[36] = 1_100
    histogram = [_pixels((199, 222, blue), count) for blue, count in counts.items()]

    assert measure_palette(histogram, bands=(LIME_BAND,)) == {"--brand-lime": "#C7DE24"}


def test_measure_palette_rejects_a_band_that_is_only_a_seam_between_two_colors():
    histogram = [_pixels(RED, 100_000), _pixels(LIME, 200)]

    with pytest.raises(BrandAssetError, match=r"lime covers 0\.20% of the mark"):
        measure_palette(histogram, bands=(LIME_BAND,))


def test_measure_palette_leaves_out_the_pixels_the_mark_was_anti_aliased_with():
    # Same hue and saturation as the fill, and far more numerous — only its alpha keeps this
    # shade of the mark's own edge from being reported as the mark's color.
    histogram = [_pixels(LIME, 1_000), _pixels((199, 222, 60), 50_000, alpha=120)]

    assert measure_palette(histogram, bands=(LIME_BAND,)) == {"--brand-lime": "#C7DE24"}


def test_measure_palette_leaves_out_a_washed_out_pixel_inside_the_band():
    histogram = [_pixels(LIME, 1_000), _pixels(DULL, 50_000)]

    assert measure_palette(histogram, bands=(LIME_BAND,)) == {"--brand-lime": "#C7DE24"}


def test_measure_palette_rejects_a_source_missing_one_of_the_marks_colors():
    histogram = [_pixels(LIME, 1_000)]

    with pytest.raises(BrandAssetError, match=r"green covers 0\.00% of the mark"):
        measure_palette(histogram)


def test_measure_palette_rejects_a_source_with_no_opaque_pixel_at_all():
    histogram = [_pixels(LIME, 1_000, alpha=0)]

    with pytest.raises(BrandAssetError, match="no opaque pixel to measure"):
        measure_palette(histogram)
