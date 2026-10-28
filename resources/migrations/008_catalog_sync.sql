CREATE TABLE catalog_sync_baselines (
  entity_kind TEXT NOT NULL CHECK(entity_kind IN ('character','pool')),
  entity_id TEXT NOT NULL,
  source_json TEXT NOT NULL CHECK(json_valid(source_json)),
  source_sha TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(entity_kind,entity_id)
);
