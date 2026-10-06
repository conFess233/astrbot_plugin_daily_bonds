"""SQLite 迁移、作用域配置、周期推进与关系不变量入口。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..models import Scope, StorageError
from .settings import default_config, effective_config, merge_sparse, validate_config

_SCHEMA_VERSION = 12
_LOCAL_MIGRATION_CHECKSUMS = (
    "7c44e263ca99da770dd7d61d0aaa0cf5a5e879f1869d0a44dffc458db7e829d2",
    "bea912981c68fffc7c18e220f0b941441a2a390b606418e4ae27f0c5afeed4c6",
    "a939983fc85a3e652fe573ef935e5b342204724174290ad22409ace8f1ec24a7",
)
_NAMESPACE = uuid.UUID("1dc04dd2-af4f-4585-b350-0aa574597b9f")


class SQLiteStorage:
    """插件运行库。每次状态变更必须在短 BEGIN IMMEDIATE 事务中完成。"""

    def __init__(self, database_path: Path, schema_path: Path | None = None) -> None:
        self.database_path = database_path
        self.schema_path = schema_path or Path(__file__).with_name("migrations")
        self.connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        """打开运行库并迁移；损坏库或未知版本直接失败，不重建。"""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        self.connection = connection
        try:
            self._migrate()
            problems = connection.execute("PRAGMA quick_check").fetchall()
            if len(problems) != 1 or problems[0][0] != "ok":
                raise StorageError("SQLite quick_check 未通过，已停止插件初始化。")
        except Exception:
            connection.close()
            self.connection = None
            raise

    def close(self) -> None:
        """关闭连接并释放 SQLite 资源。"""

        with self._lock:
            if self.connection is not None:
                self.connection.close()
                self.connection = None

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """串行化单进程写事务，避免协程交错使用同一连接。"""

        with self._lock:
            connection = self._connection()
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                try:
                    connection.commit()
                except sqlite3.Error as exc:
                    connection.rollback()
                    raise StorageError(f"SQLite 提交失败：{exc}") from exc

    def ensure_scope(self, scope: Scope, umo: str, now: int) -> int:
        """创建或刷新机器人/群作用域，并返回本地数据库 ID。"""

        if not all((scope.platform_id, scope.self_id, scope.group_id, umo)):
            raise StorageError("作用域身份字段不得为空。")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO scopes(platform_id,self_id,group_id,umo,created_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(platform_id,self_id,group_id)
                   DO UPDATE SET umo=excluded.umo""",
                (scope.platform_id, scope.self_id, scope.group_id, umo, now),
            )
            row = db.execute(
                "SELECT id FROM scopes WHERE platform_id=? AND self_id=? AND group_id=?",
                (scope.platform_id, scope.self_id, scope.group_id),
            ).fetchone()
            assert row is not None
            return int(row["id"])

    def get_scope_id(self, scope: Scope) -> int | None:
        with self._lock:
            row = self._connection().execute(
                "SELECT id FROM scopes WHERE platform_id=? AND self_id=? AND group_id=?",
                (scope.platform_id, scope.self_id, scope.group_id),
            ).fetchone()
        return int(row["id"]) if row else None

    def get_settings(self, scope_id: int | None = None) -> tuple[dict[str, Any], int]:
        """返回全局值或指定群的全局合成快照与对应修订号。"""

        with self._lock:
            rows = self._connection().execute(
                "SELECT scope_key,revision,value_json FROM settings WHERE scope_key IN (?, 'global')",
                (f"scope:{scope_id}" if scope_id is not None else "global",),
            ).fetchall()
        global_row = next((row for row in rows if row["scope_key"] == "global"), None)
        defaults = default_config()
        global_value = merge_sparse(defaults, json.loads(global_row["value_json"])) if global_row else defaults
        if scope_id is None:
            validate_config(global_value)
            return global_value, int(global_row["revision"]) if global_row else 0
        scope_row = next((row for row in rows if row["scope_key"] == f"scope:{scope_id}"), None)
        override = json.loads(scope_row["value_json"]) if scope_row else {}
        return effective_config(global_value, override), int(scope_row["revision"]) if scope_row else 0

    def save_settings(
        self,
        scope_id: int | None,
        value: Mapping[str, Any],
        *,
        expected_revision: int,
        now: int,
        updated_by: str,
        request_id: str | None = None,
        request_hash: str | None = None,
    ) -> int:
        """按 revision 乐观锁保存配置；群值必须是稀疏覆盖。"""

        key = "global" if scope_id is None else f"scope:{scope_id}"
        if (request_id is None) != (request_hash is None):
            raise StorageError("幂等请求编号和摘要必须同时提供。")
        endpoint = f"settings:{key}"
        with self.transaction() as db:
            if request_id is not None and request_hash is not None:
                previous = db.execute(
                    "SELECT request_hash,response_json FROM web_request_dedup WHERE actor_username=? AND endpoint=? AND request_id=?",
                    (updated_by, endpoint, request_id),
                ).fetchone()
                if previous is not None:
                    if previous["request_hash"] != request_hash:
                        raise StorageError("request_id 已被用于不同的请求内容。")
                    return int(json.loads(previous["response_json"])["revision"])
            row = db.execute("SELECT revision FROM settings WHERE scope_key=?", (key,)).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if current_revision != expected_revision:
                raise StorageError(f"配置版本冲突：期望 {expected_revision}，当前 {current_revision}。")
            if scope_id is None:
                validate_config(value)
                saved = dict(value)
            else:
                allowed = {"enabled", "access", "reset", "commands", "modes", "statistics", "weights", "display", "members", "messages"}
                if set(value) - allowed:
                    raise StorageError("群覆盖包含仅允许全局设置的字段。")
                if "access" in value and set(value["access"]) - {"users", "extra_bot_ids"}:
                    raise StorageError("群覆盖只能设置用户规则和补充机器人名单。")
                defaults = default_config()
                merged = merge_sparse(defaults, value)
                validate_config(merged)
                saved = dict(value)
            revision = current_revision + 1
            db.execute(
                """INSERT INTO settings(scope_key,revision,schema_version,value_json,updated_at,updated_by)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                   revision=excluded.revision,schema_version=excluded.schema_version,
                   value_json=excluded.value_json,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                (key, revision, _SCHEMA_VERSION, json.dumps(saved, ensure_ascii=False, allow_nan=False), now, updated_by),
            )
            if request_id is not None and request_hash is not None:
                global_revision = revision
                if scope_id is not None:
                    global_row = db.execute("SELECT revision FROM settings WHERE scope_key='global'").fetchone()
                    global_revision = int(global_row["revision"]) if global_row else 0
                db.execute(
                    """INSERT INTO web_request_dedup(actor_username,endpoint,request_id,request_hash,response_json,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (updated_by, endpoint, request_id, request_hash,
                     json.dumps({
                         "revision": revision,
                         "global_revision": global_revision,
                     }), now),
                )
            return revision

    def get_settings_request_replay(
        self, scope_id: int | None, actor: str, request_id: str, request_hash: str
    ) -> dict[str, int] | None:
        """Return the original saved revisions, or reject a reused key with other content."""

        key = "global" if scope_id is None else f"scope:{scope_id}"
        endpoint = f"settings:{key}"
        with self._lock:
            row = self._connection().execute(
                "SELECT request_hash,response_json FROM web_request_dedup WHERE actor_username=? AND endpoint=? AND request_id=?",
                (actor, endpoint, request_id),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise StorageError("request_id 已被用于不同的请求内容。")
        result = json.loads(row["response_json"])
        return {"revision": int(result["revision"]), "global_revision": int(result["global_revision"])}

    def ensure_current_period(
        self,
        scope_id: int,
        now: int,
        timezone: str,
        reset_time: str,
    ) -> sqlite3.Row:
        """创建当前周期，或在停机跨日后直接推进到当前周期。"""

        zone = ZoneInfo(timezone)
        try:
            hour, minute = (int(part) for part in reset_time.split(":"))
        except (ValueError, AttributeError) as exc:
            raise StorageError("周期重置时刻格式无效。") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise StorageError("周期重置时刻超出范围。")
        current_local = datetime.fromtimestamp(now, UTC).astimezone(zone)
        boundary_date = current_local.date()
        start = _boundary(boundary_date, time(hour, minute), zone)
        if int(start.timestamp()) > now:
            boundary_date -= timedelta(days=1)
            start = _boundary(boundary_date, time(hour, minute), zone)
        end = _boundary(boundary_date + timedelta(days=1), time(hour, minute), zone)
        start_timestamp = int(start.timestamp())
        end_timestamp = int(end.timestamp())
        with self.transaction() as db:
            current = db.execute(
                "SELECT * FROM periods WHERE scope_id=? AND state='current'", (scope_id,)
            ).fetchone()
            if current and now < int(current["ends_at"]):
                return current
            if current:
                old_boundary = int(current["ends_at"])
                next_new_boundary = _next_boundary_after(old_boundary, time(hour, minute), zone)
                if now < next_new_boundary:
                    start_timestamp = old_boundary
                    end_timestamp = next_new_boundary
                sequence = int(current["sequence"]) + 1
                db.execute("UPDATE periods SET state='closed' WHERE id=?", (current["id"],))
                db.execute("UPDATE relationships SET state='ended',ended_at=?,end_reason='period_end' WHERE scope_id=? AND period_id=? AND state='active'", (int(current["ends_at"]), scope_id, current["id"]))
                db.execute("UPDATE redraw_credits SET state='expired' WHERE scope_id=? AND period_id=? AND state='available'", (scope_id, current["id"]))
                db.execute("UPDATE gift_invites SET state='invalidated',finalized_at=?,reason='period_end' WHERE scope_id=? AND period_id=? AND state='pending'", (now, scope_id, current["id"]))
            else:
                sequence_row = db.execute("SELECT COALESCE(MAX(sequence),0)+1 AS next FROM periods WHERE scope_id=?", (scope_id,)).fetchone()
                sequence = int(sequence_row["next"])
            period_id = str(uuid.uuid5(_NAMESPACE, f"{scope_id}:{sequence}:{start_timestamp}"))
            db.execute(
                "INSERT INTO periods(id,scope_id,sequence,starts_at,ends_at,timezone,reset_time,state) VALUES(?,?,?,?,?,?,?,'current')",
                (period_id, scope_id, sequence, start_timestamp, end_timestamp, timezone, reset_time),
            )
            return db.execute("SELECT * FROM periods WHERE id=?", (period_id,)).fetchone()

    def create_relationship(
        self,
        *,
        scope_id: int,
        period_id: str,
        mode: str,
        owner_id: str,
        subject_kind: str,
        subject_id: str,
        snapshot_id: str,
        capacity: int,
        now: int,
        slot_kind: str = "normal",
        ignore_capacity: bool = False,
    ) -> str:
        """在当前事务中统一检查容量、自配偶和 subject 唯一归属。"""

        if (
            mode not in {"wife", "husband", "member"}
            or capacity < 1
            or slot_kind not in {"normal", "steal", "designated"}
        ):
            raise StorageError("玩法或持有容量无效。")
        if slot_kind == "designated" and mode == "member":
            raise StorageError("群友玩法不支持指定槽位。")
        if ignore_capacity and (mode != "wife" or slot_kind != "normal"):
            raise StorageError("只有管理员设置的普通老婆关系可绕过容量。")
        if mode == "member":
            if subject_kind != "member" or owner_id == subject_id:
                raise StorageError("群友关系类型无效，禁止娶自己。")
        elif subject_kind != "character":
            raise StorageError("角色玩法只能持有角色对象。")
        connection = self._connection()
        if not connection.in_transaction:
            raise StorageError("关系创建必须包含在调用方的写事务中。")
        period = connection.execute(
            "SELECT 1 FROM periods WHERE id=? AND scope_id=? AND state='current'",
            (period_id, scope_id),
        ).fetchone()
        if period is None:
            raise StorageError("不能在非当前周期中创建关系。")
        snapshot = connection.execute(
            "SELECT subject_kind,subject_id FROM subject_snapshots WHERE id=?",
            (snapshot_id,),
        ).fetchone()
        if (
            snapshot is None
            or snapshot["subject_kind"] != subject_kind
            or snapshot["subject_id"] != subject_id
        ):
            raise StorageError("关系对象与不可变快照不匹配。")
        count = connection.execute(
            "SELECT COUNT(*) AS amount FROM relationships WHERE scope_id=? AND period_id=? AND mode=? AND owner_id=? AND slot_kind=? AND state='active'",
            (scope_id, period_id, mode, owner_id, slot_kind),
        ).fetchone()["amount"]
        if int(count) >= capacity and not ignore_capacity:
            raise StorageError("持有名额已满。")
        relation_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO relationships(id,scope_id,period_id,mode,owner_id,subject_kind,subject_id,snapshot_id,state,acquired_at,slot_kind)
               VALUES(?,?,?,?,?,?,?,?, 'active',?,?)""",
            (
                relation_id,
                scope_id,
                period_id,
                mode,
                owner_id,
                subject_kind,
                subject_id,
                snapshot_id,
                now,
                slot_kind,
            ),
        )
        return relation_id

    def accepts_migration_chain(self, applied: list[dict[str, Any]]) -> bool:
        """识别远端或已发布本地分支的完整迁移链，用于恢复预检。"""
        files = sorted(self.schema_path.glob("[0-9][0-9][0-9]_*.sql"))
        expected = [
            {
                "version": version,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for version, path in enumerate(files, 1)
        ]
        if not 8 <= len(applied) <= _SCHEMA_VERSION:
            return False
        if (
            len(applied) <= 11
            and applied[8:]
            and applied[8]["sha256"] == _LOCAL_MIGRATION_CHECKSUMS[0]
        ):
            expected = expected[:8] + [
                {"version": version, "sha256": checksum}
                for version, checksum in enumerate(_LOCAL_MIGRATION_CHECKSUMS, 9)
            ]
        return applied == expected[: len(applied)]

    def _migrate(self) -> None:
        connection = self._connection()
        migration_dir = self.schema_path
        migration_files = [
            migration_dir / "001_initial.sql",
            migration_dir / "002_catalog_packs.sql",
            migration_dir / "003_web_request_dedup.sql",
            migration_dir / "004_catalog_admin.sql",
            migration_dir / "005_admin_correction_preflights.sql",
            migration_dir / "006_period_resets.sql",
            migration_dir / "007_steal_slots.sql",
            migration_dir / "008_catalog_sync.sql",
            migration_dir / "009_admin_set_wife.sql",
            migration_dir / "010_designated_slots.sql",
            migration_dir / "011_activity_days.sql",
            migration_dir / "012_message_templates.sql",
        ]
        checksums = [
            hashlib.sha256(path.read_bytes()).hexdigest() for path in migration_files
        ]
        try:
            has_migrations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if has_migrations:
                versions = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                current = max((int(row["version"]) for row in versions), default=0)
            else:
                current = 0
            if current > _SCHEMA_VERSION:
                raise StorageError(
                    f"数据库版本 {current} 高于插件支持版本 {_SCHEMA_VERSION}。"
                )
            applied = (
                {
                    int(row["version"]): row["checksum"]
                    for row in connection.execute(
                        "SELECT version,checksum FROM schema_migrations"
                    ).fetchall()
                }
                if has_migrations
                else {}
            )
            if applied.get(9) == _LOCAL_MIGRATION_CHECKSUMS[0]:
                # 兼容已发布的本地分支：9～11 顺延，保留其数据与已执行的迁移。
                if (
                    current > 11
                    or any(
                        applied.get(version) != checksums[version - 1]
                        for version in range(1, 9)
                    )
                    or any(
                        applied.get(version) != _LOCAL_MIGRATION_CHECKSUMS[version - 9]
                        for version in range(9, current + 1)
                    )
                ):
                    raise StorageError("本地分支迁移校验和无效，已保留原数据库。")
                connection.execute("BEGIN IMMEDIATE")
                for version in range(current, 8, -1):
                    connection.execute(
                        "UPDATE schema_migrations SET version=?,checksum=? WHERE version=?",
                        (version + 1, checksums[version], version),
                    )
                connection.execute(
                    "INSERT INTO schema_migrations(version,applied_at,checksum) VALUES(9,unixepoch(),?)",
                    (checksums[8],),
                )
                connection.execute("DROP INDEX uq_active_subject")
                connection.execute(
                    "CREATE UNIQUE INDEX uq_active_subject ON relationships(scope_id,period_id,mode,subject_id) WHERE state='active' AND slot_kind<>'designated' AND mode IN ('husband','member')"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX uq_active_wife_owner_subject ON relationships(scope_id,period_id,owner_id,subject_id) WHERE state='active' AND mode='wife' AND slot_kind<>'designated'"
                )
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise StorageError("分支迁移的外键校验失败，已保留原数据库。")
                connection.commit()
                current += 1
                applied = {
                    int(row["version"]): row["checksum"]
                    for row in connection.execute(
                        "SELECT version,checksum FROM schema_migrations"
                    )
                }
            for version in range(1, current + 1):
                if version not in applied or applied[version] != checksums[version - 1]:
                    raise StorageError(f"数据库迁移 {version} 的校验和无效。")
            for version in range(current + 1, _SCHEMA_VERSION + 1):
                migration = migration_files[version - 1].read_text(encoding="utf-8")
                rebuild = version == 10
                if rebuild:
                    # SQLite 重建被其他表引用的父表，FK 开关必须在 BEGIN 之前设置。
                    connection.execute("PRAGMA foreign_keys=OFF")
                script = (
                    "BEGIN IMMEDIATE;\n"
                    + migration
                    + f"\nINSERT INTO schema_migrations(version,applied_at,checksum) VALUES({version},unixepoch(),'{checksums[version - 1]}');\n"
                    + ("" if rebuild else "COMMIT;")
                )
                try:
                    connection.executescript(script)
                    if rebuild:
                        if connection.execute("PRAGMA foreign_key_check").fetchone():
                            raise StorageError(
                                "关系槽位迁移的外键校验失败，已保留原数据。"
                            )
                        connection.commit()
                finally:
                    if rebuild:
                        if connection.in_transaction:
                            connection.rollback()
                        connection.execute("PRAGMA foreign_keys=ON")
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"SQLite 迁移失败：{exc}") from exc

    def _connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise StorageError("SQLite 尚未打开。")
        return self.connection


def _boundary(day: date, reset_at: time, zone: ZoneInfo) -> datetime:
    """解决夏令时：重复时刻取第一次，缺失时刻向前找首个有效分钟。"""

    naive = datetime.combine(day, reset_at)
    for offset in range(181):
        candidate = naive + timedelta(minutes=offset)
        aware = candidate.replace(tzinfo=zone, fold=0)
        round_trip = aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip == candidate:
            return aware
    raise StorageError(f"无法解析时区 {zone.key} 的周期边界。")


def _next_boundary_after(timestamp: int, reset_at: time, zone: ZoneInfo) -> int:
    """Return the first new-schedule boundary strictly after an existing period end."""

    local_day = datetime.fromtimestamp(timestamp, UTC).astimezone(zone).date()
    candidate = _boundary(local_day, reset_at, zone)
    while int(candidate.timestamp()) <= timestamp:
        local_day += timedelta(days=1)
        candidate = _boundary(local_day, reset_at, zone)
    return int(candidate.timestamp())
