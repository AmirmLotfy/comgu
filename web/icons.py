"""Derive every icon Comgu needs from the two source logo files.

The source artwork lives in `comgu-logo/` and is the only thing a designer
touches:

    1.png  charcoal mark, transparent background — for light backgrounds
    2.png  white mark, transparent background    — for dark backgrounds

Everything else is generated, so the icons cannot drift from the artwork.
Run it after changing either source file:

    python web/icons.py

Two things are worth knowing about the output:

  * Favicons ship in both colours. A charcoal mark disappears against a dark
    browser tab strip, so the pages link the white variant behind
    `prefers-color-scheme: dark`.
  * Touch and maskable icons are flattened onto the brand ivory. iOS composites
    a transparent icon onto black, which would swallow the charcoal mark.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "comgu-logo"
OUT = ROOT / "apps" / "api" / "static" / "marketing"

IVORY = (246, 242, 233, 255)  # --ivory
INK = (26, 28, 31, 255)       # the mark's own charcoal

# Transparent favicons keep the mark edge-to-edge; icons on a plate get room to
# breathe, matching how platform icons are normally drawn.
PLATE_PADDING = 0.16


def _load(name: str) -> Image.Image:
    im = Image.open(SRC / name).convert("RGBA")
    return im.crop(im.getbbox())  # trim the transparent margin first


def _square(im: Image.Image, size: int, background=None, padding=0.0) -> Image.Image:
    """Fit `im` into a `size`x`size` canvas without distorting it."""
    inner = round(size * (1 - 2 * padding))
    fitted = im.copy()
    fitted.thumbnail((inner, inner), Image.LANCZOS)

    canvas = Image.new("RGBA", (size, size), background or (0, 0, 0, 0))
    canvas.paste(
        fitted,
        ((size - fitted.width) // 2, (size - fitted.height) // 2),
        fitted,
    )
    return canvas


def main() -> int:
    dark = _load("1.png")   # charcoal — for light backgrounds
    light = _load("2.png")  # white    — for dark backgrounds

    written: list[tuple[str, int]] = []

    def write(im: Image.Image, name: str) -> None:
        path = OUT / name
        # The mark is two flat colours plus antialiasing, so a palette holds it
        # exactly while cutting the file to a fraction of full RGBA.
        im.quantize(colors=64, method=Image.FASTOCTREE).save(path, "PNG", optimize=True)
        written.append((name, path.stat().st_size))

    # The header lockup renders at 26px, so it gets its own small file rather
    # than making every page pull a 512px mark down to thumbnail size.
    write(_square(dark, 64), "logo-dark-64.png")
    write(_square(light, 64), "logo-light-64.png")

    # Full-resolution marks, for slide decks, READMEs and anything else.
    write(_square(dark, 512), "logo-dark.png")
    write(_square(light, 512), "logo-light.png")

    # Favicons, both colours. Transparent so they sit on any tab strip.
    for px in (16, 32, 48):
        write(_square(dark, px), f"favicon-{px}.png")
        write(_square(light, px), f"favicon-{px}-light.png")

    # iOS: no transparency, or the charcoal mark lands on black.
    write(_square(dark, 180, IVORY, PLATE_PADDING), "apple-touch-icon.png")

    # Android / PWA.
    write(_square(dark, 192, IVORY, PLATE_PADDING), "icon-192.png")
    write(_square(dark, 512, IVORY, PLATE_PADDING), "icon-512.png")

    # Link preview. Judges paste these URLs around; an unstyled card looks
    # broken next to the rest of the submission.
    og = Image.new("RGBA", (1200, 630), IVORY)
    mark = dark.copy()
    mark.thumbnail((300, 300), Image.LANCZOS)
    og.paste(mark, ((1200 - mark.width) // 2, (630 - mark.height) // 2 - 40), mark)
    og.convert("RGB").save(OUT / "og.png", "PNG", optimize=True)
    written.append(("og.png", (OUT / "og.png").stat().st_size))

    total = sum(size for _, size in written)
    for name, size in written:
        print(f"  {name:26} {size / 1024:7.1f} KB")
    print(f"  {len(written)} files, {total / 1024:.1f} KB total -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
