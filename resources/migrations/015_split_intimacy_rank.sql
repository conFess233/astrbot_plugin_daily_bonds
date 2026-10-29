-- 亲密度排行拆为“亲密度排行”（全群）与“我的亲密度排行”（个人），不再需要范围开关。
-- 已保存的全局与群级配置删除该字段，否则合并默认配置时会被当作未知字段拒绝。
UPDATE settings
SET value_json = json_remove(value_json, '$.display.intimacy_rank_mode')
WHERE json_type(value_json, '$.display.intimacy_rank_mode') IS NOT NULL;
UPDATE settings
SET value_json = json_remove(value_json, '$.display')
WHERE json_type(value_json, '$.display') = 'object'
  AND json_extract(value_json, '$.display') = '{}';
