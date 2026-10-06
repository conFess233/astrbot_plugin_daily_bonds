"""Pillow-based list cards with a bundled CJK font."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


@dataclass(frozen=True, slots=True)
class CardRow:
    primary: str
    secondary: str = ""
    image_path: Path | None = None
    avatar_path: Path | None = None
    other_avatar_path: Path | None = None
    avatar_user_id: str = ""
    other_avatar_user_id: str = ""
    pair_names: tuple[str, str] | None = None
    pair_rank: int = 0
    show_user_ids: bool = False
    pair_scores: tuple[int, int] | None = None
    pair_score_labels: tuple[str, str] | None = None


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

    def render(
        self,
        title: str,
        rows: Sequence[CardRow],
        *,
        cache_key: str,
        header_avatar_path: Path | None = None,
        header_user_id: str = "",
        header_name: str = "",
        max_height: int = 2000,
        max_rows: int = ROWS_PER_CARD,
        empty_text: str = "暂无记录。",
    ) -> tuple[Path, ...]:
        """Return ordered card files, or an empty tuple if a CJK font is unavailable."""

        font_path = _find_cjk_font()
        if font_path is None:
            return ()
        has_header_avatar = bool(header_avatar_path or header_user_id or header_name)
        row_top = 150 if has_header_avatar else 118
        if max_rows < 1 or max_height < row_top + self.ROW_HEIGHT + 40:
            raise ValueError("卡片高度必须能够容纳标题和至少一行内容，行数必须为正数。")
        rows_per_card = min(max_rows, (max_height - row_top - 40) // self.ROW_HEIGHT)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        chunks = [
            rows[i : i + rows_per_card] for i in range(0, len(rows), rows_per_card)
        ] or [()]
        digest = hashlib.sha256(f"{cache_key}:{uuid.uuid4()}".encode()).hexdigest()[:20]
        paths: list[Path] = []
        output: Path | None = None
        try:
            for page_index, chunk in enumerate(chunks, start=1):
                page_label = f"{page_index}/{len(chunks)}" if len(chunks) > 1 else ""
                card_title = f"{title}  {page_label}".strip()
                output = self.output_dir / f"{digest}-{page_index}.png"
                self._render_one(
                    output,
                    card_title,
                    chunk,
                    font_path,
                    header_avatar_path,
                    header_user_id,
                    header_name,
                    empty_text,
                )
                paths.append(output)
        except Exception:
            for path in (*paths, output):
                if path is not None:
                    path.unlink(missing_ok=True)
            raise
        return tuple(paths)

    def _render_one(
        self,
        output: Path,
        title: str,
        rows: Sequence[CardRow],
        font_path: str,
        header_avatar_path: Path | None = None,
        header_user_id: str = "",
        header_name: str = "",
        empty_text: str = "暂无记录。",
    ) -> None:
        title_font = ImageFont.truetype(font_path, 38)
        primary_font = ImageFont.truetype(font_path, 25)
        secondary_font = ImageFont.truetype(font_path, 18)
        has_header_avatar = bool(header_avatar_path or header_user_id or header_name)
        row_top = 150 if has_header_avatar else 118
        banner_bottom = row_top - 18
        height = row_top + max(1, len(rows)) * self.ROW_HEIGHT + 40
        image = Image.new("RGB", (self.WIDTH, height), self.BACKGROUND)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (20, 20, self.WIDTH - 20, height - 20), radius=28, fill="white"
        )
        draw.rounded_rectangle(
            (20, 20, self.WIDTH - 20, banner_bottom), radius=28, fill=self.ACCENT
        )
        draw.rectangle((20, 70, self.WIDTH - 20, banner_bottom), fill=self.ACCENT)
        title_left = 124 if has_header_avatar else self.PADDING
        draw.text(
            (title_left, 31 if has_header_avatar else 39),
            _fit_text(draw, title, title_font, self.WIDTH - title_left - self.PADDING),
            font=title_font,
            fill="white",
        )
        if has_header_avatar:
            _draw_avatar(
                image,
                header_avatar_path,
                (40, 42, 104, 106),
                header_user_id or "?",
                secondary_font,
            )
            draw.text(
                (title_left, 85),
                _fit_text(
                    draw,
                    header_name,
                    secondary_font,
                    self.WIDTH - title_left - self.PADDING,
                ),
                font=secondary_font,
                fill="white",
            )
        if not rows:
            draw.text(
                (self.PADDING, row_top + 20),
                _fit_text(
                    draw, empty_text, primary_font, self.WIDTH - 2 * self.PADDING
                ),
                font=primary_font,
                fill=self.MUTED,
            )
        for index, row in enumerate(rows):
            top = row_top + index * self.ROW_HEIGHT
            if index % 2 == 0:
                draw.rounded_rectangle(
                    (32, top, self.WIDTH - 32, top + 62),
                    radius=12,
                    fill=(250, 249, 254),
                )
            if row.pair_names is not None:
                if row.pair_rank:
                    draw.text(
                        (38, top + 18),
                        f"{row.pair_rank}.",
                        font=primary_font,
                        fill=self.INK,
                    )
                _draw_avatar(
                    image,
                    row.avatar_path,
                    (83, top + 9, 129, top + 55),
                    row.avatar_user_id,
                    secondary_font,
                )
                draw.text(
                    (140, top + 8),
                    _fit_text(draw, row.pair_names[0], primary_font, 155),
                    font=primary_font,
                    fill=self.INK,
                )
                if row.show_user_ids:
                    draw.text(
                        (140, top + 39),
                        row.avatar_user_id,
                        font=secondary_font,
                        fill=self.MUTED,
                    )
                draw.text((300, top + 18), "→", font=primary_font, fill=self.MUTED)
                _draw_avatar(
                    image,
                    row.other_avatar_path,
                    (338, top + 9, 384, top + 55),
                    row.other_avatar_user_id,
                    secondary_font,
                )
                draw.text(
                    (395, top + 8),
                    _fit_text(draw, row.pair_names[1], primary_font, 155),
                    font=primary_font,
                    fill=self.INK,
                )
                if row.show_user_ids:
                    draw.text(
                        (395, top + 39),
                        row.other_avatar_user_id,
                        font=secondary_font,
                        fill=self.MUTED,
                    )
                if row.pair_scores is not None:
                    forward, reverse = row.pair_scores
                    labels = row.pair_score_labels or (
                        f"→ 好感度 {forward}",
                        f"← 好感度 {reverse}",
                    )
                    draw.text(
                        (610, top + 8),
                        _fit_text(draw, labels[0], secondary_font, 310),
                        font=secondary_font,
                        fill=self.ACCENT,
                    )
                    draw.text(
                        (610, top + 39),
                        _fit_text(draw, labels[1], secondary_font, 310),
                        font=secondary_font,
                        fill=self.ACCENT,
                    )
                else:
                    draw.text(
                        (610, top + 19),
                        _fit_text(draw, row.secondary, primary_font, 310),
                        font=primary_font,
                        fill=self.ACCENT,
                    )
                continue
            text_left = self.PADDING
            if row.avatar_path is not None or row.avatar_user_id:
                _draw_avatar(
                    image,
                    row.avatar_path,
                    (38, top + 10, 84, top + 56),
                    row.avatar_user_id,
                    secondary_font,
                )
                text_left = 96
            if row.other_avatar_path is not None or row.other_avatar_user_id:
                draw.text((85, top + 20), "→", font=secondary_font, fill=self.MUTED)
                _draw_avatar(
                    image,
                    row.other_avatar_path,
                    (108, top + 10, 154, top + 56),
                    row.other_avatar_user_id,
                    secondary_font,
                )
                text_left = 166
            if row.image_path is not None:
                _draw_thumbnail(image, row.image_path, (842, top + 4, 922, top + 58))
            available_width = (
                830 if row.image_path is not None else self.WIDTH - self.PADDING
            ) - text_left
            primary = _fit_text(draw, row.primary, primary_font, available_width)
            draw.text((text_left, top + 9), primary, font=primary_font, fill=self.INK)
            if row.secondary:
                secondary = _fit_text(
                    draw, row.secondary, secondary_font, available_width
                )
                draw.text(
                    (text_left, top + 41),
                    secondary,
                    font=secondary_font,
                    fill=self.MUTED,
                )
        image.save(output, format="PNG", optimize=True)


def _find_cjk_font() -> str | None:
    candidates = (
        os.environ.get("DAILY_BONDS_CJK_FONT"),
        str(Path(__file__).resolve().parent.parent / "resources" / "fonts" / "NotoSansSC-wght.ttf"),
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/System/Library/Fonts/PingFang.ttc",
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


def _draw_avatar(
    canvas: Image.Image,
    source_path: Path | None,
    box: tuple[int, int, int, int],
    user_id: str = "",
    font: ImageFont.FreeTypeFont | None = None,
) -> None:
    left, top, right, bottom = box
    try:
        if source_path is None:
            raise OSError("QQ avatar unavailable")
        with Image.open(source_path) as opened:
            avatar = opened.convert("RGBA")
        avatar = ImageOps.fit(
            avatar, (right - left, bottom - top), method=Image.Resampling.LANCZOS
        )
        mask = Image.new("L", avatar.size, 0)
        ImageDraw.Draw(mask).ellipse(
            (0, 0, avatar.width - 1, avatar.height - 1), fill=255
        )
        canvas.paste(avatar, (left, top), mask)
    except (OSError, ValueError, Image.DecompressionBombError):
        if not user_id or font is None:
            return
        draw = ImageDraw.Draw(canvas)
        draw.ellipse(box, fill=(229, 225, 250))
        label = "?"
        bounds = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (
                left + (right - left - (bounds[2] - bounds[0])) / 2,
                top + (bottom - top - (bounds[3] - bounds[1])) / 2 - bounds[1],
            ),
            label,
            font=font,
            fill=(89, 76, 176),
        )


def _fit_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> str:
    text = " ".join(text.split())  # 固定行高卡片内的换行与超长文案统一压成一行再截断。
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
