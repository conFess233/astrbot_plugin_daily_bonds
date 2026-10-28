"""一次性导入 0.1.x 写在 AstrBot 原生配置页中的业务配置。

0.2.0 起业务配置只保存在插件数据库。宿主会删除 schema 中不存在的键，
因此 `_conf_schema.json` 把 `settings` 保留为隐藏的自由字典，启动时导入后清空。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import MutableMapping
from copy import deepcopy
from typing import Any

from astrbot.api import logger

from .config_upgrade import upgrade_config
from .settings import default_config, merge_sparse, validate_config
from .storage import SQLiteStorage


def import_legacy_host_settings(
    config: MutableMapping[str, Any], storage: SQLiteStorage
) -> bool:
    """宿主配置中残留旧版设置时导入数据库；返回是否写入了新配置。"""

    legacy = config.get("settings")
    if not isinstance(legacy, dict) or not legacy:
        return False
    marker = config.get("_settings_revision", 0)
    if isinstance(marker, bool) or not isinstance(marker, int) or marker < 0:
        marker = 0
    stored, revision = storage.get_settings()
    upgraded, notes = upgrade_config(legacy, sparse=False)
    candidate = merge_sparse(default_config(), upgraded)
    validate_config(candidate)
    # 0.1.x 每次保存都会把数据库修订号写回宿主；只有原生页在最后一次同步后
    # 又被编辑（修订号不落后且内容不同），或数据库还没有配置时才采用宿主值。
    imported = revision == 0 or (marker >= revision and candidate != stored)
    if imported:
        storage.save_settings(
            None,
            candidate,
            expected_revision=revision,
            now=int(time.time()),
            updated_by="system:legacy-host-config",
        )
    with storage.transaction() as db:
        db.execute(
            "INSERT INTO migration_reports(id,source,from_version,to_version,report_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                "host_config",
                1,
                2,
                json.dumps({"imported": imported, "notes": notes}, ensure_ascii=False),
                int(time.time()),
            ),
        )
    previous = deepcopy(legacy), marker
    config["settings"] = {}
    config["_settings_revision"] = 0
    save = getattr(config, "save_config", None)
    if callable(save):
        try:
            save()
        except Exception:  # noqa: BLE001 - 宿主保存失败只影响清理，导入已完成
            config["settings"], config["_settings_revision"] = previous
            logger.warning(
                "今日姻缘：旧版宿主配置已导入，但清空宿主配置失败；下次启动会再次比对。",
                exc_info=True,
            )
    logger.info(
        "今日姻缘：已处理旧版宿主配置，%s。",
        "导入为当前配置" if imported else "数据库配置更新，保留数据库版本",
    )
    return imported
