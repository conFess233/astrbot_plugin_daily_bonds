"""Prepare large images for OneBot delivery without changing stored media."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PIL import Image, ImageOps


MAX_SEND_EDGE = 1600


def prepare_outgoing_image(source: Path, *, max_edge: int = MAX_SEND_EDGE) -> tuple[Path, bool]:
    """Return a sendable path and whether the caller must remove it afterwards."""

    if max_edge < 1:
        raise ValueError("max_edge must be positive")
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        if max(image.size) <= max_edge:
            return source, False
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

        descriptor, name = tempfile.mkstemp(prefix="daily-bonds-send-", suffix=".png")
        os.close(descriptor)
        output = Path(name)
        try:
            image.save(output, format="PNG", optimize=True)
        except Exception:
            output.unlink(missing_ok=True)
            raise
        return output, True
