-- 记录内置清单的首次安装版本；后续版本只补充缺失记录，不覆盖管理员数据。
CREATE TABLE catalog_packs (
  pack_id TEXT PRIMARY KEY,
  pack_version TEXT NOT NULL,
  installed_at INTEGER NOT NULL
);
