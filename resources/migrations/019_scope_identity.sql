-- 作用域按机器人号与群号识别：平台适配器改名后沿用原作用域；重复的旧作用域合并后记下去向。
ALTER TABLE scopes ADD COLUMN merged_into INTEGER REFERENCES scopes(id);
