-- 重建关系表以扩展槽位 CHECK；调用方在事务外暂时关闭 FK，并在提交前复核。
CREATE TABLE relationships_new (
  id TEXT PRIMARY KEY,
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  period_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('wife','husband','member')),
  owner_id TEXT NOT NULL,
  subject_kind TEXT NOT NULL CHECK(subject_kind IN ('character','member')),
  subject_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL REFERENCES subject_snapshots(id),
  state TEXT NOT NULL CHECK(state IN ('active','ended')),
  acquired_at INTEGER NOT NULL,
  ended_at INTEGER,
  end_reason TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
  slot_kind TEXT NOT NULL DEFAULT 'normal' CHECK(slot_kind IN ('normal','steal','designated')),
  FOREIGN KEY(scope_id,period_id) REFERENCES periods(scope_id,id),
  CHECK((mode='member' AND subject_kind='member' AND owner_id<>subject_id) OR
        (mode IN ('wife','husband') AND subject_kind='character')),
  CHECK(slot_kind<>'designated' OR mode IN ('wife','husband')),
  CHECK((state='active' AND ended_at IS NULL) OR (state='ended' AND ended_at IS NOT NULL))
);
INSERT INTO relationships_new
  (id,scope_id,period_id,mode,owner_id,subject_kind,subject_id,snapshot_id,state,acquired_at,ended_at,end_reason,version,slot_kind)
SELECT id,scope_id,period_id,mode,owner_id,subject_kind,subject_id,snapshot_id,state,acquired_at,ended_at,end_reason,version,slot_kind
FROM relationships;
DROP TABLE relationships;
ALTER TABLE relationships_new RENAME TO relationships;
CREATE UNIQUE INDEX uq_active_subject ON relationships(scope_id,period_id,mode,subject_id)
  WHERE state='active' AND slot_kind<>'designated' AND mode IN ('husband','member');
CREATE UNIQUE INDEX uq_active_wife_owner_subject ON relationships(scope_id,period_id,owner_id,subject_id)
  WHERE state='active' AND mode='wife' AND slot_kind<>'designated';
CREATE INDEX ix_owner_active ON relationships(scope_id,period_id,mode,owner_id,state);
CREATE INDEX ix_owner_active_slot ON relationships(scope_id,period_id,mode,owner_id,slot_kind,state);
ALTER TABLE daily_counters ADD COLUMN designated_draws INTEGER NOT NULL DEFAULT 0 CHECK(designated_draws>=0);
