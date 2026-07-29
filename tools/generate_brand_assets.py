# /// script
# requires-python = ">=3.14"
# dependencies = ["pillow>=12.0.0"]
# ///
"""Generate every Baibai App brand asset from the single source logo.

The web UI needs the same mark in five shapes — a header image, a favicon, two
maskable PWA icons and an iOS home-screen icon — each with its own size, padding
and transparency rules. Deriving them here keeps the logo a one-file change: swap
`ui/brand/logo.png`, run this, and the whole set follows.

That source is an RGBA mark standing on a transparent ground. How it is framed is not part
of the contract: every asset is rendered from the mark re-centered on a square canvas it
fills to a fixed share, so artwork delivered with more room around it still lands at the
same size in the header and inside every launcher icon.

The run also reports the mark's own lime and green, so the palette entries that name them
can be checked against the image they claim to come from rather than against memory. The
whole set — five images and that measurement — is rendered in memory before anything is
written, and the measurement goes to disk first: a swap that only half applies would
otherwise leave `ui/public/` carrying the new mark while the palette and its measurement
still describe the old one, and no check downstream reads the images.

Run it with `uv run --script tools/generate_brand_assets.py`. Pillow is declared
above rather than in the project's dependency groups: this runs a few times a year
when the logo changes, and putting a native image library in the shared lock would
install it in every daily workflow and keep it in the audit surface for good. The rules
applied here live in `tools/brand_mark.py`, which the type check and the test run do reach;
`.github/workflows/ci.yml` runs the end-to-end test with Pillow as a throwaway overlay.
"""

from __future__ import annotations

import argparse
import json
import sys
from io import BytesIO
from pathlib import Path

# `uv run --script` puts this file's directory on the import path and the repo root
# nowhere, so the rules module is imported under its own name rather than through `tools.`.
from brand_mark import (
    APPLE_TOUCH_SCALE,
    APPLE_TOUCH_SIZE,
    BANDS,
    FAVICON_SIZES,
    HEADER_SIZE,
    MARK_COVERAGE,
    MASKABLE_SCALE,
    MASKABLE_SIZES,
    VISIBLE_ALPHA,
    BrandAssetError,
    measure_palette,
    square_frame,
)
from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "ui" / "brand" / "logo.png"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "ui" / "public"
# Written next to the source so the palette can be checked against the image it claims to
# come from: `ui/tests/brand.test.ts` reads this, not the developer's memory.
MEASUREMENT_FILENAME = "measured-colors.json"

WHITE = (255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)


def load_source(path: Path) -> Image.Image:
    if not path.is_file():
        message = f"source logo not found: {path}"
        raise BrandAssetError(message)
    image = Image.open(path).convert("RGBA")
    # The header image and the favicon are the two outputs that keep their transparency, and
    # both are drawn straight from the source. Artwork delivered as a render still carries the
    # background it was composited on, which would paint a square behind the mark on every
    # surface those two land on — and nothing downstream can tell that from a mark meant to be
    # opaque. Refuse it here instead, where the fix is to cut the background out.
    if image.getchannel("A").getextrema()[0] > 0:
        message = f"source logo stands on no transparent ground: {display(path)}"
        raise BrandAssetError(message)
    return image


def framed(source: Image.Image) -> Image.Image:
    """The mark alone, centered on a transparent square it fills to `MARK_COVERAGE`."""
    visible = source.getchannel("A").point(lambda alpha: 255 if alpha >= VISIBLE_ALPHA else 0)
    box = visible.getbbox()
    frame = square_frame(box)
    canvas = Image.new("RGBA", (frame.size, frame.size), TRANSPARENT)
    # Pasted without a mask so the mark's own alpha is copied rather than composited over the
    # canvas: compositing would leave every transparent pixel carrying the canvas' black.
    canvas.paste(source.crop(box), (frame.left, frame.top))
    return canvas


