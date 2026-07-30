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


def test_check_destinations_accepts_the_repos_own_source_and_asset_directory():
    check_destinations(DEFAULT_SOURCE, DEFAULT_OUTPUT_DIR)


def test_check_destinations_accepts_a_source_and_output_that_are_both_outside_the_repo():
    check_destinations(OUTSIDE, OUTSIDE.parent / "out")


def test_check_destinations_refuses_repo_measurement_with_images_written_elsewhere():
    with pytest.raises(BrandAssetError, match="while the measurement lands in"):
        check_destinations(DEFAULT_SOURCE, Path("/tmp/preview"))


def test_check_destinations_refuses_a_foreign_source_rendered_into_the_repos_assets():
    # The half that goes unnoticed: the repo would carry images of a mark that its own logo.png,
    # its measurement and its palette all disagree with, and only a binary diff would show it.
    with pytest.raises(BrandAssetError, match="while the measurement lands in"):
        check_destinations(OUTSIDE, DEFAULT_OUTPUT_DIR)


def test_check_destinations_refuses_an_output_directory_holding_the_source():
    with pytest.raises(BrandAssetError, match="would overwrite the source logo"):
        check_destinations(REPO_ROOT / "ui" / "public" / "logo.png", DEFAULT_OUTPUT_DIR)


def test_square_frame_pads_the_mark_out_to_the_coverage_share():
    frame = square_frame((0, 0, 95, 95), (2, 2, 93, 93))

    assert frame.size == round(95 / MARK_COVERAGE)
    assert (frame.left, frame.top) == (2, 2)


def test_square_frame_ignores_the_margin_the_source_was_drawn_with():
    tight = square_frame((0, 0, 95, 95), (2, 2, 93, 93))
    roomy = square_frame((40, 300, 135, 395), (42, 302, 133, 393))

    assert tight == roomy


def test_square_frame_sizes_a_tall_mark_from_its_longest_side():
    frame = square_frame((0, 0, 50, 95), (2, 2, 48, 93))

    assert frame.size == round(95 / MARK_COVERAGE)
    assert (frame.left, frame.top) == (25, 2)


def test_square_frame_reports_what_the_paint_spans_rather_than_the_coverage_constant():
    frame = square_frame((0, 0, 100, 100), (10, 10, 90, 90))

    # 80px of paint on a canvas sized from the 100px visible extent, which is 105 wide.
    assert frame.paint_share == pytest.approx(80 / 105)


def test_square_frame_rejects_a_source_with_nothing_visible_on_it():
    with pytest.raises(BrandAssetError, match="no visible pixel to frame"):
        square_frame(None, None)


def test_square_frame_accepts_a_mark_with_a_soft_edge_around_it():
    # A generous drop shadow on the mark this repo ships reaches 1.11x past the opaque paint.
    frame = square_frame((0, 0, 111, 111), (5, 5, 105, 105))

    assert frame.size == round(111 / MARK_COVERAGE)


def test_square_frame_rejects_a_mark_whose_reach_is_dust_rather_than_a_soft_edge():
    # A single faint speck in the corner of a canvas: the visible extent has nothing to do with
    # where the paint is, and framing from it would shrink the mark in every asset at once.
    with pytest.raises(BrandAssetError, match=r"reaches 2\.5x past its own paint"):
        square_frame((0, 0, 500, 500), (300, 300, 500, 500))


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
