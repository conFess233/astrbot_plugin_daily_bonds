-- 历史清理按 observed_at 删除过期的事件去重记录；没有索引时每天都要扫全表。
CREATE INDEX IF NOT EXISTS ix_processed_events_observed ON processed_events(observed_at);
-- 重载不再让待处理赠送失效，对应的通知文案随之删除；已保存的配置中去掉该字段，
-- 否则合并默认配置时会被当作未知字段拒绝。
UPDATE settings
SET value_json = json_remove(value_json, '$.messages.notifications.restart_invalidated')
WHERE json_type(value_json, '$.messages.notifications.restart_invalidated') IS NOT NULL;
UPDATE settings
SET value_json = json_remove(value_json, '$.messages.notifications')
WHERE json_type(value_json, '$.messages.notifications') = 'object'
  AND json_extract(value_json, '$.messages.notifications') = '{}';
UPDATE settings
SET value_json = json_remove(value_json, '$.messages')
WHERE json_type(value_json, '$.messages') = 'object'
  AND json_extract(value_json, '$.messages') = '{}';
-- 亲密度衰减：记录每个群上次衰减的时间，停机多日后按经过的天数一次补齐。
CREATE TABLE IF NOT EXISTS intimacy_decay (
  scope_id INTEGER PRIMARY KEY REFERENCES scopes(id),
  decayed_at INTEGER NOT NULL
);
