"""Generuje logo Glovaro (favicon, ikona nawigacji, profil) z pliku źródłowego."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "static" / "img" / "glovaro-source.png"
OUT = ROOT / "static" / "img"
BG = (2, 6, 23, 255)  # #020617


def _content_bbox(img: Image.Image, y_min: int = 0, y_max: int | None = None) -> tuple[int, int, int, int]:
    y_max = y_max if y_max is not None else img.height
    pixels = img.load()
    w, h = img.size
    minx, miny, maxx, maxy = w, h, 0, 0
    for y in range(y_min, min(y_max, h)):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if a > 20 and (r + g + b) > 80:
                minx, miny = min(minx, x), min(miny, y)
                maxx, maxy = max(maxx, x), max(maxy, y)
    return minx, miny, maxx, maxy


def _square_crop(img: Image.Image, box: tuple[int, int, int, int], pad: int = 0) -> Image.Image:
    left, top, right, bottom = box
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(img.width, right + pad)
    bottom = min(img.height, bottom + pad)
    cropped = img.crop((left, top, right, bottom))
    cw, ch = cropped.size
    side = max(cw, ch)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(cropped, ((side - cw) // 2, (side - ch) // 2))
    return square


def _on_bg(icon: Image.Image, size: int, margin_ratio: float = 0.12) -> Image.Image:
    icon = icon.resize((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), BG)
    inner = int(size * (1 - 2 * margin_ratio))
    fitted = icon.resize((inner, inner), Image.Resampling.LANCZOS)
    ox = (size - inner) // 2
    canvas.paste(fitted, (ox, ox), fitted)
    return canvas


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"Brak pliku źródłowego: {SRC}")
    img = Image.open(SRC).convert("RGBA")
    OUT.mkdir(parents=True, exist_ok=True)

    icon_box = _content_bbox(img, 125, 405)
    icon_sq = _square_crop(img, icon_box, pad=28)

    full_box = _content_bbox(img)
    full_sq = _square_crop(img, full_box, pad=36)

    transparent_sizes = {16: "favicon-16x16.png", 32: "favicon-32x32.png", 192: "glovaro-icon-192.png"}
    for px, name in transparent_sizes.items():
        icon_sq.resize((px, px), Image.Resampling.LANCZOS).save(OUT / name, optimize=True)

    icon_sq.resize((36, 36), Image.Resampling.LANCZOS).save(OUT / "glovaro-icon-nav.png", optimize=True)
    _on_bg(icon_sq, 180).save(OUT / "glovaro-apple-touch.png", optimize=True)
    _on_bg(full_sq, 512).save(OUT / "glovaro-profile.png", optimize=True)

    og = Image.new("RGBA", (1200, 630), BG)
    logo = full_sq.resize((420, 420), Image.Resampling.LANCZOS)
    og.paste(logo, ((1200 - 420) // 2, (630 - 420) // 2), logo)
    og.save(OUT / "glovaro-og.png", optimize=True)

    ico_images = [icon_sq.resize((s, s), Image.Resampling.LANCZOS) for s in (16, 32, 48)]
    ico_images[0].save(
        OUT / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
        append_images=ico_images[1:],
    )
    print("Zapisano assety w", OUT)


if __name__ == "__main__":
    main()
