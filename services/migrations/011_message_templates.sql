-- 旧版本抽取自动附图；迁移为显式占位符，并保留已有自定义文案。
UPDATE settings SET revision=revision+1, value_json=json_set(value_json,
  '$.messages.results.draw_wife', json_extract(value_json,'$.messages.results.draw_wife') || '{image}')
WHERE json_type(value_json,'$.messages.results.draw_wife')='text'
  AND instr(json_extract(value_json,'$.messages.results.draw_wife'),'{image}')=0;
UPDATE settings SET revision=revision+1, value_json=json_set(value_json,
  '$.messages.results.draw_husband', json_extract(value_json,'$.messages.results.draw_husband') || '{image}')
WHERE json_type(value_json,'$.messages.results.draw_husband')='text'
  AND instr(json_extract(value_json,'$.messages.results.draw_husband'),'{image}')=0;
UPDATE settings SET revision=revision+1, value_json=json_set(value_json,
  '$.messages.results.draw_member', json_extract(value_json,'$.messages.results.draw_member') || '{image}')
WHERE json_type(value_json,'$.messages.results.draw_member')='text'
  AND instr(json_extract(value_json,'$.messages.results.draw_member'),'{image}')=0;
