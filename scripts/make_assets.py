"""Draws the Rich Presence images: one app icon and four state badges."""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "assets")
SS = 4

CLAY = (217, 119, 87, 255)
INK = (31, 30, 29, 255)
WHITE = (255, 255, 255, 255)
AMBER = (232, 168, 56, 255)
SLATE = (92, 98, 112, 255)
GREEN = (59, 165, 93, 255)


def font(size):
    for name in ("consolab.ttf", "DejaVuSansMono-Bold.ttf", "Menlo.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def canvas(size):
    return Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))


def finish(img, size, path):
    img.resize((size, size), Image.LANCZOS).save(path)


def large(path):
    n = 512 * SS
    img = canvas(512)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, n - 1, n - 1], radius=int(n * 0.22), fill=CLAY)
    f = font(int(n * 0.42))
    text = ">_"
    box = d.textbbox((0, 0), text, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((n - w) / 2 - box[0], (n - h) / 2 - box[1]), text, font=f, fill=WHITE)
    finish(img, 512, path)


def badge(path, bg, draw_icon):
    n = 128 * SS
    img = canvas(128)
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, n - 1, n - 1], fill=bg)
    draw_icon(d, n)
    finish(img, 128, path)


def thinking(d, n):
    r = n * 0.075
    for x in (0.30, 0.50, 0.70):
        cx, cy = n * x, n * 0.5
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)


def tools(d, n):
    pts = [(0.56, 0.18), (0.30, 0.55), (0.48, 0.55), (0.42, 0.82), (0.70, 0.43), (0.52, 0.43), (0.60, 0.18)]
    d.polygon([(x * n, y * n) for x, y in pts], fill=WHITE)


def waiting(d, n):
    w = n * 0.12
    d.rounded_rectangle([n / 2 - w / 2, n * 0.22, n / 2 + w / 2, n * 0.60], radius=w / 2, fill=WHITE)
    r = w * 0.6
    d.ellipse([n / 2 - r, n * 0.72 - r, n / 2 + r, n * 0.72 + r], fill=WHITE)


def idle(d, n):
    r = n * 0.26
    cx, cy = n * 0.5, n * 0.5
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)
    off = r * 0.55
    d.ellipse([cx - r + off, cy - r - off * 0.45, cx + r + off, cy + r - off * 0.45], fill=SLATE)


os.makedirs(OUT, exist_ok=True)
large(os.path.join(OUT, "large.png"))
badge(os.path.join(OUT, "thinking.png"), CLAY, thinking)
badge(os.path.join(OUT, "tools.png"), GREEN, tools)
badge(os.path.join(OUT, "waiting.png"), AMBER, waiting)
badge(os.path.join(OUT, "idle.png"), SLATE, idle)
print("wrote", sorted(os.listdir(OUT)))
