"""Owned export, complete backup, and staged restore jobs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import struct
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from ..models import StorageError
from .storage import SQLiteStorage

_BACKUP_FORMAT = "astrbot-daily-bonds-backup"
_MAX_PATH = 512


class MaintenanceService:
    def __init__(self, storage: SQLiteStorage, data_root: Path) -> None:
        self.storage = storage
        self.data_root = data_root
        self.jobs_root = data_root / "jobs"
        self.backups_root = data_root / "backups"
        self.staging_root = data_root / "staging" / "maintenance"
        self.media_root = data_root / "media"
        self._request_lock = threading.RLock()

    def create_export(self, payload: dict[str, Any], *, actor: str, now: int) -> dict[str, Any]:
        pool_ids = payload.get("pool_ids")
        if not isinstance(pool_ids, list) or not pool_ids or len(pool_ids) > 500 or any(not isinstance(item, str) for item in pool_ids):
            raise ValueError("pool_ids must be a non-empty list with at most 500 IDs")
        if len(set(pool_ids)) != len(pool_ids):
            raise ValueError("pool_ids cannot contain duplicates")
        if payload.get("format", "zip") != "zip":
            raise ValueError("only ZIP role-pack exports are supported")
        artifact = self._build_role_pack(pool_ids, now)
        return self._create_job("export", artifact, actor, now, {"pool_ids": pool_ids})

    def create_backup(self, *, actor: str, now: int, keep_count: int = 10,
                      request_id: str | None = None) -> dict[str, Any]:
        with self._request_lock:
            digest = hashlib.sha256(b"complete-backup-v1").hexdigest()
            if request_id:
                with self.storage._lock:
                    prior = self.storage._connection().execute(
                        "SELECT request_hash,response_json FROM web_admin_dedup WHERE actor_username=? AND endpoint='backups/create' AND request_id=?",
                        (actor, request_id),
                    ).fetchone()
                if prior:
                    if prior["request_hash"] != digest:
                        raise StorageError("request_id was used for another backup request")
                    return json.loads(prior["response_json"])
            artifact = self._build_backup(now)
            result = self._create_job("backup", artifact, actor, now, {"complete": True})
            self.backups_root.mkdir(parents=True, exist_ok=True)
            retained = self.backups_root / f"{now}-{result['job_id']}.zip"
            shutil.copyfile(self.jobs_root / result["job_id"] / "artifact.zip", retained)
            self._prune_backups(keep_count)
            result["retained_backup"] = retained.name
            if request_id:
                with self.storage.transaction() as db:
                    db.execute("INSERT INTO web_admin_dedup(actor_username,endpoint,request_id,request_hash,response_json,created_at) VALUES(?,?,?,?,?,?)",
                               (actor, "backups/create", request_id, digest, json.dumps(result, ensure_ascii=False), now))
            return result

    def get_artifact(self, job_id: str, *, actor: str) -> tuple[Path, str] | None:
        with self.storage._lock:
            row = self.storage._connection().execute(
                "SELECT kind,state,actor,expires_at,staged_manifest_hash FROM maintenance_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None or row["actor"] != actor or row["kind"] not in {"export", "backup"} or row["state"] != "completed" or int(row["expires_at"]) < int(time.time()):
            return None
        path = self.jobs_root / job_id / "artifact.zip"
        if not path.is_file() or self._hash_file(path) != row["staged_manifest_hash"]:
            raise StorageError("maintenance artifact is missing or failed its hash check")
        return path, f"{row['kind']}-{job_id}.zip"

    def preview_restore(self, source: Path, *, actor: str, now: int, max_bytes: int,
                        max_entries: int, max_expanded: int) -> dict[str, Any]:
        if not source.is_file() or source.stat().st_size <= 0 or source.stat().st_size > max_bytes:
            raise ValueError("backup is empty or exceeds import_max_bytes")
        job_id = str(uuid.uuid4())
        stage = self.staging_root / job_id
        stage.mkdir(parents=True, exist_ok=False)
        try:
            manifest, names = self._stage_backup(source, stage, max_entries=max_entries, max_expanded=max_expanded)
            database_path = stage / "database.sqlite3"
            report = self._inspect_database(database_path, manifest)
            with self.storage._lock:
                _current_config, current_revision = self.storage.get_settings()
                current_state_hash = self._current_state_hash_locked()
                current_scopes = [dict(row) for row in self.storage._connection().execute(
                    "SELECT id,platform_id,self_id,group_id,umo FROM scopes ORDER BY id").fetchall()]
            source_scopes = report.pop("scopes")
            scope_mappings = []
            for source_scope in source_scopes:
                exact = next((scope for scope in current_scopes if all(
                    scope[key] == source_scope[key] for key in ("platform_id", "self_id", "group_id", "umo"))), None)
                scope_mappings.append({"source": source_scope, "exact_match": exact,
                                       "requires_mapping": exact is None})
            plan = {"job_id": job_id, "actor": actor, "created_at": now, "backup": manifest,
                    "files": names, "database": report, "current_revision": current_revision,
                    "current_state_hash": current_state_hash, "source_scopes": source_scopes,
                    "current_scopes": current_scopes, "scope_mappings": scope_mappings}
            plan_bytes = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            (stage / "plan.json").write_bytes(plan_bytes)
            digest = hashlib.sha256(plan_bytes).hexdigest()
            with self.storage.transaction() as db:
                db.execute("INSERT INTO maintenance_jobs(id,kind,actor,state,expected_revision,staged_manifest_hash,report_json,created_at,expires_at) VALUES(?,'restore',?,'ready',?,?,?,?,?)",
                           (job_id, actor, str(current_revision), digest, json.dumps(plan, ensure_ascii=False), now, now + 86400))
            return {"job_id": job_id, "expires_at": now + 86400, "expected_revision": current_revision,
                    "files": names, "database": report, "backup_created_at": manifest["created_at"],
                    "scope_mappings": scope_mappings, "current_scopes": current_scopes}
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def commit_restore(
        self, payload: dict[str, Any], *, actor: str, now: int, keep_count: int = 10
    ) -> dict[str, Any]:
        job_id = self._text(payload.get("job_id"), "job_id", 64)
        request_id = self._text(payload.get("request_id"), "request_id", 128)
        expected_revision = payload.get("expected_revision")
        if payload.get("confirm") is not True:
            raise ValueError("confirm must be true after reviewing the restore preview")
        scope_mapping = payload.get("scope_mapping", {})
        if not isinstance(scope_mapping, dict) or any(
            not isinstance(key, str) for key in scope_mapping
        ):
            raise ValueError("scope_mapping must be an object keyed by backup scope ID")
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "job_id": job_id,
                    "expected_revision": expected_revision,
                    "confirm": True,
                    "scope_mapping": scope_mapping,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self.storage.transaction() as db:
            previous = db.execute(
                "SELECT request_hash,response_json FROM web_admin_dedup WHERE actor_username=? AND endpoint='restore/commit' AND request_id=?",
                (actor, request_id),
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise StorageError("request_id was used for another restore")
                return json.loads(previous["response_json"])
            job = db.execute(
                "SELECT * FROM maintenance_jobs WHERE id=? AND kind='restore' AND actor=?",
                (job_id, actor),
            ).fetchone()
            if job is None or int(job["expires_at"]) < now or job["state"] != "ready":
                raise StorageError(
                    "restore job is missing, expired, or already committed"
                )
        stage = self.staging_root / job_id
        plan_path = stage / "plan.json"
        try:
            plan_bytes = plan_path.read_bytes()
        except OSError as exc:
            raise StorageError(
                "restore staging has expired; preview the backup again"
            ) from exc
        if hashlib.sha256(plan_bytes).hexdigest() != job["staged_manifest_hash"]:
            raise StorageError("restore preview hash check failed")
        plan = json.loads(plan_bytes)
        if str(expected_revision) != str(plan["current_revision"]) or str(
            expected_revision
        ) != str(job["expected_revision"]):
            raise StorageError(
                "current database revision changed; preview restore again"
            )
        for name, spec in plan["backup"]["files"].items():
            staged_path = stage / self._safe_name(name)
            if (
                not staged_path.is_file()
                or staged_path.stat().st_size != int(spec["bytes"])
                or self._hash_file(staged_path) != spec["sha256"]
            ):
                raise StorageError(
                    f"staged restore file failed its preview hash check: {name}"
                )
        database_path = stage / "database.sqlite3"
        media_stage = stage / "media"
        old_database = self.data_root / f".restore-old-{job_id}.sqlite3"
        old_media = self.data_root / f".restore-old-{job_id}-media"
        live_database = self.storage.database_path
        moved_db = moved_media = False
        with self.storage._lock:
            if self._current_state_hash_locked() != plan["current_state_hash"]:
                raise StorageError(
                    "database or media changed after restore preview; preview again"
                )
            backup_file = self._build_backup(now)
            self.backups_root.mkdir(parents=True, exist_ok=True)
            automatic_backup = self.backups_root / f"automatic-{now}-{job_id}.zip"
            shutil.copyfile(backup_file, automatic_backup)
            self._prune_backups(keep_count)
            response = {
                "restored": True,
                "job_id": job_id,
                "automatic_backup": automatic_backup.name,
                "database": plan["database"],
                "restored_at": now,
            }
            staged_db = sqlite3.connect(database_path)
            try:
                staged_db.execute("PRAGMA foreign_keys=ON")
                staged_db.execute("BEGIN IMMEDIATE")
                if not isinstance(scope_mapping, dict) or len(scope_mapping) > len(
                    plan["source_scopes"]
                ):
                    raise ValueError(
                        "scope_mapping must map backup scope IDs to target scope identities"
                    )
                source_ids = {str(int(scope["id"])) for scope in plan["source_scopes"]}
                if set(scope_mapping) - source_ids:
                    raise ValueError(
                        "scope_mapping contains an unknown backup scope ID"
                    )
                targets: set[tuple[str, str, str]] = set()
                for source_scope in plan["source_scopes"]:
                    source_id = int(source_scope["id"])
                    candidate = scope_mapping.get(str(source_id))
                    if candidate is None:
                        exact = next(
                            (
                                scope
                                for scope in plan["current_scopes"]
                                if all(
                                    scope[key] == source_scope[key]
                                    for key in (
                                        "platform_id",
                                        "self_id",
                                        "group_id",
                                        "umo",
                                    )
                                )
                            ),
                            None,
                        )
                        if exact is None:
                            raise ValueError(
                                f"scope {source_id} needs an explicit target mapping"
                            )
                        candidate = {
                            key: source_scope[key]
                            for key in ("platform_id", "self_id", "group_id", "umo")
                        }
                    if not isinstance(candidate, dict):
                        raise ValueError(f"scope {source_id} mapping must be an object")
                    target = tuple(
                        self._text(candidate.get(key), f"scope {source_id} {key}", 256)
                        for key in ("platform_id", "self_id", "group_id", "umo")
                    )
                    if not any(
                        tuple(
                            str(scope[key])
                            for key in ("platform_id", "self_id", "group_id", "umo")
                        )
                        == target
                        for scope in plan["current_scopes"]
                    ):
                        raise ValueError(
                            f"scope {source_id} target must match a currently known scope"
                        )
                    identity = target[:3]
                    if identity in targets:
                        raise ValueError(
                            "multiple backup scopes cannot map to the same target group"
                        )
                    targets.add(identity)
                    staged_db.execute(
                        "UPDATE scopes SET platform_id=?,self_id=?,group_id=?,umo=? WHERE id=?",
                        (*target, source_id),
                    )
                staged_db.execute(
                    "INSERT INTO web_admin_dedup(actor_username,endpoint,request_id,request_hash,response_json,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        actor,
                        "restore/commit",
                        request_id,
                        request_hash,
                        json.dumps(response, ensure_ascii=False),
                        now,
                    ),
                )
                staged_db.execute(
                    "INSERT INTO maintenance_jobs(id,kind,actor,state,expected_revision,staged_manifest_hash,report_json,created_at,expires_at) VALUES(?,'restore',?,'completed',?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state='completed',report_json=excluded.report_json",
                    (
                        job_id,
                        actor,
                        str(expected_revision),
                        job["staged_manifest_hash"],
                        json.dumps(response, ensure_ascii=False),
                        now,
                        now + 86400,
                    ),
                )
                staged_db.execute(
                    "INSERT INTO web_admin_audit(id,actor_username,action,entity_kind,entity_id,request_id,reason,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        actor,
                        "restore",
                        "database",
                        job_id,
                        request_id,
                        "confirmed full backup restore",
                        json.dumps(response, ensure_ascii=False),
                        now,
                    ),
                )
                staged_db.execute(
                    "UPDATE gift_invites SET state='invalidated',finalized_at=?,reason='restore' WHERE state='pending'",
                    (now,),
                )
                staged_db.execute(
                    "UPDATE notification_outbox SET state='unknown',error_summary='restore canceled pending notification' WHERE state IN ('pending','sending')"
                )
                # 恢复是新配置版本；宿主同步失败时，重载仍须优先采用恢复后的配置。
                staged_db.execute(
                    "UPDATE settings SET revision=MAX(revision, ?)+1 WHERE scope_key='global'",
                    (plan["current_revision"],),
                )
                staged_db.commit()
                check = staged_db.execute("PRAGMA integrity_check").fetchall()
                if len(check) != 1 or check[0][0] != "ok":
                    raise StorageError(
                        "staged restored database failed integrity_check"
                    )
            finally:
                staged_db.close()
            connection = self.storage._connection()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.storage.close()
            try:
                for suffix in ("-wal", "-shm"):
                    live_database.with_name(live_database.name + suffix).unlink(
                        missing_ok=True
                    )
                live_database.replace(old_database)
                moved_db = True
                if self.media_root.exists():
                    self.media_root.replace(old_media)
                    moved_media = True
                database_path.replace(live_database)
                if media_stage.exists():
                    media_stage.replace(self.media_root)
                self.storage.open()
                self._finalize_restored_state(now)
            except Exception:
                self.storage.close()
                if live_database.exists():
                    live_database.unlink()
                if moved_db and old_database.exists():
                    old_database.replace(live_database)
                if self.media_root.exists():
                    shutil.rmtree(self.media_root, ignore_errors=True)
                if moved_media and old_media.exists():
                    old_media.replace(self.media_root)
                self.storage.open()
                raise
        old_database.unlink(missing_ok=True)
        shutil.rmtree(old_media, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)
        return response

    def _build_role_pack(self, pool_ids: list[str], now: int) -> Path:
        rows: list[dict[str, Any]] = []
        members: dict[str, set[str]] = {pool_id: set() for pool_id in pool_ids}
        with self.storage._lock:
            db = self.storage._connection()
            pools = db.execute(f"SELECT id,name,mode FROM pools WHERE id IN ({','.join('?' for _ in pool_ids)}) AND deleted_at IS NULL ORDER BY id", pool_ids).fetchall()
            if len(pools) != len(pool_ids):
                raise ValueError("one or more pool IDs do not exist or are deleted")
            for row in db.execute(f"SELECT pool_id,character_id FROM pool_members WHERE pool_id IN ({','.join('?' for _ in pool_ids)})", pool_ids):
                members[row["pool_id"]].add(row["character_id"])
            role_ids = sorted({character_id for values in members.values() for character_id in values})
            if not role_ids:
                raise ValueError("selected pools contain no characters")
            chars = db.execute(f"SELECT id,name,aliases_json,gender,enabled,provenance_json FROM characters WHERE id IN ({','.join('?' for _ in role_ids)}) AND deleted_at IS NULL ORDER BY id", role_ids).fetchall()
            images = db.execute(f"SELECT ci.character_id,ci.media_hash,mb.relative_path,mb.mime_type,mb.byte_size,mb.width,mb.height FROM character_images ci JOIN media_blobs mb ON mb.hash=ci.media_hash WHERE ci.character_id IN ({','.join('?' for _ in role_ids)}) ORDER BY ci.character_id,ci.ordinal", role_ids).fetchall()
        image_rows: dict[str, list[dict[str, Any]]] = {role_id: [] for role_id in role_ids}
        blobs: dict[str, Path] = {}
        for image in images:
            ext = Path(image["relative_path"]).suffix.lower().lstrip(".") or "img"
            relative = f"images/{image['media_hash']}.{ext}"
            path = self.media_root / image["relative_path"]
            if not path.is_file() or self._hash_file(path) != image["media_hash"]:
                raise StorageError(f"media blob {image['media_hash']} is missing or corrupt")
            blobs[relative] = path
            image_rows[image["character_id"]].append({"path": relative, "sha256": image["media_hash"], "bytes": image["byte_size"],
                                                        "width": image["width"], "height": image["height"], "mime_type": image["mime_type"]})
        for row in chars:
            provenance = json.loads(row["provenance_json"])
            character = {key: value for key, value in provenance.items() if key not in {"import_pack", "import_image_sources"}}
            character.update({"id": row["id"], "name": row["name"], "aliases": json.loads(row["aliases_json"]),
                              "gender": row["gender"], "enabled": bool(row["enabled"]),
                              "pool_ids": sorted(pool_id for pool_id, values in members.items() if row["id"] in values),
                              "images": image_rows[row["id"]]})
            rows.append(character)
        manifest = {"schema_version": 1, "pack_id": f"export-{uuid.uuid4().hex[:12]}", "pack_version": str(now),
                    "pools": [{"id": row["id"], "name": row["name"], "mode": row["mode"]} for row in pools], "characters": rows}
        path = self._temporary_artifact("role-pack-")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
            for relative, blob in blobs.items():
                archive.write(blob, relative)
        return path

    def _current_state_hash_locked(self) -> str:
        digest = hashlib.sha256()
        connection = self.storage._connection()
        tables = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for table in tables:
            name = str(table["name"])
            if name in {"maintenance_jobs", "web_admin_dedup"}:
                continue
            digest.update(name.encode("utf-8"))
            digest.update((table["sql"] or "").encode("utf-8"))
            try:
                rows = connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            except sqlite3.OperationalError:
                rows = connection.execute(f'SELECT * FROM "{name}"').fetchall()
            for row in rows:
                values = [value.hex() if isinstance(value, bytes) else value for value in tuple(row)]
                digest.update(json.dumps(values, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8"))
                digest.update(b"\n")
        if self.media_root.is_dir():
            for path in sorted(self.media_root.rglob("*")):
                if path.is_symlink():
                    raise StorageError("media contains a symlink and cannot be safely restored")
                if path.is_file():
                    digest.update(path.relative_to(self.media_root).as_posix().encode("utf-8"))
                    digest.update(self._hash_file(path).encode("ascii"))
        return digest.hexdigest()

    def _build_backup(self, now: int) -> Path:
        path = self._temporary_artifact("full-backup-")
        with tempfile.TemporaryDirectory(prefix="daily-bonds-backup-") as temp:
            root = Path(temp)
            database = root / "database.sqlite3"
            with self.storage._lock:
                source = self.storage._connection()
                target = sqlite3.connect(database)
                target.row_factory = sqlite3.Row
                try:
                    source.backup(target)
                    result = target.execute("PRAGMA integrity_check").fetchall()
                    if len(result) != 1 or result[0][0] != "ok":
                        raise StorageError("database backup failed integrity_check")
                    schema_rows = target.execute("SELECT version,checksum FROM schema_migrations ORDER BY version").fetchall()
                    media_rows = target.execute("SELECT hash,relative_path,byte_size FROM media_blobs ORDER BY hash").fetchall()
                    configuration_rows = target.execute(
                        "SELECT scope_key,revision,schema_version FROM settings ORDER BY scope_key").fetchall()
                finally:
                    target.close()
                members = [("database.sqlite3", database)]
                for row in media_rows:
                    raw_relative = str(row["relative_path"])
                    relative = PurePosixPath(raw_relative)
                    if "\\" in raw_relative or ":" in raw_relative or relative.as_posix() != raw_relative or relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                        raise StorageError("database contains an unsafe media path")
                    media_file = self.media_root.joinpath(*relative.parts)
                    if any(parent.is_symlink() for parent in (media_file, *media_file.parents) if parent == self.media_root or self.media_root in parent.parents):
                        raise StorageError("plugin media contains a symlink and cannot be backed up")
                    if not media_file.is_file() or media_file.stat().st_size != int(row["byte_size"]) or self._hash_file(media_file) != row["hash"]:
                        raise StorageError(f"referenced media {row['hash']} is missing or corrupt")
                    members.append((media_file.relative_to(self.data_root).as_posix(), media_file))
                _config, configuration_revision = self.storage.get_settings()
                database_schema = {"version": max((int(row[0]) for row in schema_rows), default=0),
                                   "migrations": [{"version": int(row[0]), "sha256": str(row[1])}
                                                   for row in schema_rows]}
                manifest = {"format": _BACKUP_FORMAT, "schema_version": 1, "created_at": now,
                            "database_schema": database_schema,
                            "configuration_versions": [{"scope_key": str(row["scope_key"]),
                                                         "revision": int(row["revision"]),
                                                         "schema_version": int(row["schema_version"])}
                                                        for row in configuration_rows],
                            "configuration_revision": configuration_revision, "files": {}}
                for name, member in members:
                    manifest["files"][name] = {"bytes": member.stat().st_size, "sha256": self._hash_file(member)}
                with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("backup.json", json.dumps(manifest, separators=(",", ":")))
                    for name, member in members:
                        archive.write(member, name)
        return path

    def _stage_backup(self, source: Path, stage: Path, *, max_entries: int, max_expanded: int) -> tuple[dict[str, Any], list[str]]:
        self._preflight_zip_count(source, max_entries)
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > max_entries:
                raise ValueError("backup entry count is empty or exceeds the configured limit")
            entries: dict[str, zipfile.ZipInfo] = {}
            folded: set[str] = set()
            total = 0
            for info in infos:
                name = self._safe_name(info.filename)
                if info.is_dir():
                    continue
                if name.casefold() in folded:
                    raise ValueError("backup contains duplicate or case-colliding paths")
                folded.add(name.casefold())
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or info.flag_bits & 1:
                    raise ValueError("backup may not contain symlinks or encrypted files")
                total += info.file_size
                if total > max_expanded:
                    raise ValueError("backup expanded size exceeds the configured limit")
                entries[name] = info
            if "backup.json" not in entries or "database.sqlite3" not in entries:
                raise ValueError("backup must contain backup.json and database.sqlite3")
            manifest = json.loads(archive.read(entries["backup.json"]))
            if not isinstance(manifest, dict) or manifest.get("format") != _BACKUP_FORMAT or manifest.get("schema_version") != 1:
                raise ValueError("unsupported backup format or schema version")
            expected = manifest.get("files")
            if not isinstance(expected, dict) or set(expected) != set(entries) - {"backup.json"}:
                raise ValueError("backup file list does not match its manifest")
            names = []
            for name, info in entries.items():
                if name == "backup.json":
                    continue
                spec = expected[name]
                if not isinstance(spec, dict) or int(spec.get("bytes", -1)) != info.file_size:
                    raise ValueError(f"backup size mismatch for {name}")
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info) as reader, destination.open("wb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        size += len(chunk)
                        digest.update(chunk)
                        writer.write(chunk)
                if size != info.file_size or digest.hexdigest() != spec.get("sha256"):
                    raise ValueError(f"backup hash mismatch for {name}")
                names.append(name)
            return manifest, names

    @staticmethod
    def _preflight_zip_count(source: Path, max_entries: int) -> None:
        size = source.stat().st_size
        tail_size = min(size, 22 + 65_535)
        with source.open("rb") as stream:
            stream.seek(size - tail_size)
            tail = stream.read(tail_size)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise ValueError("ZIP end-of-directory record is missing or invalid")
        record = tail[offset:offset + 22]
        _signature, disk, central_disk, disk_entries, total_entries, central_size, central_offset, comment_size = struct.unpack("<4s4H2LH", record)
        if len(tail) - offset != 22 + comment_size or disk != 0 or central_disk != 0 or disk_entries != total_entries:
            raise ValueError("multi-disk or malformed ZIP archives are not supported")
        if total_entries in {0xFFFF} or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
            raise ValueError("ZIP64 archives exceed the supported entry-count envelope")
        if total_entries > max_entries:
            raise ValueError("backup entry count exceeds the configured limit")
        if central_size > min(16 * 1024 * 1024, max_entries * 4096):
            raise ValueError("ZIP central directory is too large for its configured entry-count limit")
        if central_offset + central_size > size - tail_size + offset:
            raise ValueError("ZIP central directory is outside the uploaded file")

    def _inspect_database(self, path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            if len(integrity) != 1 or integrity[0][0] != "ok" or foreign:
                raise ValueError("backup database integrity or foreign-key validation failed")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"schema_migrations", "settings", "characters", "pools", "relationships", "media_blobs", "scopes",
                        "maintenance_jobs", "web_admin_dedup", "web_admin_audit"}
            if not required.issubset(tables):
                raise ValueError("backup database does not contain required plugin tables")
            migrations = connection.execute("SELECT version,checksum FROM schema_migrations ORDER BY version").fetchall()
            applied = [{"version": int(row[0]), "sha256": str(row[1])} for row in migrations]
            bundled = sorted(self.storage.schema_path.glob("[0-9][0-9][0-9]_*.sql"))
            expected = [{"version": version, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
                        for version, file in enumerate(bundled, 1)]
            database_schema = {"version": max((item["version"] for item in applied), default=0),
                               "migrations": applied}
            if applied != expected or database_schema != manifest.get("database_schema"):
                raise ValueError("backup database migration version or checksum does not match this plugin")
            configuration_versions = [{"scope_key": str(row[0]), "revision": int(row[1]),
                                       "schema_version": int(row[2])}
                                      for row in connection.execute(
                                          "SELECT scope_key,revision,schema_version FROM settings ORDER BY scope_key")]
            if configuration_versions != manifest.get("configuration_versions"):
                raise ValueError("backup configuration version metadata does not match its database")
            media_rows = connection.execute("SELECT hash,relative_path,byte_size FROM media_blobs ORDER BY hash").fetchall()
            for row in media_rows:
                raw_relative = str(row["relative_path"])
                relative = PurePosixPath(raw_relative)
                if "\\" in raw_relative or ":" in raw_relative or relative.as_posix() != raw_relative or relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                    raise ValueError("backup database contains an unsafe media path")
                media_file = path.parent.joinpath("media", *relative.parts)
                if not media_file.is_file() or media_file.stat().st_size != int(row["byte_size"]) or self._hash_file(media_file) != row["hash"]:
                    raise ValueError(f"backup media reference {row['hash']} is missing or corrupt")
            counts = {name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                      for name in sorted(required)}
            scopes = [dict(row) for row in connection.execute(
                "SELECT id,platform_id,self_id,group_id,umo FROM scopes ORDER BY id").fetchall()]
            pending_invites = int(connection.execute("SELECT COUNT(*) FROM gift_invites WHERE state='pending'").fetchone()[0])
            return {"integrity": "ok", "foreign_key_errors": 0, "tables": counts,
                    "database_bytes": path.stat().st_size, "scopes": scopes,
                    "pending_invites_to_invalidate": pending_invites,
                    "configuration_versions": configuration_versions}
        finally:
            connection.close()

    def _finalize_restored_state(self, now: int) -> None:
        db = self.storage._connection()
        scope_ids = [int(row[0]) for row in db.execute("SELECT id FROM scopes ORDER BY id").fetchall()]
        for scope_id in scope_ids:
            config, _revision = self.storage.get_settings(scope_id)
            reset = config["reset"]
            self.storage.ensure_current_period(scope_id, now, str(reset["timezone"]), str(reset["time"]))

    def _create_job(self, kind: str, artifact: Path, actor: str, now: int, report: dict[str, Any]) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        target_dir = self.jobs_root / job_id
        target_dir.mkdir(parents=True, exist_ok=False)
        target = target_dir / "artifact.zip"
        artifact.replace(target)
        digest = self._hash_file(target)
        response = {"job_id": job_id, "kind": kind, "state": "completed", "bytes": target.stat().st_size,
                    "sha256": digest, "expires_at": now + 86400, "download": f"downloads/{job_id}"}
        _config, revision = self.storage.get_settings()
        with self.storage.transaction() as db:
            db.execute("INSERT INTO maintenance_jobs(id,kind,actor,state,expected_revision,staged_manifest_hash,report_json,created_at,expires_at) VALUES(?,?,?,'completed',?,?,?,?,?)",
                       (job_id, kind, actor, str(revision), digest, json.dumps({**report, **response}, ensure_ascii=False), now, now + 86400))
        return response

    def _temporary_artifact(self, prefix: str) -> Path:
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".zip", dir=self.jobs_root)
        os.close(descriptor)
        return Path(name)

    def _prune_backups(self, keep_count: int) -> None:
        keep_count = min(100, max(1, int(keep_count)))
        backups = sorted((path for path in self.backups_root.glob("*.zip") if path.is_file()), key=lambda item: item.stat().st_mtime, reverse=True)
        for path in backups[keep_count:]:
            path.unlink(missing_ok=True)

    @staticmethod
    def _safe_name(value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > _MAX_PATH or "\\" in value or "\x00" in value or value.startswith("/") or ":" in value:
            raise ValueError("backup contains an absolute or invalid path")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("backup path contains an empty, dot, or parent segment")
        return "/".join(parts)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{label} is empty or too long")
        return value.strip()
