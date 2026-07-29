# /// script
# requires-python = ">=3.14"
# dependencies = ["pillow>=12.0.0"]
# ///
"""Generate every Baibai App brand asset from the single source logo.

The web UI needs the same mark in five shapes — a header image, a favicon, two
maskable PWA icons and an iOS home-screen icon — each with its own size, padding
and transparency rules. Deriving them here keeps the logo a one-file change: swap
`ui/brand/logo.png`, run this, and the whole set follows.

That source is a square RGBA mark standing on a transparent ground and drawn to fill
roughly 95% of its canvas. The header image is the source scaled down whole and the
maskable insets are tuned against that framing, so padding left around the mark shrinks
it in the header and in every launcher icon at once.

The run also reports the source's own lime and gold, so the palette tokens that
name those colors (`--brand-lime`, `--accent-display`) can be checked against the
image they claim to come from rather than against memory.

Run it with `uv run --script tools/generate_brand_assets.py`. Pillow is declared
above rather than in the project's dependency groups: this runs a few times a year
when the logo changes, and putting a native image library in the shared lock would
install it in every daily workflow and keep it in the audit surface for good.
"""

from __future__ import annotations

import argparse
import colorsys
import json
import sys
from collections import Counter
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "ui" / "brand" / "logo.png"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "ui" / "public"
# Written next to the source so the palette can be checked against the image it claims to
# come from: `ui/tests/brand.test.ts` reads this, not the developer's memory.
MEASUREMENT_FILENAME = "measured-colors.json"

WHITE = (255, 255, 255)
# A maskable icon may be cropped to any shape inscribed in the canvas, so the mark
# is drawn at 80% and centered: that keeps it inside the safe area every launcher
# honors. The iOS mask is a rounded square that crops far less, so its icon fills more.
MASKABLE_SCALE = 0.80
APPLE_TOUCH_SCALE = 0.90
FAVICON_SIZES = (16, 32)
HEADER_SIZE = 192
APPLE_TOUCH_SIZE = 180
MASKABLE_SIZES = (192, 512)

# Hue bands (HSL degrees) that separate the mark's three colors. The logo's lime sits
# near 65°, its leaf near 40° and its shading near 95°, so the bands report the lime
# and the gold without depending on where either sits in the composition.
GOLD_HUE_RANGE = (20.0, 55.0)
LIME_HUE_RANGE = (55.0, 85.0)
MIN_SATURATION = 0.35
OPAQUE_ALPHA = 250


class BrandAssetError(RuntimeError):
    """The source image cannot produce the asset set."""


def load_source(path: Path) -> Image.Image:
    if not path.is_file():
        message = f"source logo not found: {path}"
        raise BrandAssetError(message)
    image = Image.open(path).convert("RGBA")
    if image.width != image.height:
        message = f"source logo must be square, got {image.width}x{image.height}"
        raise BrandAssetError(message)
    # The header image and the favicon are the two outputs that keep their transparency, and
    # both are drawn straight from the source. Artwork delivered as a render still carries the
    # background it was composited on, which would paint a square behind the mark on every
    # surface those two land on — and nothing downstream can tell that from a mark meant to be
    # opaque. Refuse it here instead, where the fix is to cut the background out.
    if image.getchannel("A").getextrema()[0] > 0:
        message = f"source logo stands on no transparent ground: {display(path)}"
        raise BrandAssetError(message)
    return image


def dominant_hex(image: Image.Image, hue_range: tuple[float, float]) -> str | None:
    """Return the most frequent opaque color whose hue falls inside the band."""
    low, high = hue_range
    colors = image.getcolors(maxcolors=image.width * image.height)
    if colors is None:
        message = "source logo has more distinct colors than pixels"
        raise BrandAssetError(message)
    counter: Counter[tuple[int, int, int]] = Counter()
    for count, (red, green, blue, alpha) in colors:
        if alpha < OPAQUE_ALPHA:
            continue
        hue, _, saturation = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
        if saturation < MIN_SATURATION:
            continue
        if low <= hue * 360 < high:
            counter[(red, green, blue)] += count
    if not counter:
        return None
    red, green, blue = counter.most_common(1)[0][0]
    return f"#{red:02X}{green:02X}{blue:02X}"


def on_white(image: Image.Image, size: int, scale: float) -> Image.Image:
    """Draw the mark centered at `scale` of the canvas over an opaque white square."""
    canvas = Image.new("RGB", (size, size), WHITE)
    inner = max(1, round(size * scale))
    mark = image.resize((inner, inner), Image.LANCZOS)
    offset = (size - inner) // 2
    canvas.paste(mark, (offset, offset), mark)
    return canvas


def write_png(image: Image.Image, path: Path) -> None:
    image.save(path, format="PNG", optimize=True)


def write_favicon(image: Image.Image, path: Path) -> None:
    largest = max(FAVICON_SIZES)
    image.resize((largest, largest), Image.LANCZOS).save(
        path, format="ICO", sizes=[(size, size) for size in FAVICON_SIZES]
    )


def display(path: Path) -> str:
    """Repo-relative where that reads better, absolute where it would not."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path.resolve())


def generate(source_path: Path, output_dir: Path) -> list[Path]:
    source = load_source(source_path)
    outputs = {
        "logo.png": lambda: source.resize((HEADER_SIZE, HEADER_SIZE), Image.LANCZOS),
        **{
            f"icon-{size}.png": (lambda size=size: on_white(source, size, MASKABLE_SCALE))
            for size in MASKABLE_SIZES
        },
        "apple-touch-icon.png": lambda: on_white(source, APPLE_TOUCH_SIZE, APPLE_TOUCH_SCALE),
    }
    paths = [output_dir / name for name in (*outputs, "favicon.ico")]

    # The source is read from disk and the outputs are written to it, so an output dir
    # holding the source would replace the original with a 192px derivative — and every
    # later run would then shrink it again. Refuse before anything is written.
    resolved_source = source_path.resolve()
    collision = next((path for path in paths if path.resolve() == resolved_source), None)
    if collision is not None:
        message = f"output would overwrite the source logo: {display(collision)}"
        raise BrandAssetError(message)

    output_dir.mkdir(parents=True, exist_ok=True)
    for name, render in outputs.items():
        write_png(render(), output_dir / name)
    write_favicon(source, output_dir / "favicon.ico")

    lime = dominant_hex(source, LIME_HUE_RANGE)
    gold = dominant_hex(source, GOLD_HUE_RANGE)
    # The palette names these two as coming from the logo, so a source without them cannot
    # answer what the tokens should be. Say so instead of writing nulls the checks skip.
    missing = [name for name, value in (("lime", lime), ("gold", gold)) if value is None]
    if missing:
        message = (
            f"source logo has no {' and no '.join(missing)} to measure: {display(source_path)}"
        )
        raise BrandAssetError(message)
    measured = {"lime": lime, "gold": gold}
    measurement_path = source_path.parent / MEASUREMENT_FILENAME
    measurement_path.write_text(json.dumps(measured, indent=2) + "\n", encoding="utf-8")

    print(f"source: {display(source_path)} ({source.width}x{source.height})")
    print(f"measured lime (--brand-lime):     {lime}")
    print(f"measured gold (--accent-display): {gold}")
    for path in paths:
        print(f"wrote {display(path)} ({path.stat().st_size} bytes)")
    print(f"wrote {display(measurement_path)}")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    try:
        generate(args.source, args.output_dir)
    except (BrandAssetError, OSError, UnidentifiedImageError, DecompressionBombError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
