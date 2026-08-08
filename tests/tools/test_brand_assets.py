"""What `tools/generators/generate_brand_assets.py` produces, exercised through its CLI.

The generator declares Pillow inline and runs under `uv run --script`, so the project's lock
deliberately does not carry it and this file is skipped wherever it is absent. CI runs it in a
step of its own that supplies Pillow as a throwaway overlay (`.github/workflows/ci.yml`), which
is what keeps the contract below from resting on whatever a developer happens to have installed.
The rules the assets are built from are checked without Pillow in `tests/tools/test_brand_mark.py`;
what is left to see here is that they reach the files on disk.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from tools.generators.brand_mark import APPLE_TOUCH_SIZE, FAVICON_SIZES, HEADER_SIZE

Image = pytest.importorskip("PIL.Image", reason="Pillow is a script dependency, not a locked one")

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "tools" / "generators" / "generate_brand_assets.py"
COMMITTED_LOGO = REPO_ROOT / "web" / "frontend" / "brand" / "logo.png"
COMMITTED_MEASUREMENT = REPO_ROOT / "web" / "frontend" / "brand" / "measured-colors.json"
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


def _span(icon):
    """How much of an icon's width the mark takes up."""
    painted = _painted(icon)
    return (painted[2] - painted[0]) / icon.width


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """One run of the generator, shared by the cases that only read what it produced."""
    root = tmp_path_factory.mktemp("rendered")
    source = _source(root / "brand", canvas=400, mark=200, offset=(100, 100))
    output_dir = root / "public"
    result = _generate(source, output_dir)
    assert result.returncode == 0, result.stderr
    return source, output_dir


def test_generate_gives_each_asset_the_shape_it_is_used_at(rendered):
    source, output_dir = rendered

    assert sorted(path.name for path in output_dir.iterdir()) == sorted(ASSET_NAMES)
    measured = json.loads((source.parent / "measured-colors.json").read_text(encoding="utf-8"))
    assert measured == MEASURED
    # The header image and the favicon keep their transparency; the two icons that land on a
    # home screen are flattened, because a transparent PWA icon renders as a black square.
    header = Image.open(output_dir / "logo.png")
    assert (header.size, header.mode) == ((HEADER_SIZE, HEADER_SIZE), "RGBA")
    assert header.getchannel("A").getextrema()[0] == 0
    touch = Image.open(output_dir / "apple-touch-icon.png")
    assert touch.size == (APPLE_TOUCH_SIZE, APPLE_TOUCH_SIZE)
    assert "A" not in touch.getbands()
    with Image.open(output_dir / "favicon.ico") as icon:
        assert sorted(icon.info["sizes"]) == [(size, size) for size in sorted(FAVICON_SIZES)]
    # The iOS mask is a rounded square that crops far less than a launcher's, so its icon is
    # drawn larger — and neither fills the canvas outright.
    assert _span(Image.open(output_dir / "icon-192.png")) < _span(touch) < 0.95


def test_generate_keeps_the_maskable_icons_inside_the_launcher_safe_area(rendered):
    _, output_dir = rendered

    # The one shape a viewer cannot check by looking at the app: a launcher may crop the icon to
    # any inscribed shape, and only an installed Android home screen shows the result. The bounds
    # are written out rather than derived from the inset the generator applies, so widening that
    # inset until a circular mask clips the mark fails here.
    for name in ("icon-192.png", "icon-512.png"):
        icon = Image.open(output_dir / name)
        painted = _painted(icon)
        assert painted is not None, name
        assert painted[0] >= icon.width * 0.10, name
        assert painted[1] >= icon.height * 0.10, name
        assert painted[2] <= icon.width * 0.90, name
        assert painted[3] <= icon.height * 0.90, name


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


