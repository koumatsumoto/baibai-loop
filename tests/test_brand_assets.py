"""What `tools/generate_brand_assets.py` produces, exercised through its command line.

The generator declares Pillow inline and runs under `uv run --script`, so the project's lock
deliberately does not carry it and this file is skipped wherever it is absent — including CI,
which reports the skip. It runs where the generator itself is run: a checkout that has just
rendered a new logo. The rules the assets are built from are checked without Pillow in
`tests/test_brand_mark.py`; what is left to see here is that they reach the files on disk.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from tools.brand_mark import APPLE_TOUCH_SIZE, FAVICON_SIZES, HEADER_SIZE, MASKABLE_SCALE

Image = pytest.importorskip("PIL.Image", reason="Pillow is a script dependency, not a locked one")

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "tools" / "generate_brand_assets.py"
ASSET_NAMES = ("logo.png", "icon-192.png", "icon-512.png", "apple-touch-icon.png", "favicon.ico")

LIME = (199, 222, 36, 255)
GRAY = (120, 122, 121, 255)


def _source(directory, *, canvas, mark, offset, color=LIME):
    """A square mark of `mark` pixels placed at `offset` on a transparent `canvas`."""
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (mark, mark), color), offset)
    path = directory / "logo.png"
    image.save(path)
    return path


def _generate(source, output_dir):
    return subprocess.run(
        [sys.executable, str(GENERATOR), "--source", str(source), "--output-dir", str(output_dir)],
        capture_output=True,
        check=False,
        text=True,
    )


def test_generate_writes_every_asset_and_the_measurement(tmp_path):
    source = _source(tmp_path / "brand", canvas=400, mark=200, offset=(100, 100))
    output_dir = tmp_path / "public"

    result = _generate(source, output_dir)

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in output_dir.iterdir()) == sorted(ASSET_NAMES)
    measured = json.loads((source.parent / "measured-colors.json").read_text(encoding="utf-8"))
    assert measured == {"lime": "#C7DE24"}


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

    for name in ("icon-192.png", "icon-512.png"):
        icon = Image.open(output_dir / name)
        size = icon.width
        margin = round(size * (1 - MASKABLE_SCALE) / 2)
        painted = Image.eval(icon.convert("L"), lambda level: 255 - level).getbbox()
        assert painted is not None
        assert painted[0] >= margin, name
        assert painted[1] >= margin, name
        assert painted[2] <= size - margin, name
        assert painted[3] <= size - margin, name


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


def test_generate_leaves_the_previous_assets_alone_when_the_source_cannot_be_measured(tmp_path):
    source = _source(tmp_path / "brand", canvas=400, mark=200, offset=(100, 100), color=GRAY)
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
