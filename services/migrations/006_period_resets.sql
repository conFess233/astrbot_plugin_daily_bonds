CREATE TABLE web_admin_action_preflights_new (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  action TEXT NOT NULL CHECK(action IN ('relationship_end','credit_grant','intimacy_set','activity_set','counter_reset','period_reset_relations','period_reset_counters')),
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
INSERT INTO web_admin_action_preflights_new
  SELECT * FROM web_admin_action_preflights;
DROP TABLE web_admin_action_preflights;
ALTER TABLE web_admin_action_preflights_new RENAME TO web_admin_action_preflights;

-- Add the new alias to installations that still use the previous default.
-- A later deliberate keyword edit is preserved because this migration runs once.
UPDATE settings
SET value_json=json_set(value_json,'$.commands.keywords.divorce_member',json('["踹群友","离婚群友"]')),
    revision=revision+1,updated_at=unixepoch(),updated_by='system:migration-006'
WHERE json_type(value_json,'$.commands.keywords.divorce_member')='array'
  AND json_array_length(value_json,'$.commands.keywords.divorce_member')=1
  AND json_extract(value_json,'$.commands.keywords.divorce_member[0]')='踹群友';

UPDATE settings
SET value_json=json_set(value_json,'$.messages.errors.divorce_ambiguous',
      '找到多段匹配关系，请用“离婚老婆/离婚老公/离婚群友”指定玩法和名称或 ID：'),
    revision=revision+1,updated_at=unixepoch(),updated_by='system:migration-006'
WHERE json_extract(value_json,'$.messages.errors.divorce_ambiguous')=
      '没有找到唯一匹配的关系；如果同名或跨玩法，请用“离婚老婆/离婚老公”并指定 #ID。';

UPDATE settings
SET value_json=json_set(value_json,'$.messages.errors.divorce_relation_missing','没有找到匹配的当前关系。'),
    revision=revision+1,updated_at=unixepoch(),updated_by='system:migration-006'
WHERE json_extract(value_json,'$.messages.errors.divorce_relation_missing')=
      '没有找到唯一匹配的关系；跨玩法或同名对象请使用明确指令和 #ID。';
