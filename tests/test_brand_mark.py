from __future__ import annotations

import pytest
from tools.brand_mark import (
    MARK_COVERAGE,
    BrandAssetError,
    measure_palette,
    square_frame,
)

# The mark's lime, and the neighbours a shading gradient spreads it over. Every value here
# reads as hue 67° at saturation 0.73, well inside the band and well past the saturation floor.
LIME = (199, 222, 36)
# Opaque, saturated, and nowhere near the lime band: filler that decides how large a share of
# the mark the lime covers.
RED = (200, 30, 30)
# Inside the lime band by hue, but too washed out to be paint.
DULL = (130, 132, 120)


def _pixels(color, count, alpha=255):
    return (count, (*color, alpha))


def _lime_spread(peak, runner_up):
    """A lime fill spread over a shading gradient, with two neighbouring values in the lead.

    The gradient is flat across blue 30..42, so its mean is exactly 36 whatever happens at the
    peak — which is where a mode is decided.
    """
    counts = dict.fromkeys(range(30, 43), 500)
    counts[36] += peak
    counts[37] += runner_up
    return [_pixels((199, 222, blue), count) for blue, count in counts.items()]


def test_square_frame_pads_the_mark_out_to_the_coverage_share():
    frame = square_frame((0, 0, 95, 95))

    assert frame.size == round(95 / MARK_COVERAGE)
    assert (frame.left, frame.top) == (2, 2)


def test_square_frame_ignores_the_margin_the_source_was_drawn_with():
    tight = square_frame((0, 0, 95, 95))
    roomy = square_frame((40, 300, 135, 395))

    assert tight == roomy


def test_square_frame_sizes_a_tall_mark_from_its_longest_side():
    frame = square_frame((0, 0, 50, 95))

    assert frame.size == round(95 / MARK_COVERAGE)
    assert (frame.left, frame.top) == (25, 2)


def test_square_frame_rejects_a_source_with_nothing_drawn_on_it():
    with pytest.raises(BrandAssetError, match="no opaque pixel to frame"):
        square_frame(None)


def test_measure_palette_reports_the_band_mean_rather_than_its_most_frequent_pixel():
    histogram = [_pixels((199, 222, 36), 40), _pixels((199, 222, 38), 60)]

    # The mode is blue 38 (#C7DE26); the mean of 36 and 38 weighted 40:60 is 37.2.
    assert measure_palette(histogram) == {"lime": "#C7DE25"}


def test_measure_palette_holds_still_when_a_re_encode_reverses_the_leading_pixel():
    leaning_dark = _lime_spread(peak=600, runner_up=590)
    leaning_light = _lime_spread(peak=590, runner_up=600)

    # A mode would move from blue 36 to blue 37 on a ten-pixel swing out of 7,690.
    assert measure_palette(leaning_dark) == measure_palette(leaning_light) == {"lime": "#C7DE24"}


def test_measure_palette_rejects_a_band_that_is_only_a_seam_between_two_colors():
    histogram = [_pixels(RED, 100_000), _pixels(LIME, 200)]

    with pytest.raises(BrandAssetError, match=r"lime covers 0\.20% of the mark"):
        measure_palette(histogram)


def test_measure_palette_leaves_out_the_pixels_the_mark_was_anti_aliased_with():
    histogram = [_pixels(LIME, 1_000), _pixels((199, 222, 200), 50_000, alpha=120)]

    assert measure_palette(histogram) == {"lime": "#C7DE24"}


def test_measure_palette_leaves_out_a_washed_out_pixel_inside_the_band():
    histogram = [_pixels(LIME, 1_000), _pixels(DULL, 50_000)]

    assert measure_palette(histogram) == {"lime": "#C7DE24"}


def test_measure_palette_rejects_a_source_with_no_opaque_pixel_at_all():
    histogram = [_pixels(LIME, 1_000, alpha=0)]

    with pytest.raises(BrandAssetError, match="no opaque pixel to measure"):
        measure_palette(histogram)
