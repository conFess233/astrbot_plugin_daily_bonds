"""Keep AstrBot's native global settings and the runtime store in sync."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import MutableMapping
from typing import Any
import time

from .settings import default_config, merge_sparse, validate_config
from .storage import SQLiteStorage


class HostConfigBridge:
    def __init__(self, config: MutableMapping[str, Any], storage: SQLiteStorage) -> None:
        self.config = config
        self.storage = storage

    def reconcile(self) -> dict[str, Any]:
        """Apply a native edit, or import an existing Web UI configuration."""

        stored, revision = self.storage.get_settings()
        native = merge_sparse(default_config(), deepcopy(self.config.get("settings", default_config())))
        validate_config(native)
        marker = self.config.get("_settings_revision", 0)
        if isinstance(marker, bool) or not isinstance(marker, int) or marker < 0:
            marker = 0

        if revision == 0:
            revision = self._save_runtime(native, revision)
        elif marker == 0:
            # On upgrade, AstrBot adds schema defaults before the plugin starts.
            # Preserve existing Web UI edits unless the native page was edited.
            if native == default_config() or _matches_migrated_defaults(native, stored):
                native = stored
            elif native != stored:
                revision = self._save_runtime(native, revision)
        elif marker < revision:
            # A newer runtime revision takes precedence after a failed host save.
            native = stored
        elif native != stored:
            revision = self._save_runtime(native, revision)

        self.mirror(native, revision)
        return native

    def mirror(self, value: dict[str, Any], revision: int) -> None:
        """Publish a committed global revision to AstrBot's configuration page."""

        if self.config.get("settings") == value and self.config.get("_settings_revision") == revision:
            return
        previous_settings = self.config.get("settings")
        previous_revision = self.config.get("_settings_revision")
        self.config["settings"] = deepcopy(value)
        self.config["_settings_revision"] = revision
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception:
                if previous_settings is None:
                    self.config.pop("settings", None)
                else:
                    self.config["settings"] = previous_settings
                if previous_revision is None:
                    self.config.pop("_settings_revision", None)
                else:
                    self.config["_settings_revision"] = previous_revision
                raise

    def _save_runtime(self, value: dict[str, Any], revision: int) -> int:
        return self.storage.save_settings(
            None,
            value,
            expected_revision=revision,
            now=int(time.time()),
            updated_by="system:astrbot-config",
        )


def _matches_migrated_defaults(native: dict[str, Any], stored: dict[str, Any]) -> bool:
    """Recognize the one-time default updates in migration 006 before host reconciliation."""

    candidate = deepcopy(native)
    if candidate["commands"]["keywords"]["divorce_member"] == ["踹群友"]:
        candidate["commands"]["keywords"]["divorce_member"] = ["踹群友", "离婚群友"]
    errors = candidate["messages"]["errors"]
    if errors["divorce_ambiguous"] == "没有找到唯一匹配的关系；如果同名或跨玩法，请用“离婚老婆/离婚老公”并指定 #ID。":
        errors["divorce_ambiguous"] = "找到多段匹配关系，请用“离婚老婆/离婚老公/离婚群友”指定玩法和名称或 ID："
    if errors["divorce_relation_missing"] == "没有找到唯一匹配的关系；跨玩法或同名对象请使用明确指令和 #ID。":
        errors["divorce_relation_missing"] = "没有找到匹配的当前关系。"
    return candidate == stored
