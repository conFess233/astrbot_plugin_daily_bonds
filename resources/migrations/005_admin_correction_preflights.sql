CREATE TABLE web_admin_action_preflights (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  action TEXT NOT NULL CHECK(action IN ('relationship_end','credit_grant','intimacy_set','activity_set','counter_reset')),
  target_key TEXT NOT NULL,
  reason TEXT NOT NULL,
  spec_json TEXT NOT NULL CHECK(json_valid(spec_json)),
  before_json TEXT NOT NULL CHECK(json_valid(before_json)),
  after_json TEXT NOT NULL CHECK(json_valid(after_json)),
  config_revision TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  consumed_at INTEGER
);
