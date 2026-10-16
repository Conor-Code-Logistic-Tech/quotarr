"""Burn a caption onto a frame, Frinkiac style (white text, black outline, bottom centred)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FALLBACK_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


@lru_cache(maxsize=32)
def _font(size: int, preferred: str) -> ImageFont.FreeTypeFont:
    for path in [preferred, *_FALLBACK_FONTS]:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: float) -> list[str]:
    lines = []
    for paragraph in text.splitlines():
        words = paragraph.split()
        if not words:
            continue
        line = words[0]
        for word in words[1:]:
            trial = f"{line} {word}"
            if draw.textlength(trial, font=font) <= max_width:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def render_meme(frame: Path, text: str, out: Path, font_path: str = "") -> None:
    img = Image.open(frame).convert("RGB")
    width, height = img.size
    draw = ImageDraw.Draw(img)
    size = max(12, round(width / 22))
    while True:
        font = _font(size, font_path)
        lines = _wrap(draw, text, font, width * 0.92)
        line_height = round(size * 1.2)
        if line_height * len(lines) <= height * 0.45 or size <= 12:
            break
        size = max(12, int(size * 0.85))
    stroke = max(1, round(size / 9))
    y = height - round(height * 0.04) - line_height * len(lines)
    for line in lines:
        draw.text((width / 2, y), line, font=font, fill="white", stroke_width=stroke,
                  stroke_fill="black", anchor="ma")
        y += line_height
    img.save(out, "JPEG", quality=90)
