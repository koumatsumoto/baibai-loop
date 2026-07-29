"""What `tools/generate_brand_assets.py` produces, exercised through its command line.

The generator declares Pillow inline and runs under `uv run --script`, so the project's lock
deliberately does not carry it and this file is skipped wherever it is absent. CI runs it in a
step of its own that supplies Pillow as a throwaway overlay (`.github/workflows/ci.yml`), which
is what keeps the contract below from resting on whatever a developer happens to have installed.
The rules the assets are built from are checked without Pillow in `tests/test_brand_mark.py`;
what is left to see here is that they reach the files on disk.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from tools.brand_mark import APPLE_TOUCH_SIZE, FAVICON_SIZES, HEADER_SIZE

Image = pytest.importorskip("PIL.Image", reason="Pillow is a script dependency, not a locked one")

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "tools" / "generate_brand_assets.py"
ASSET_NAMES = ("logo.png", "icon-192.png", "icon-512.png", "apple-touch-icon.png", "favicon.ico")
MEASURED = {"--brand-lime": "#C7DE24", "--brand-green": "#09AB65"}

LIME = (199, 222, 36, 255)
GREEN = (9, 171, 101, 255)
GRAY = (120, 122, 121, 255)


def _source(directory, *, canvas, mark, offset, colors=(LIME, GREEN)):
    """A square mark of `mark` pixels placed at `offset` on a transparent `canvas`.

    The mark is banded in `colors` so the run has each of the palette's brand colors to read.
    """
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    band = mark // len(colors)
    for index, color in enumerate(colors):
        image.paste(Image.new("RGBA", (mark, band), color), (offset[0], offset[1] + index * band))
    path = directory / "logo.png"
    image.save(path)
    return path


def _generate(source, output_dir):
    return subprocess.run(
        [sys.executable, str(GENERATOR), "--source", str(source), "--output-dir", str(output_dir)],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )


def _painted(icon):
    """The box the mark occupies on an opaque white icon."""
    return Image.eval(icon.convert("L"), lambda level: 255 - level).getbbox()


def test_generate_writes_every_asset_and_the_measurement(tmp_path):
    source = _source(tmp_path / "brand", canvas=400, mark=200, offset=(100, 100))
    output_dir = tmp_path / "public"

    result = _generate(source, output_dir)

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in output_dir.iterdir()) == sorted(ASSET_NAMES)
    measured = json.loads((source.parent / "measured-colors.json").read_text(encoding="utf-8"))
    assert measured == MEASURED


def test_generate_keeps_the_header_image_transparent_and_the_touch_icon_opaque(tmp_path):
    source = _source(tmp_path / "brand", canvas=400, mark=200, offset=(100, 100))
    output_dir = tmp_path / "public"

    _generate(source, output_dir)

    header = Image.open(output_dir / "logo.png")
    assert header.size == (HEADER_SIZE, HEADER_SIZE)
    assert header.mode == "RGBA"
    assert header.getchannel("A").getextrema()[0] == 0
    touch = Image.open(output_dir / "apple-touch-icon.png")
    assert touch.size == (APPLE_TOUCH_SIZE, APPLE_TOUCH_SIZE)
    assert "A" not in touch.getbands()


def test_generate_keeps_the_maskable_icons_inside_the_launcher_safe_area(tmp_path):
    source = _source(tmp_path / "brand", canvas=400, mark=200, offset=(100, 100))
    output_dir = tmp_path / "public"

    _generate(source, output_dir)

    # A launcher may crop the icon to any shape inscribed in the canvas, so the mark has to stay
    # off the edges. The bounds are written out rather than derived from the inset the generator
    # applies, so widening that inset until a circular mask clips the mark fails here: a mark
    # drawn edge to edge across the safe area still leaves a tenth of the canvas on every side.
    for name in ("icon-192.png", "icon-512.png"):
        icon = Image.open(output_dir / name)
        painted = _painted(icon)
        assert painted is not None, name
        assert painted[0] >= icon.width * 0.10, name
        assert painted[1] >= icon.height * 0.10, name
        assert painted[2] <= icon.width * 0.90, name
        assert painted[3] <= icon.height * 0.90, name


def test_generate_fills_the_touch_icon_more_than_the_maskable_ones(tmp_path):
    source = _source(tmp_path / "brand", canvas=400, mark=200, offset=(100, 100))
    output_dir = tmp_path / "public"

    _generate(source, output_dir)

    def share(name):
        icon = Image.open(output_dir / name)
        painted = _painted(icon)
        return (painted[2] - painted[0]) / icon.width

    # The iOS mask is a rounded square that crops far less than a launcher's, so its icon is
    # drawn larger — and both stay clear of filling the canvas outright.
    assert share("icon-192.png") < share("apple-touch-icon.png") < 0.95


def test_generate_keeps_both_favicon_frames(tmp_path):
    source = _source(tmp_path / "brand", canvas=400, mark=200, offset=(100, 100))
    output_dir = tmp_path / "public"

    _generate(source, output_dir)

    with Image.open(output_dir / "favicon.ico") as icon:
        assert sorted(icon.info["sizes"]) == [(size, size) for size in sorted(FAVICON_SIZES)]


def test_generate_places_the_mark_the_same_whatever_margin_the_source_was_drawn_with(tmp_path):
    tight = _source(tmp_path / "tight", canvas=220, mark=200, offset=(10, 10))
    roomy = _source(tmp_path / "roomy", canvas=800, mark=200, offset=(30, 540))

    _generate(tight, tmp_path / "tight-out")
    _generate(roomy, tmp_path / "roomy-out")

    for name in ASSET_NAMES:
        assert (tmp_path / "tight-out" / name).read_bytes() == (
            tmp_path / "roomy-out" / name
        ).read_bytes(), name


def test_generate_ignores_a_pixel_too_faint_for_a_viewer_to_see(tmp_path):
    clean = _source(tmp_path / "clean", canvas=800, mark=200, offset=(300, 300))
    specked = _source(tmp_path / "specked", canvas=800, mark=200, offset=(300, 300))
    with Image.open(specked) as image:
        dusty = image.convert("RGBA")
    # The dust an export leaves in a corner: opaque enough for getbbox(), invisible on any
    # background. Framing from it would stretch the frame to the whole canvas.
    dusty.putpixel((0, 0), (255, 0, 0, 1))
    dusty.putpixel((799, 799), (255, 0, 0, 1))
    dusty.save(specked)

    assert _generate(clean, tmp_path / "clean-out").returncode == 0
    assert _generate(specked, tmp_path / "specked-out").returncode == 0

    for name in ASSET_NAMES:
        assert (tmp_path / "clean-out" / name).read_bytes() == (
            tmp_path / "specked-out" / name
        ).read_bytes(), name


def test_generate_frames_a_pixel_a_viewer_can_see(tmp_path):
    plain = _source(tmp_path / "plain", canvas=800, mark=200, offset=(300, 300))
    marked = _source(tmp_path / "marked", canvas=800, mark=200, offset=(300, 300))
    with Image.open(marked) as image:
        visible = image.convert("RGBA")
    # Half-opaque, so it reads as part of the artwork and the frame has to reach it.
    visible.putpixel((0, 0), (255, 0, 0, 128))
    visible.save(marked)

    _generate(plain, tmp_path / "plain-out")
    _generate(marked, tmp_path / "marked-out")

    assert (tmp_path / "plain-out" / "logo.png").read_bytes() != (
        tmp_path / "marked-out" / "logo.png"
    ).read_bytes()


def test_generate_leaves_the_previous_assets_alone_when_the_source_cannot_be_measured(tmp_path):
    source = _source(tmp_path / "brand", canvas=400, mark=200, offset=(100, 100), colors=(GRAY,))
    output_dir = tmp_path / "public"
    output_dir.mkdir()
    for name in ASSET_NAMES:
        (output_dir / name).write_bytes(b"the mark that is still deployed")

    result = _generate(source, output_dir)

    assert result.returncode == 1
    assert "lime covers 0.00% of the mark" in result.stderr
    for name in ASSET_NAMES:
        assert (output_dir / name).read_bytes() == b"the mark that is still deployed", name
    assert not (source.parent / "measured-colors.json").exists()


def test_generate_refuses_a_source_that_stands_on_an_opaque_ground(tmp_path):
    directory = tmp_path / "brand"
    directory.mkdir(parents=True)
    Image.new("RGBA", (400, 400), LIME).save(directory / "logo.png")

    result = _generate(directory / "logo.png", tmp_path / "public")

    assert result.returncode == 1
    assert "stands on no transparent ground" in result.stderr


def test_generate_refuses_to_overwrite_the_source_it_reads(tmp_path):
    source = _source(tmp_path / "brand", canvas=400, mark=200, offset=(100, 100))

    result = _generate(source, source.parent)

    assert result.returncode == 1
    assert "would overwrite the source logo" in result.stderr
    assert not (source.parent / "icon-192.png").exists()


def test_generate_refuses_to_measure_a_repo_source_into_images_written_elsewhere(tmp_path):
    result = _generate(REPO_ROOT / "ui" / "brand" / "logo.png", tmp_path / "preview")

    assert result.returncode == 1
    assert "while the measurement lands in" in result.stderr
    assert not (tmp_path / "preview").exists()
