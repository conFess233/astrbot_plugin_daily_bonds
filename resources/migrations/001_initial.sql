-- 今日姻缘实际首版迁移：初始化空库的版本化 DDL。
PRAGMA foreign_keys = ON;

CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL,
  checksum TEXT NOT NULL
);
CREATE TABLE scopes (
  id INTEGER PRIMARY KEY,
  platform_id TEXT NOT NULL,
  self_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  umo TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(platform_id, self_id, group_id)
);
CREATE TABLE settings (
  scope_key TEXT PRIMARY KEY, -- global 或 scope:<id>，群记录仅存稀疏覆盖
  revision INTEGER NOT NULL CHECK(revision >= 1),
  schema_version INTEGER NOT NULL,
  value_json TEXT NOT NULL CHECK(json_valid(value_json)),
  pending_reset_json TEXT CHECK(pending_reset_json IS NULL OR json_valid(pending_reset_json)),
  updated_at INTEGER NOT NULL,
  updated_by TEXT NOT NULL
);
CREATE TABLE periods (
  id TEXT PRIMARY KEY,
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  sequence INTEGER NOT NULL CHECK(sequence >= 1),
  starts_at INTEGER NOT NULL,
  ends_at INTEGER NOT NULL CHECK(ends_at > starts_at),
  timezone TEXT NOT NULL,
  reset_time TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('current','closed')),
  UNIQUE(scope_id, sequence),
  UNIQUE(scope_id, id)
);
CREATE UNIQUE INDEX uq_period_current ON periods(scope_id) WHERE state='current';
CREATE TABLE members (
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  user_id TEXT NOT NULL,
  nickname TEXT NOT NULL,
  card TEXT NOT NULL DEFAULT '',
  is_present INTEGER NOT NULL CHECK(is_present IN (0,1)),
  is_known_bot INTEGER NOT NULL CHECK(is_known_bot IN (0,1)),
  last_observed_at INTEGER NOT NULL,
  PRIMARY KEY(scope_id,user_id)
);
CREATE TABLE media_blobs (
  hash TEXT PRIMARY KEY,
  relative_path TEXT NOT NULL UNIQUE,
  mime_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL CHECK(byte_size > 0),
  width INTEGER NOT NULL CHECK(width > 0),
  height INTEGER NOT NULL CHECK(height > 0),
  source_url TEXT,
  verified_at INTEGER NOT NULL
);
CREATE TABLE characters (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(aliases_json)),
  gender TEXT NOT NULL CHECK(gender IN ('female','male','unspecified')),
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  deleted_at INTEGER,
  revision INTEGER NOT NULL CHECK(revision >= 1),
  provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json))
);
CREATE TABLE character_images (
  character_id TEXT NOT NULL REFERENCES characters(id),
  media_hash TEXT NOT NULL REFERENCES media_blobs(hash),
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  PRIMARY KEY(character_id,media_hash)
);
CREATE TABLE pools (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('wife','husband')),
  builtin INTEGER NOT NULL CHECK(builtin IN (0,1)),
  deleted_at INTEGER,
  revision INTEGER NOT NULL CHECK(revision >= 1)
);
CREATE TABLE pool_members (
  pool_id TEXT NOT NULL REFERENCES pools(id),
  character_id TEXT NOT NULL REFERENCES characters(id),
  PRIMARY KEY(pool_id,character_id)
);
CREATE TABLE subject_snapshots (
  id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL CHECK(subject_kind IN ('character','member')),
  subject_id TEXT NOT NULL,
  name TEXT NOT NULL,
  aliases_json TEXT NOT NULL CHECK(json_valid(aliases_json)),
  source_revision INTEGER,
  created_at INTEGER NOT NULL
);
CREATE TABLE snapshot_images (
  snapshot_id TEXT NOT NULL REFERENCES subject_snapshots(id),
  media_hash TEXT NOT NULL REFERENCES media_blobs(hash),
  PRIMARY KEY(snapshot_id,media_hash)
);
CREATE TABLE relationships (
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
  FOREIGN KEY(scope_id,period_id) REFERENCES periods(scope_id,id),
  CHECK((mode='member' AND subject_kind='member' AND owner_id<>subject_id) OR
        (mode IN ('wife','husband') AND subject_kind='character')),
  CHECK((state='active' AND ended_at IS NULL) OR (state='ended' AND ended_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_active_subject ON relationships(scope_id,period_id,mode,subject_id) WHERE state='active';
CREATE INDEX ix_owner_active ON relationships(scope_id,period_id,mode,owner_id,state);
CREATE TABLE daily_counters (
  scope_id INTEGER NOT NULL,
  period_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('wife','husband','member')),
  user_id TEXT NOT NULL,
  normal_draws INTEGER NOT NULL DEFAULT 0 CHECK(normal_draws>=0),
  steal_attempts INTEGER NOT NULL DEFAULT 0 CHECK(steal_attempts>=0),
  stolen_successes INTEGER NOT NULL DEFAULT 0 CHECK(stolen_successes>=0),
  divorces INTEGER NOT NULL DEFAULT 0 CHECK(divorces>=0),
  last_steal_at INTEGER,
  PRIMARY KEY(scope_id,period_id,mode,user_id),
  FOREIGN KEY(scope_id,period_id) REFERENCES periods(scope_id,id)
);
CREATE TABLE redraw_credits (
  id TEXT PRIMARY KEY,
  scope_id INTEGER NOT NULL,
  period_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('wife','husband','member')),
  user_id TEXT NOT NULL,
  source_operation_id TEXT NOT NULL,
  reason TEXT NOT NULL CHECK(reason IN ('stolen','divorce','member_removed','admin_grant')),
  state TEXT NOT NULL CHECK(state IN ('available','consumed','expired')),
  issued_at INTEGER NOT NULL,
  consumed_at INTEGER,
  consumed_relationship_id TEXT REFERENCES relationships(id),
  UNIQUE(source_operation_id,user_id,mode),
  FOREIGN KEY(scope_id,period_id) REFERENCES periods(scope_id,id)
);
CREATE INDEX ix_credit_available ON redraw_credits(scope_id,period_id,mode,user_id,state,issued_at);
CREATE TABLE gift_invites (
  id TEXT PRIMARY KEY,
  scope_id INTEGER NOT NULL,
  period_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('wife','husband','member')),
  relationship_id TEXT NOT NULL REFERENCES relationships(id),
  relationship_version INTEGER NOT NULL,
  sender_id TEXT NOT NULL,
  recipient_id TEXT NOT NULL CHECK(recipient_id<>sender_id),
  state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected','cancelled','expired','invalidated')),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL CHECK(expires_at>created_at),
  finalized_at INTEGER,
  reason TEXT,
  result_relationship_id TEXT REFERENCES relationships(id),
  FOREIGN KEY(scope_id,period_id) REFERENCES periods(scope_id,id)
);
CREATE UNIQUE INDEX uq_pending_gift ON gift_invites(relationship_id) WHERE state='pending';
CREATE INDEX ix_invite_expiry ON gift_invites(state,expires_at);
CREATE INDEX ix_invite_recipient ON gift_invites(scope_id,recipient_id,state);
CREATE TABLE intimacy_edges (
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL CHECK(source_id<>target_id),
  score INTEGER NOT NULL CHECK(score>=0),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(scope_id,source_id,target_id)
);
CREATE INDEX ix_intimacy_rank ON intimacy_edges(scope_id,source_id,score DESC,target_id);
CREATE TABLE activity_seconds (
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  user_id TEXT NOT NULL,
  observed_second INTEGER NOT NULL,
  message_count INTEGER NOT NULL CHECK(message_count>=0),
  PRIMARY KEY(scope_id,user_id,observed_second)
);
CREATE INDEX ix_activity_window ON activity_seconds(scope_id,observed_second,user_id);
CREATE TABLE processed_events (
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  event_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  observed_at INTEGER NOT NULL,
  stats_applied INTEGER NOT NULL DEFAULT 0 CHECK(stats_applied IN (0,1)),
  command_operation_id TEXT,
  PRIMARY KEY(scope_id,event_key)
);
CREATE TABLE cooldowns (
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  user_id TEXT NOT NULL,
  last_command_at INTEGER NOT NULL,
  PRIMARY KEY(scope_id,user_id)
);
CREATE TABLE operation_log (
  id TEXT PRIMARY KEY,
  scope_id INTEGER REFERENCES scopes(id),
  period_id TEXT,
  mode TEXT CHECK(mode IS NULL OR mode IN ('wife','husband','member')),
  kind TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  actor_type TEXT NOT NULL CHECK(actor_type IN ('qq','dashboard','system')),
  result_code TEXT NOT NULL,
  config_revision TEXT NOT NULL,
  data_json TEXT NOT NULL CHECK(json_valid(data_json)),
  reason TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX ix_operation_history ON operation_log(scope_id,created_at DESC);
CREATE TABLE notification_outbox (
  id TEXT PRIMARY KEY,
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  dedupe_key TEXT NOT NULL UNIQUE,
  umo TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','failed','unknown')),
  created_at INTEGER NOT NULL,
  attempted_at INTEGER,
  sent_at INTEGER,
  error_summary TEXT
);
CREATE TABLE maintenance_jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('import','export','restore','backup','reconcile')),
  actor TEXT NOT NULL,
  state TEXT NOT NULL,
  expected_revision TEXT NOT NULL,
  staged_manifest_hash TEXT,
  report_json TEXT NOT NULL CHECK(json_valid(report_json)),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
