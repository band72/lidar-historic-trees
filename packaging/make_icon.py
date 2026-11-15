"""Generate the app icon (PNG + ICO) with Pillow: a stem cross-section with a DBH ring."""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "assets"


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256
    d.rounded_rectangle([8 * s, 8 * s, 248 * s, 248 * s], radius=48 * s, fill=(24, 74, 46, 255))
    # tree rings
    for r, col in ((92, (196, 150, 98)), (74, (176, 128, 78)), (54, (196, 150, 98)), (34, (176, 128, 78)), (14, (120, 84, 48))):
        d.ellipse([(128 - r) * s, (128 - r) * s, (128 + r) * s, (128 + r) * s], fill=col + (255,))
    # DBH measurement ring and diameter line
    d.ellipse([22 * s, 22 * s, 234 * s, 234 * s], outline=(250, 178, 25, 255), width=max(1, int(10 * s)))
    d.line([36 * s, 128 * s, 220 * s, 128 * s], fill=(255, 255, 255, 255), width=max(1, int(8 * s)))
    return img


def main() -> None:
    OUT.mkdir(exist_ok=True)
    big = draw(256)
    big.save(OUT / "icon.png")
    big.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {OUT / 'icon.png'} and {OUT / 'icon.ico'}")


if __name__ == "__main__":
    main()
