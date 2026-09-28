"""Pillow-based list cards with a plain-text fallback when no CJK font exists."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps


@dataclass(frozen=True, slots=True)
class CardRow:
    primary: str
    secondary: str = ""
    image_path: Path | None = None
    avatar_path: Path | None = None
    other_avatar_path: Path | None = None


class CardRenderer:
    """Render bounded PNG cards without requiring browser automation."""

    WIDTH = 960
    ROWS_PER_CARD = 20
    ROW_HEIGHT = 72
    PADDING = 36
    BACKGROUND = (247, 246, 253)
    INK = (42, 39, 58)
    MUTED = (111, 106, 132)
    ACCENT = (115, 103, 240)

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    @property
    def has_cjk_font(self) -> bool:
        return _find_cjk_font() is not None

    def render(self, title: str, rows: Sequence[CardRow], *, cache_key: str) -> tuple[Path, ...]:
        """Return ordered card files, or an empty tuple if a CJK font is unavailable."""

        font_path = _find_cjk_font()
        if font_path is None:
            return ()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        chunks = [rows[i : i + self.ROWS_PER_CARD] for i in range(0, len(rows), self.ROWS_PER_CARD)] or [()]
        digest = hashlib.sha256(f"{cache_key}:{uuid.uuid4()}".encode("utf-8")).hexdigest()[:20]
        paths: list[Path] = []
        for page_index, chunk in enumerate(chunks, start=1):
            page_label = f"{page_index}/{len(chunks)}" if len(chunks) > 1 else ""
            card_title = f"{title}  {page_label}".strip()
            output = self.output_dir / f"{digest}-{page_index}.png"
            self._render_one(output, card_title, chunk, font_path)
            paths.append(output)
        return tuple(paths)

    def _render_one(self, output: Path, title: str, rows: Sequence[CardRow], font_path: str) -> None:
        title_font = ImageFont.truetype(font_path, 38)
        primary_font = ImageFont.truetype(font_path, 25)
        secondary_font = ImageFont.truetype(font_path, 18)
        height = 130 + max(1, len(rows)) * self.ROW_HEIGHT + 28
        image = Image.new("RGB", (self.WIDTH, height), self.BACKGROUND)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((20, 20, self.WIDTH - 20, height - 20), radius=28, fill="white")
        draw.rounded_rectangle((20, 20, self.WIDTH - 20, 100), radius=28, fill=self.ACCENT)
        draw.rectangle((20, 70, self.WIDTH - 20, 100), fill=self.ACCENT)
        draw.text((self.PADDING, 39), _fit_text(draw, title, title_font, self.WIDTH - 2 * self.PADDING), font=title_font, fill="white")
        if not rows:
            draw.text((self.PADDING, 148), "暂无记录。", font=primary_font, fill=self.MUTED)
        for index, row in enumerate(rows):
            top = 118 + index * self.ROW_HEIGHT
            if index % 2 == 0:
                draw.rounded_rectangle((32, top, self.WIDTH - 32, top + 62), radius=12, fill=(250, 249, 254))
            text_left = self.PADDING
            if row.avatar_path is not None:
                _draw_avatar(image, row.avatar_path, (38, top + 10, 84, top + 56))
                text_left = 96
            if row.other_avatar_path is not None:
                _draw_avatar(image, row.other_avatar_path, (62, top + 10, 108, top + 56))
                text_left = 120
            if row.image_path is not None:
                _draw_thumbnail(image, row.image_path, (842, top + 4, 922, top + 58))
            available_width = (830 if row.image_path is not None else self.WIDTH - self.PADDING) - text_left
            primary = _fit_text(draw, row.primary, primary_font, available_width)
            draw.text((text_left, top + 9), primary, font=primary_font, fill=self.INK)
            if row.secondary:
                secondary = _fit_text(draw, row.secondary, secondary_font, available_width)
                draw.text((text_left, top + 41), secondary, font=secondary_font, fill=self.MUTED)
        image.save(output, format="PNG", optimize=True)


def _find_cjk_font() -> str | None:
    candidates = (
        os.environ.get("DAILY_BONDS_CJK_FONT"),
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
    )
    return next((font for font in candidates if font and Path(font).is_file()), None)


def _draw_thumbnail(canvas: Image.Image, source_path: Path, box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = box
    try:
        with Image.open(source_path) as opened:
            thumbnail = opened.convert("RGBA")
        thumbnail.thumbnail((right - left, bottom - top), Image.Resampling.LANCZOS)
        x = left + (right - left - thumbnail.width) // 2
        y = top + (bottom - top - thumbnail.height) // 2
        canvas.paste(thumbnail, (x, y), thumbnail)
    except (OSError, ValueError, Image.DecompressionBombError):
        return


def _draw_avatar(canvas: Image.Image, source_path: Path, box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = box
    try:
        with Image.open(source_path) as opened:
            avatar = opened.convert("RGBA")
        avatar = ImageOps.fit(avatar, (right - left, bottom - top), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", avatar.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, avatar.width - 1, avatar.height - 1), fill=255)
        canvas.paste(avatar, (left, top), mask)
    except (OSError, ValueError, Image.DecompressionBombError):
        return


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    suffix = "…"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle] + suffix
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + suffix
