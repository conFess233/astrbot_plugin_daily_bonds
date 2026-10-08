"""枚举服务端字体，使用稳定标识选择字体并生成中文预览。"""

from __future__ import annotations

import base64
import hashlib
import io
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


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
    paths = set()
    configured = os.environ.get("DAILY_BONDS_CJK_FONT")
    if configured:
        paths.add(Path(configured))
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
        font_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
        result.append(
            {
                "id": font_id,
                "name": f"{family} · {style}",
                "path": str(path),
            }
        )
    # 自动选择优先使用常见中文字体；用户仍可手动选取任意读取到的字体。
    cjk_names = (
        "msyh",
        "simhei",
        "simsun",
        "notosanssc",
        "notosanscjk",
        "noto sans sc",
        "noto sans cjk",
        "wqy",
        "wenquanyi",
        "pingfang",
        "ukai",
        "uming",
        "microsoft yahei",
        "微软雅黑",
        "黑体",
        "宋体",
    )
    return sorted(
        result,
        key=lambda item: (
            not (
                configured
                and Path(item["path"]).resolve() == Path(configured).resolve()
            ),
            not any(
                name in (item["name"] + item["path"]).casefold() for name in cjk_names
            ),
            item["name"].casefold(),
        ),
    )


def resolve_font(font_id: str = "auto") -> str | None:
    fonts = available_fonts()
    # 显式选择优先；旧 bundled 标识与已失效的选择均使用本地自动选择。
    candidates = sorted(fonts, key=lambda item: item["id"] != font_id)
    for item in candidates:
        if Path(item["path"]).is_file():
            try:
                ImageFont.truetype(item["path"], 24)
                return item["path"]
            except (OSError, ValueError):
                continue
    return None


def font_preview(font_id: str) -> dict[str, str | bool]:
    path = resolve_font(font_id)
    if path is None:
        raise ValueError(
            "未读取到可用的本地字体，请在 AstrBot 所在机器安装字体后重载插件。"
        )
    canvas = Image.new("RGB", (720, 150), "#f7f7f9")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(path, 28)
    draw.text((24, 25), "今日姻缘 · 老婆、老公与群友", font=font, fill="#2f2b3d")
    draw.text((24, 80), "亲密度 123 · 活跃天数 7 · ABC abc", font=font, fill="#7367f0")
    stream = io.BytesIO()
    canvas.save(stream, format="PNG")
    return {
        "src": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
        "fallback": font_id != "auto"
        and not any(
            item["id"] == font_id and item["path"] == path for item in available_fonts()
        ),
    }
