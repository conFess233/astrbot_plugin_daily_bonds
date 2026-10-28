"""全局与群级配置、字体预览的管理 API。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

from astrbot.api.web import json_response, request

from ..models import ConfigurationError, InvalidInputError, StorageError
from ..services.fonts import available_fonts, font_preview
from ..services.message_templates import template_fields
from ..services.settings import SCOPE_OVERRIDABLE, effective_config, validate_config


class ConfigRoutes:
    """配置读取、预检与带版本校验的保存。"""

    async def get_config(self):
        _, denied = self._authorize()
        if denied:
            return denied
        try:
            scope_id = await asyncio.to_thread(self._query_scope_id)
            global_value, global_revision = await asyncio.to_thread(
                self.storage.get_settings
            )
            if scope_id is None:
                value, revision, override = global_value, global_revision, None
            else:
                value, revision = await asyncio.to_thread(
                    self.storage.get_settings, scope_id
                )
                override = await asyncio.to_thread(self._stored_override, scope_id)
            return json_response(
                {
                    "ok": True,
                    "data": {
                        "scope_id": scope_id,
                        "global": global_value,
                        "override": override,
                        "effective": value,
                        "template_fields": template_fields(),
                        # AstrBot's Plugin Page bridge may unwrap the outer API envelope.
                        # Keep the revision beside the config payload so the UI can
                        # normalize either wrapped or unwrapped responses.
                        "revision": {"global": global_revision, "scope": revision},
                    },
                    "revision": {"global": global_revision, "scope": revision},
                    "request_id": str(uuid.uuid4()),
                }
            )
        except ValueError as exc:
            return self._error("INVALID_SCOPE", str(exc), 400)
        except StorageError:
            return self._error("INTERNAL_ERROR", "读取配置失败，请稍后重试。", 500)

    async def preview_config(self):
        _, denied = self._authorize()
        if denied:
            return denied
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        try:
            scope_id, stored, effective = await asyncio.to_thread(
                self._prepare_draft, payload
            )
            impacted_scopes = await asyncio.to_thread(
                self._validate_effective_scopes, scope_id, effective
            )
            return json_response(
                {
                    "ok": True,
                    "data": {
                        "scope_id": scope_id,
                        "valid": True,
                        "stored_value": stored,
                        "effective": effective,
                        "affected_scopes": impacted_scopes,
                    },
                    "request_id": str(uuid.uuid4()),
                }
            )
        except (ConfigurationError, ValueError, TypeError) as exc:
            return self._error("CONFIG_INVALID", str(exc), 400)
        except StorageError:
            return self._error("INTERNAL_ERROR", "配置预检失败，请刷新后重试。", 500)

    async def save_config(self):
        username, denied = self._authorize()
        if denied:
            return denied
        if username is None:
            return username
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._error("INVALID_BODY", "请求内容必须是 JSON 对象。", 400)
        request_id = payload.get("request_id")
        expected_revision = payload.get("expected_revision")
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or len(request_id) > 128
        ):
            return self._error(
                "INVALID_REQUEST_ID", "request_id 必须是 1 到 128 个字符。", 400
            )
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            return self._error(
                "INVALID_REVISION", "expected_revision 必须是非负整数。", 400
            )
        try:
            scope_id = await asyncio.to_thread(self._requested_scope_id, payload)
            draft_hash = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            replay = await asyncio.to_thread(
                self.storage.get_settings_request_replay,
                scope_id,
                username,
                request_id.strip(),
                draft_hash,
            )
            if replay is not None:
                return json_response(
                    {
                        "ok": True,
                        "data": {
                            "scope_id": scope_id,
                            "effective": payload.get("draft"),
                            "revision": {
                                "global": replay["global_revision"],
                                "scope": replay["revision"],
                            },
                        },
                        "revision": {
                            "global": replay["global_revision"],
                            "scope": replay["revision"],
                        },
                        "request_id": request_id.strip(),
                    }
                )
            scope_id, stored, effective = await asyncio.to_thread(
                self._prepare_draft, payload
            )
            await asyncio.to_thread(
                self._validate_effective_scopes, scope_id, effective
            )
            revision = await asyncio.to_thread(
                self.storage.save_settings,
                scope_id,
                stored,
                expected_revision=expected_revision,
                now=int(time.time()),
                updated_by=username,
                request_id=request_id.strip(),
                request_hash=draft_hash,
            )
            saved_request = await asyncio.to_thread(
                self.storage.get_settings_request_replay,
                scope_id,
                username,
                request_id.strip(),
                draft_hash,
            )
            global_revision = (
                saved_request["global_revision"] if saved_request else revision
            )
            return json_response(
                {
                    "ok": True,
                    "data": {
                        "scope_id": scope_id,
                        "effective": effective,
                        "revision": {"global": global_revision, "scope": revision},
                    },
                    "revision": {"global": global_revision, "scope": revision},
                    "request_id": request_id.strip(),
                }
            )
        except (ConfigurationError, ValueError, TypeError) as exc:
            return self._error("CONFIG_INVALID", str(exc), 400)
        except StorageError as exc:
            message = str(exc)
            if "版本冲突" in message:
                return self._error("CONFIG_CONFLICT", message, 409)
            if "request_id" in message:
                return self._error("REQUEST_ID_CONFLICT", message, 409)
            if any(
                fragment in message
                for fragment in ("仅允许全局设置", "群覆盖", "用户规则")
            ):
                return self._error("CONFIG_INVALID", message, 400)
            return self._error(
                "INTERNAL_ERROR", "配置保存失败，请刷新后检查状态。", 500
            )

    def _stored_override(self, scope_id: int) -> dict[str, Any]:
        with self.storage._lock:
            row = (
                self.storage._connection()
                .execute(
                    "SELECT value_json FROM settings WHERE scope_key=?",
                    (f"scope:{scope_id}",),
                )
                .fetchone()
            )
        return json.loads(row["value_json"]) if row else {}

    def _prepare_draft(
        self, payload: Mapping[str, Any]
    ) -> tuple[int | None, dict[str, Any], dict[str, Any]]:
        scope_id = self._requested_scope_id(payload)
        draft = payload.get("draft")
        if not isinstance(draft, dict):
            raise InvalidInputError("draft 必须是完整配置对象。")
        if scope_id is None:
            validate_config(draft)
            return None, draft, draft
        global_value, _ = self.storage.get_settings()
        supplied_override = payload.get("override")
        if supplied_override is None:
            sparse = _sparse_diff(global_value, draft)
        elif isinstance(supplied_override, dict):
            sparse = supplied_override
            if effective_config(global_value, sparse) != draft:
                raise ConfigurationError("群覆盖与配置草稿不一致，请刷新后重试。")
            _validate_scope_override(sparse)
        else:
            raise ConfigurationError("群覆盖必须是对象。")
        effective = effective_config(global_value, sparse)
        validate_config(effective)
        return scope_id, sparse, effective

    def _requested_scope_id(self, payload: Mapping[str, Any]) -> int | None:
        raw_scope = payload.get("scope_id")
        if raw_scope in (None, "", "global"):
            return None
        if isinstance(raw_scope, int) and not isinstance(raw_scope, bool):
            scope_id = raw_scope
        elif (
            isinstance(raw_scope, str) and raw_scope.isascii() and raw_scope.isdecimal()
        ):
            scope_id = int(raw_scope)
        else:
            raise ValueError("scope_id 必须是整数或 global。")
        with self.storage._lock:
            exists = (
                self.storage._connection()
                .execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,))
                .fetchone()
            )
        if exists is None:
            raise ValueError("scope_id 不属于当前插件实例。")
        return scope_id

    def _validate_effective_scopes(
        self, changed_scope_id: int | None, changed_value: Mapping[str, Any]
    ) -> list[int]:
        if changed_scope_id is not None:
            validate_config(changed_value)
            return [changed_scope_id]
        with self.storage._lock:
            scope_ids = [
                int(row[0])
                for row in self.storage._connection().execute(
                    "SELECT id FROM scopes ORDER BY id"
                )
            ]
        affected: list[int] = []
        for scope_id in scope_ids:
            override = self._stored_override(scope_id)
            effective = effective_config(changed_value, override)
            validate_config(effective)
            affected.append(scope_id)
        return affected

    async def fonts(self):
        _actor, denied = self._authorize()
        if denied:
            return denied
        fonts = await asyncio.to_thread(available_fonts)
        return json_response(
            {"ok": True, "data": [{"id": f["id"], "name": f["name"]} for f in fonts]}
        )

    async def preview_font(self):
        _actor, denied = self._authorize()
        if denied:
            return denied
        try:
            data = await asyncio.to_thread(
                font_preview, request.query.get("font_id", "auto")
            )
            return json_response({"ok": True, "data": data})
        except (ValueError, OSError) as exc:
            return self._error("FONT_UNAVAILABLE", f"字体预览失败：{exc}", 400)


def _sparse_diff(base: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
    if set(base) != set(value):
        raise ConfigurationError("配置字段与当前版本不匹配。请刷新管理页后重试。")
    difference: dict[str, Any] = {}
    for key, base_value in base.items():
        changed = value[key]
        if isinstance(base_value, dict) and isinstance(changed, dict):
            nested = _sparse_diff(base_value, changed)
            if nested:
                difference[key] = nested
        elif changed != base_value:
            difference[key] = changed
    return difference


def _validate_scope_override(value: Mapping[str, Any]) -> None:
    if set(value) - SCOPE_OVERRIDABLE:
        raise ConfigurationError("群覆盖包含仅允许全局设置的字段。")
    access = value.get("access", {})
    if not isinstance(access, dict) or set(access) - {"users", "extra_bot_ids"}:
        raise ConfigurationError("群覆盖只能设置用户规则和补充机器人名单。")
