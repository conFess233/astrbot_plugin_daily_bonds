-- 0.2.0：迁移报告、角色图鉴。设置转换在 Python 迁移步骤中完成。
CREATE TABLE migration_reports (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  from_version INTEGER NOT NULL,
  to_version INTEGER NOT NULL,
  report_json TEXT NOT NULL CHECK(json_valid(report_json)),
  created_at INTEGER NOT NULL
);
CREATE TABLE collection_entries (
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  owner_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('wife','husband')),
  character_id TEXT NOT NULL,
  times INTEGER NOT NULL CHECK(times >= 1),
  first_at INTEGER NOT NULL,
  last_at INTEGER NOT NULL,
  PRIMARY KEY(scope_id, owner_id, mode, character_id)
);
-- 用尚未清理的历史关系回填；更早的记录已按历史保留天数删除，无法恢复。
INSERT INTO collection_entries(scope_id,owner_id,mode,character_id,times,first_at,last_at)
SELECT scope_id, owner_id, mode, subject_id, COUNT(*), MIN(acquired_at), MAX(acquired_at)
FROM relationships
WHERE subject_kind='character' AND mode IN ('wife','husband')
GROUP BY scope_id, owner_id, mode, subject_id;
