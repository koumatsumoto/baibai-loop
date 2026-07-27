"""Generate every Baibai App brand asset from the single source logo.

The web UI needs the same mark in five shapes — a header image, a favicon, two
maskable PWA icons and an iOS home-screen icon — each with its own size, padding
and transparency rules. Deriving them here keeps the logo a one-file change: swap
`ui/brand/logo.png`, run this, and the whole set follows.

The run also reports the source's own lime and gold, so the palette tokens that
name those colors (`--brand-lime`, `--accent-display`) can be checked against the
image they claim to come from rather than against memory.
"""

from __future__ import annotations

import argparse
import colorsys
import sys
from collections import Counter
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "ui" / "brand" / "logo.png"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "ui" / "public"

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


def generate(source_path: Path, output_dir: Path) -> list[Path]:
    source = load_source(source_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    header = output_dir / "logo.png"
    write_png(source.resize((HEADER_SIZE, HEADER_SIZE), Image.LANCZOS), header)

    favicon = output_dir / "favicon.ico"
    write_favicon(source, favicon)

    written = [header, favicon]
    for size in MASKABLE_SIZES:
        icon = output_dir / f"icon-{size}.png"
        write_png(on_white(source, size, MASKABLE_SCALE), icon)
        written.append(icon)

    apple = output_dir / "apple-touch-icon.png"
    write_png(on_white(source, APPLE_TOUCH_SIZE, APPLE_TOUCH_SCALE), apple)
    written.append(apple)

    lime = dominant_hex(source, LIME_HUE_RANGE)
    gold = dominant_hex(source, GOLD_HUE_RANGE)
    print(f"source: {source_path} ({source.width}x{source.height})")
    print(f"measured lime (--brand-lime):    {lime or 'not found'}")
    print(f"measured gold (--accent-display): {gold or 'not found'}")
    for path in written:
        print(f"wrote {path.relative_to(REPO_ROOT)} ({path.stat().st_size} bytes)")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    try:
        generate(args.source, args.output_dir)
    except BrandAssetError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
