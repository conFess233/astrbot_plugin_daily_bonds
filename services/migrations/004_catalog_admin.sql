CREATE TABLE web_admin_dedup (
  actor_username TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  request_id TEXT NOT NULL,
  request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
  response_json TEXT NOT NULL CHECK(json_valid(response_json)),
  created_at INTEGER NOT NULL,
  PRIMARY KEY(actor_username, endpoint, request_id)
);

CREATE TABLE web_admin_preflights (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  entity_kind TEXT NOT NULL CHECK(entity_kind IN ('character','pool')),
  entity_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  impact_json TEXT NOT NULL CHECK(json_valid(impact_json)),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  consumed_at INTEGER
);

CREATE TABLE web_admin_audit (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  details_json TEXT NOT NULL CHECK(json_valid(details_json)),
  created_at INTEGER NOT NULL
);
