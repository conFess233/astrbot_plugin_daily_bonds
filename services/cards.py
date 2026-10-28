"""通过 AstrBot t2i 服务渲染卡片，并按配置选择卡片或纯文本。

t2i 服务读不到插件的本地文件，角色图与头像先缩成小图再以 data URI 写入模板数据。
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .message_templates import MESSAGE_CONTEXT

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
RenderHtml = Callable[..., Awaitable[str]]
# 截图使用 JPEG：PNG 不接受宿主默认附带的 quality 参数。
_RENDER_OPTIONS = {"full_page": True, "type": "jpeg", "quality": 90}
_RANK_COLUMNS = 3
_RANK_ROWS_PER_COLUMN = 10


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
    pair_scores: tuple[int, int] | None = None
    pair_score_labels: tuple[str, str] | None = None
    source_label_parts: tuple[str, str, str] | None = None
    source_avatar_path: Path | None = None
    source_user_id: str = ""
    score: int | None = None
    badge: str = ""
    highlight: bool = False
    tone: str = ""


def render_mode(kind: str, render: Mapping[str, Any] | None = None) -> str:
    """返回某类回复的生效形式：`card` 或 `text`。"""

    render = render or MESSAGE_CONTEXT.get().get("render") or {}
    chosen = render.get("types", {}).get(kind, "inherit")
    if chosen in {"card", "text"}:
        return chosen
    return "text" if render.get("mode") == "text" else "card"


@lru_cache(maxsize=512)
def _data_uri_cached(path: str, mtime_ns: int, max_side: int) -> str:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        image.thumbnail((max_side, max_side))
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        stream = io.BytesIO()
        image.save(stream, format="WEBP", quality=82, method=4)
    return "data:image/webp;base64," + base64.b64encode(stream.getvalue()).decode()


def data_uri(path: Path | None, max_side: int) -> str:
    """把本地图片缩小后编码为 data URI；读取失败返回空串，由模板显示占位。"""

    if path is None:
        return ""
    try:
        stat = path.stat()
        return _data_uri_cached(str(path), stat.st_mtime_ns, max_side)
    except (OSError, ValueError, Image.DecompressionBombError):
        return ""


def _initial(name: str) -> str:
    stripped = (name or "").strip()
    return stripped[:1] or "?"


@lru_cache(maxsize=16)
def _template(name: str) -> str:
    theme = (TEMPLATE_DIR / "theme.css").read_text(encoding="utf-8")
    source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    return source.replace("/*THEME*/", theme)


class CardService:
    """把卡片数据交给宿主 html_render；未提供渲染器时总是返回空结果。"""

    def __init__(self, render_html: RenderHtml | None) -> None:
        self.render_html = render_html

    @property
    def available(self) -> bool:
        return self.render_html is not None

    @staticmethod
    def page_capacity(*, max_rows: int, ranking: bool = False, **_: Any) -> int:
        if max_rows < 1:
            raise ValueError("每张卡片的行数必须为正数。")
        if ranking:
            return min(max_rows, _RANK_COLUMNS * _RANK_ROWS_PER_COLUMN)
        return max_rows

    async def render_template(self, name: str, data: Mapping[str, Any]) -> Path:
        """渲染单张卡片并返回宿主下载的图片路径；失败抛出异常由调用方降级。"""

        if self.render_html is None:
            raise RuntimeError("卡片渲染不可用")
        path = await self.render_html(
            _template(name), dict(data), return_url=False, options=_RENDER_OPTIONS
        )
        result = Path(str(path))
        if not result.is_file():
            raise OSError("t2i 服务未返回图片文件")
        return result

    async def render(
        self,
        title: str,
        rows: Sequence[CardRow],
        *,
        header_avatar_path: Path | None = None,
        header_name: str = "",
        max_rows: int = 20,
        empty_text: str = "暂无记录。",
        ranking: bool = False,
        subtitle: str = "",
        range_label: str = "",
        **_: Any,
    ) -> tuple[Path, ...]:
        """按行数分成多张卡片；任一张失败时清理已生成的图片并抛出异常。"""

        if self.render_html is None:
            return ()
        per_card = self.page_capacity(max_rows=max_rows, ranking=ranking)
        chunks = [rows[i : i + per_card] for i in range(0, len(rows), per_card)] or [()]
        header = await asyncio.to_thread(data_uri, header_avatar_path, 160)
        paths: list[Path] = []
        try:
            for index, chunk in enumerate(chunks, start=1):
                page_label = f"分图 {index}/{len(chunks)}" if len(chunks) > 1 else ""
                if rows and rows[0].pair_scores is not None:
                    name, data = "affection.html", await self._affection(title, chunk)
                elif ranking:
                    name = "ranking.html"
                    data = {
                        "title": title,
                        "subtitle": " · ".join(filter(None, (subtitle, page_label))),
                        "range_label": range_label,
                        "header_avatar": header,
                        "empty_text": empty_text,
                        "rows": await asyncio.to_thread(self._rank_rows, chunk),
                        "rows_per_column": max(
                            1, min(_RANK_ROWS_PER_COLUMN, math.ceil(len(chunk) / 3))
                        ),
                    }
                else:
                    name = "list.html"
                    data = {
                        "title": title,
                        "subtitle": subtitle,
                        "page_label": page_label,
                        "header": {"avatar": header, "name": header_name},
                        "empty_text": empty_text,
                        "rows": await asyncio.to_thread(self._list_rows, chunk),
                    }
                paths.append(await self.render_template(name, data))
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        return tuple(paths)

    @staticmethod
    def _list_rows(rows: Sequence[CardRow]) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            owner, _, subject = row.primary.partition(" → ")
            source = None
            if row.source_label_parts is not None:
                before, name, after = row.source_label_parts
                source = {
                    "before": before,
                    "name": name,
                    "after": after,
                    "avatar": data_uri(row.source_avatar_path, 48),
                    "tone": "pink" if "抢" in before + after else "",
                }
            result.append(
                {
                    "primary": row.primary,
                    "secondary": row.secondary,
                    "tone": row.tone,
                    "source": source,
                    "image": data_uri(row.image_path, 240),
                    "avatar": data_uri(row.avatar_path, 96),
                    "initial": _initial(owner),
                    "other_avatar": data_uri(row.other_avatar_path, 96),
                    "other_initial": _initial(subject)
                    if row.other_avatar_user_id
                    else "",
                }
            )
        return result

    @staticmethod
    def _rank_rows(rows: Sequence[CardRow]) -> list[dict[str, Any]]:
        return [
            {
                "rank": row.pair_rank,
                "name": row.primary,
                "detail": row.secondary,
                "badge": row.badge,
                "score": row.score,
                "highlight": row.highlight,
                "pair": list(row.pair_names) if row.pair_names else None,
                "avatar": data_uri(row.avatar_path, 96),
                "initial": _initial(row.primary),
            }
            for row in rows
        ]

    @staticmethod
    async def _affection(title: str, rows: Sequence[CardRow]) -> dict[str, Any]:
        row = rows[0]
        left_name, right_name = row.pair_names or ("", "")
        left, right = await asyncio.gather(
            asyncio.to_thread(data_uri, row.avatar_path, 192),
            asyncio.to_thread(data_uri, row.other_avatar_path, 192),
        )
        labels = row.pair_score_labels or ("", "")
        scores = row.pair_scores or (0, 0)
        return {
            "title": title,
            "left": {"name": left_name, "avatar": left, "initial": _initial(left_name)},
            "right": {
                "name": right_name,
                "avatar": right,
                "initial": _initial(right_name),
            },
            "scores": [
                {"score": scores[0], "label": labels[0]},
                {"score": scores[1], "label": labels[1]},
            ],
        }
