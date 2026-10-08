"""枚举服务端字体，使用稳定标识选择字体并生成中文预览。"""

from __future__ import annotations

import base64
import hashlib
import io
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BUNDLED_FONT = (
    Path(__file__).resolve().parents[1] / "resources/fonts/NotoSansSC-wght.ttf"
)


@lru_cache(maxsize=1)
def available_fonts() -> list[dict[str, str]]:
    """读取常见系统及用户字体目录，不接受客户端传入的文件路径。"""
    roots = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        / "Microsoft/Windows/Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".fonts",
        Path.home() / ".local/share/fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        Path.home() / "Library/Fonts",
    ]
    paths = {BUNDLED_FONT}
    for root in roots:
        if root.is_dir():
            paths.update(
                p
                for p in root.rglob("*")
                if p.suffix.lower() in {".ttf", ".otf", ".ttc"}
            )
    result = []
    for path in sorted(paths):
        try:
            font = ImageFont.truetype(str(path), 24)
            family, style = font.getname()
        except (OSError, ValueError):
            continue
        font_id = (
            "bundled"
            if path == BUNDLED_FONT
            else hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
        )
        result.append(
            {
                "id": font_id,
                "name": "内置 Noto Sans SC"
                if font_id == "bundled"
                else f"{family} · {style}",
                "path": str(path),
            }
        )
    return sorted(
        result, key=lambda item: (item["id"] != "bundled", item["name"].casefold())
    )


def resolve_font(font_id: str = "bundled") -> str | None:
    for item in available_fonts():
        if item["id"] == font_id and Path(item["path"]).is_file():
            try:
                ImageFont.truetype(item["path"], 24)
                return item["path"]
            except (OSError, ValueError):
                break
    return str(BUNDLED_FONT) if BUNDLED_FONT.is_file() else None


def font_preview(font_id: str) -> dict[str, str | bool]:
    path = resolve_font(font_id)
    if path is None:
        raise ValueError("没有可用的预览字体。")
    canvas = Image.new("RGB", (720, 150), "#f7f7f9")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(path, 28)
    draw.text((24, 25), "今日姻缘 · 老婆、老公与群友", font=font, fill="#2f2b3d")
    draw.text((24, 80), "亲密度 123 · 活跃天数 7 · ABC abc", font=font, fill="#7367f0")
    stream = io.BytesIO()
    canvas.save(stream, format="PNG")
    return {
        "src": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
        "fallback": not any(
            item["id"] == font_id and item["path"] == path for item in available_fonts()
        ),
    }
