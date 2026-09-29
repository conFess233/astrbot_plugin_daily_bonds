"""Authorize privileged group commands from the host role and global QQ list."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def may_set_wife(event: Any, sender_id: str, config: Mapping[str, Any]) -> bool:
    return bool(event.is_admin()) or sender_id in config["access"]["extra_admin_ids"]
