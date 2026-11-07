-- 补抽资格改为直接返还普通抽取次数；redraw_credits 由迁移钩子折算后删除。
-- /设置老婆 与强娶得到的关系单独标记来源；历史记录从操作日志回填。
ALTER TABLE relationships ADD COLUMN special_source TEXT CHECK(special_source IN ('admin','force'));
UPDATE relationships SET special_source='admin' WHERE id IN (
  SELECT json_extract(data_json,'$.relationship_id') FROM operation_log
  WHERE kind='set_wife' AND result_code='DRAWN');
-- 强娶每周期的尝试次数。
ALTER TABLE daily_counters ADD COLUMN force_attempts INTEGER NOT NULL DEFAULT 0 CHECK(force_attempts>=0);
