-- AI 好感度评估的每周期加减累计，用于每日上限与递减；旧周期的行由历史清理删除。
CREATE TABLE IF NOT EXISTS sentiment_daily (
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  period_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  gained INTEGER NOT NULL DEFAULT 0 CHECK(gained >= 0),
  lost INTEGER NOT NULL DEFAULT 0 CHECK(lost >= 0),
  PRIMARY KEY (scope_id, period_id, source_id, target_id)
);
