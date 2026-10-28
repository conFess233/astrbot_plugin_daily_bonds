-- 合格主动戳的活跃事件；旧消息日期可从 activity_seconds 回算，旧戳不补造。
CREATE TABLE activity_pokes (
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  user_id TEXT NOT NULL,
  observed_second INTEGER NOT NULL,
  PRIMARY KEY(scope_id,user_id,observed_second)
);
CREATE INDEX ix_poke_activity_window ON activity_pokes(scope_id,observed_second,user_id);
