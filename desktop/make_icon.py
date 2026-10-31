"""生成研序 ScholarFlow 应用图标 icon.ico（开发工具，不随产品分发）。"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BLUE = (10, 89, 247, 255)
WHITE = (255, 255, 255, 255)
OUT = Path(__file__).resolve().parent / "assets" / "icon.ico"


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in (
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make(size: int) -> Image.Image:
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=BLUE)
    font = _font(int(s * 0.62))
    text = "研"
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (s - w) / 2 - bbox[0]
    y = (s - h) / 2 - bbox[1] - s * 0.02
    d.text((x, y), text, font=font, fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


sizes = [16, 24, 32, 48, 64, 128, 256]
images = [make(s) for s in sizes]
images[0].save(OUT, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[1:])
print("saved", OUT)
