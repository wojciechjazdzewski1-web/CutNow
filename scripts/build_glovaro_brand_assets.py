"""Generuje logo Glovaro (favicon, ikona nawigacji, profil) z pliku źródłowego."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageEnhance

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "static" / "img" / "glovaro-source.png"
OUT = ROOT / "static" / "img"
LIGHT_BG = (255, 241, 245, 255)  # brand-50 — podgląd OG / apple-touch


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


def _luminance(r: int, g: int, b: int) -> float:
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _is_brand_pixel(r: int, g: int, b: int, a: int) -> bool:
    if a < 12:
        return False
    lum = _luminance(r, g, b)
    chroma = max(r, g, b) - min(r, g, b)
    # róż / magenta znaczka
    if lum >= 55 and chroma >= 35 and r >= 90 and r > g + 18:
        return True
    # biały napis w pełnym logo
    if lum >= 200 and chroma < 35:
        return True
    return False


def _strip_dark_background(img: Image.Image) -> Image.Image:
    out = img.convert("RGBA")
    pixels = out.load()
    w, h = out.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if not _is_brand_pixel(r, g, b, a):
                pixels[x, y] = (0, 0, 0, 0)
    return out


def _boost_brand_color(img: Image.Image) -> Image.Image:
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 12:
                continue
            lum = _luminance(r, g, b)
            if lum >= 200:
                px[x, y] = (255, 255, 255, a)
                continue
            # wyraźniejszy róż: #ff2d8a → jaśniejszy i bardziej nasycony
            nr = min(255, int(r * 1.22 + 28))
            ng = max(0, int(g * 0.82))
            nb = min(255, int(b * 1.08 + 22))
            na = min(255, int(a * 1.05 + 8))
            px[x, y] = (nr, ng, nb, na)
    boosted = ImageEnhance.Color(img).enhance(1.4)
    boosted = ImageEnhance.Contrast(boosted).enhance(1.12)
    return boosted


def _prepare_icon(img: Image.Image) -> Image.Image:
    return _boost_brand_color(_strip_dark_background(img))


def _fit_transparent(icon: Image.Image, size: int, margin_ratio: float = 0.08) -> Image.Image:
    inner = max(1, int(size * (1 - 2 * margin_ratio)))
    fitted = icon.resize((inner, inner), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ox = (size - inner) // 2
    canvas.paste(fitted, (ox, ox), fitted)
    return canvas


def _fit_on_light(icon: Image.Image, size: int, margin_ratio: float = 0.1) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), LIGHT_BG)
    inner = max(1, int(size * (1 - 2 * margin_ratio)))
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
    icon_sq = _prepare_icon(_square_crop(img, icon_box, pad=28))

    full_box = _content_bbox(img)
    full_sq = _prepare_icon(_square_crop(img, full_box, pad=36))

    transparent_sizes = {
        16: "favicon-16x16.png",
        32: "favicon-32x32.png",
        48: "favicon-48x48.png",
        192: "glovaro-icon-192.png",
    }
    for px, name in transparent_sizes.items():
        _fit_transparent(icon_sq, px).save(OUT / name, optimize=True)

    _fit_transparent(icon_sq, 40, margin_ratio=0.06).save(OUT / "glovaro-icon-nav.png", optimize=True)
    _fit_transparent(icon_sq, 180, margin_ratio=0.08).save(OUT / "glovaro-apple-touch.png", optimize=True)
    _fit_transparent(icon_sq, 512, margin_ratio=0.08).save(OUT / "glovaro-profile.png", optimize=True)

    og = Image.new("RGBA", (1200, 630), LIGHT_BG)
    logo = _fit_transparent(full_sq, 420, margin_ratio=0.06)
    og.paste(logo, ((1200 - 420) // 2, (630 - 420) // 2), logo)
    og.save(OUT / "glovaro-og.png", optimize=True)

    ico_images = [_fit_transparent(icon_sq, s, margin_ratio=0.06) for s in (16, 32, 48, 64)]
    ico_images[0].save(
        OUT / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
        append_images=ico_images[1:],
    )
    print("Zapisano assety w", OUT)


if __name__ == "__main__":
    main()