def test_generate_frames_the_soft_edge_around_the_mark(tmp_path):
    plain = _source(tmp_path / "plain", canvas=800, mark=200, offset=(300, 300))
    softened = _source(tmp_path / "softened", canvas=800, mark=200, offset=(300, 300))
    with Image.open(softened) as image:
        edged = image.convert("RGBA")
    # Half-opaque: a feathered edge or a shadow is part of the artwork, so the frame reaches it
    # rather than cropping it — the counterpart to the faint speck above, which it does not.
    edged.putpixel((280, 300), (255, 0, 0, 128))
    edged.save(softened)

    _generate(plain, tmp_path / "plain-out")
    result = _generate(softened, tmp_path / "softened-out")

    assert (tmp_path / "plain-out" / "logo.png").read_bytes() != (
        tmp_path / "softened-out" / "logo.png"
    ).read_bytes()
    # The run reports what the paint actually spans, which a soft edge pushes away from the
    # coverage the frame was sized to: 200px of paint on the 232px canvas that 220px of visible
    # mark asks for. Reading the constant back out would say 95% of every source ever passed.
    assert "paint spans 86.2% of it" in result.stdout


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


def test_generate_writes_the_measurement_before_the_images(tmp_path):
    # A file where the output dir belongs, so the images cannot be written. The measurement going
    # first is what makes a half-applied swap fail loudly: `web/frontend/tests/brand.test.ts` reads the
    # measurement against the palette, so a measurement without its images is a red test rather
    # than a deployed mark nothing describes.
    source = _source(tmp_path / "brand", canvas=400, mark=200, offset=(100, 100))
    blocked = tmp_path / "public"
    blocked.write_bytes(b"not a directory")

    result = _generate(source, blocked)

    assert result.returncode == 1
    assert json.loads((source.parent / "measured-colors.json").read_text(encoding="utf-8")) == (
        MEASURED
    )


def test_the_committed_measurement_is_what_the_committed_logo_measures(tmp_path):
    # The one case that reaches the artwork. Every other source here is synthetic, and
    # `web/frontend/tests/brand.test.ts` holds the palette to the measurement rather than to the image — so
    # without this, the palette and the measurement can agree on a color the logo is not painted
    # in and the whole suite stays green.
    source = tmp_path / "brand" / "logo.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(COMMITTED_LOGO.read_bytes())

    result = _generate(source, tmp_path / "out")

    assert result.returncode == 0, result.stderr
    measured = json.loads((source.parent / "measured-colors.json").read_text(encoding="utf-8"))
    assert measured == json.loads(COMMITTED_MEASUREMENT.read_text(encoding="utf-8"))


def test_the_measurement_moves_at_most_a_step_when_the_artwork_is_re_rendered(tmp_path):
    # Rendering the same mark at another resolution re-draws every anti-aliased pixel, which is
    # what a designer re-exporting the artwork does. No way of reading a color out of it is
    # invariant under that, so the contract is that it moves by at most one step per channel —
    # which is under the perceptual bar `web/frontend/tests/brand.test.ts` holds the palette to.
    original = tmp_path / "original" / "logo.png"
    original.parent.mkdir(parents=True)
    original.write_bytes(COMMITTED_LOGO.read_bytes())
    rerendered = tmp_path / "rerendered" / "logo.png"
    rerendered.parent.mkdir(parents=True)
    with Image.open(original) as image:
        mark = image.convert("RGBA")
    smaller = mark.resize((mark.width * 2 // 3,) * 2, Image.LANCZOS)
    smaller.resize((mark.width,) * 2, Image.LANCZOS).save(rerendered)

    assert _generate(original, tmp_path / "original-out").returncode == 0
    assert _generate(rerendered, tmp_path / "rerendered-out").returncode == 0

    before = json.loads((original.parent / "measured-colors.json").read_text(encoding="utf-8"))
    after = json.loads((rerendered.parent / "measured-colors.json").read_text(encoding="utf-8"))
    assert before.keys() == after.keys()
    for token, value in before.items():
        drift = [
            abs(int(value[start : start + 2], 16) - int(after[token][start : start + 2], 16))
            for start in (1, 3, 5)
        ]
        assert max(drift) <= 1, f"{token}: {value} -> {after[token]}"