def on_white(image: Image.Image, size: int, scale: float) -> Image.Image:
    """Draw the mark centered at `scale` of the canvas over an opaque white square."""
    canvas = Image.new("RGB", (size, size), WHITE)
    inner = max(1, round(size * scale))
    mark = image.resize((inner, inner), Image.LANCZOS)
    offset = (size - inner) // 2
    canvas.paste(mark, (offset, offset), mark)
    return canvas


def as_png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def as_favicon(image: Image.Image) -> bytes:
    largest = max(FAVICON_SIZES)
    buffer = BytesIO()
    image.resize((largest, largest), Image.LANCZOS).save(
        buffer, format="ICO", sizes=[(size, size) for size in FAVICON_SIZES]
    )
    return buffer.getvalue()


def display(path: Path) -> str:
    """Repo-relative where that reads better, absolute where it would not."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path.resolve())


def render(mark: Image.Image) -> dict[str, bytes]:
    """Every asset's bytes, so a failure to encode one of them writes none of them."""
    return {
        "logo.png": as_png(mark.resize((HEADER_SIZE, HEADER_SIZE), Image.LANCZOS)),
        **{
            f"icon-{size}.png": as_png(on_white(mark, size, MASKABLE_SCALE))
            for size in MASKABLE_SIZES
        },
        "apple-touch-icon.png": as_png(on_white(mark, APPLE_TOUCH_SIZE, APPLE_TOUCH_SCALE)),
        "favicon.ico": as_favicon(mark),
    }


def check_destinations(
    source_path: Path, output_dir: Path, paths: list[Path], measurement_path: Path
) -> None:
    """Refuse the destinations that would leave the repo describing something it does not hold."""
    # The source is read from disk and the outputs are written to it, so an output dir
    # holding the source would replace the original with a 192px derivative — and every
    # later run would then shrink it again.
    resolved_source = source_path.resolve()
    collision = next((path for path in paths if path.resolve() == resolved_source), None)
    if collision is not None:
        message = f"output would overwrite the source logo: {display(collision)}"
        raise BrandAssetError(message)
    # The measurement belongs beside the source, so a run that sends the images elsewhere while
    # the source still sits in the repo would leave the repo's measurement — and through it the
    # palette check — describing a mark that was never rendered.
    if measurement_path.resolve().is_relative_to(REPO_ROOT) and output_dir.resolve() != (
        DEFAULT_OUTPUT_DIR.resolve()
    ):
        message = (
            f"images would go to {display(output_dir)} while the measurement lands in "
            f"{display(measurement_path)}: render into {display(DEFAULT_OUTPUT_DIR)}, or copy "
            f"the source outside the repo first"
        )
        raise BrandAssetError(message)


def generate(source_path: Path, output_dir: Path) -> list[Path]:
    source = load_source(source_path)
    histogram = source.getcolors(maxcolors=source.width * source.height)
    if histogram is None:
        message = "source logo has more distinct colors than pixels"
        raise BrandAssetError(message)
    # Everything the source is asked is asked, and every byte to be written is produced, before
    # the first one lands: a source this cannot measure or frame leaves the last set untouched.
    measured = measure_palette(histogram)
    mark = framed(source)
    assets = render(mark)
    measurement_path = source_path.parent / MEASUREMENT_FILENAME
    paths = [output_dir / name for name in assets]
    check_destinations(source_path, output_dir, paths, measurement_path)

    measurement_path.write_text(json.dumps(measured, indent=2) + "\n", encoding="utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, data in assets.items():
        (output_dir / name).write_bytes(data)

    print(f"source: {display(source_path)} ({source.width}x{source.height})")
    print(f"framed: {mark.width}x{mark.height}, mark at {MARK_COVERAGE:.0%} of the canvas")
    for band in BANDS:
        print(f"measured {band.name} ({band.custom_property}): {measured[band.custom_property]}")
    print(f"wrote {display(measurement_path)}")
    for path in paths:
        print(f"wrote {display(path)} ({path.stat().st_size} bytes)")
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
