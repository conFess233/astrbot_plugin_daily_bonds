// Keep raw edits (including incomplete numbers) until the shared draft is submitted.
export function configAt(config, path) {
  return path.split(".").reduce((value, key) => value?.[key], config);
}

export function setConfigAt(config, path, value) {
  const keys = path.split(".");
  let current = config;
  for (const key of keys.slice(0, -1)) current = current[key] ??= {};
  current[keys.at(-1)] = value;
}

export function hasConfigAt(config, path) {
  let current = config;
  for (const key of path.split(".")) {
    if (!current || !Object.hasOwn(current, key)) return false;
    current = current[key];
  }
  return true;
}

export function configLeaves(config, prefix = "") {
  return Object.entries(config).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return value && !Array.isArray(value) && typeof value === "object"
      ? configLeaves(value, path) : [[path, value]];
  });
}

export function displayValue(path, value) {
  if (Array.isArray(value)) return value.join("\n");
  if (typeof value === "boolean") return value;
  return String(path.endsWith(".steal_probability") ? value * 100 : value);
}

export function draftValues(config) {
  return Object.fromEntries(configLeaves(config).map(([path, value]) => [path, displayValue(path, value)]));
}

export function changedPaths(saved, savedOverride, values, overridePaths, scoped) {
  return configLeaves(saved).filter(([path, value]) =>
    values[path] !== displayValue(path, value) ||
    (scoped && overridePaths.has(path) !== hasConfigAt(savedOverride, path))).map(([path]) => path);
}

export function serializeDraft(saved, global, values, overridePaths, scoped, floatPaths) {
  const draft = structuredClone(saved);
  const override = {};
  for (const [path, original] of configLeaves(saved)) {
    const raw = values[path];
    let value = original;
    if (scoped && !overridePaths.has(path)) value = configAt(global, path);
    else if (raw !== displayValue(path, original)) {
      if (Array.isArray(original)) value = raw.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
      else if (typeof original === "boolean") value = raw;
      else if (typeof original === "number") {
        value = Number(raw);
        if (!raw.trim() || !Number.isFinite(value) ||
            (Number.isInteger(original) && !floatPaths.has(path) && !path.endsWith(".steal_probability") && !Number.isInteger(value))) {
          throw new Error(`${path} 必须填写有效数字。`);
        }
        if (path.endsWith(".steal_probability")) value /= 100;
      } else value = raw;
    }
    setConfigAt(draft, path, structuredClone(value));
    if (scoped && overridePaths.has(path)) setConfigAt(override, path, structuredClone(value));
  }
  return { draft, override };
}

export function templateParts(text, fields, path = "模板") {
  if (Array.from(text).length > 1000) throw new Error(`${path} 必须是最多 1000 字符的文本`);
  if (!text.trim()) return [];
  if (text.includes("\ue000image\ue001")) throw new Error(`${path} 不允许内部图片标记`);
  if (/[\x00-\x09\x0b-\x1f]/.test(text)) throw new Error(`${path} 不允许控制字符`);
  const parts = [];
  const used = new Set();
  let position = 0;
  for (const match of text.matchAll(/{{|}}|{([^{}]*)}|[{}]/g)) {
    if (match.index > position) parts.push({ text: text.slice(position, match.index) });
    if (match[0] === "{{" || match[0] === "}}") parts.push({ text: match[0][0] });
    else if (match[1] !== undefined && fields.allowed.includes(match[1])) {
      used.add(match[1]);
      parts.push({ variable: match[1] });
    } else throw new Error(`${path} 含有未知变量或非法大括号：${match[0]}`);
    position = match.index + match[0].length;
  }
  if (position < text.length) parts.push({ text: text.slice(position) });
  const missing = fields.required.filter((name) => !used.has(name));
  if (missing.length && !(fields.image && used.has("image"))) throw new Error(`${path} 缺少必要变量：${missing.join("、")}`);
  return parts;
}

// 服务端 schema 2 中每条文案是 {text, enabled, quote}；编辑器沿用三类叶子路径：
// messages.<分类>.<键>、reply_enabled.<分类>.<键>、reply_quote.<分类>.<键>。
// 读取时展开，提交前合并，编辑器其余逻辑无需感知结构变化。
const MESSAGE_FLAGS = { enabled: "reply_enabled", quote: "reply_quote" };

export function toUiConfig(config) {
  if (!config || typeof config !== "object") return config;
  const result = structuredClone(config);
  const messages = result.messages;
  if (!messages) return result;
  for (const [section, entries] of Object.entries(messages)) {
    for (const [key, entry] of Object.entries(entries || {})) {
      if (!entry || typeof entry !== "object") continue;
      if (Object.hasOwn(entry, "text")) entries[key] = entry.text;
      else delete entries[key];
      for (const [flag, prefix] of Object.entries(MESSAGE_FLAGS)) {
        if (!Object.hasOwn(entry, flag)) continue;
        result[prefix] ??= {};
        result[prefix][section] ??= {};
        result[prefix][section][key] = entry[flag];
      }
    }
    if (!Object.keys(entries || {}).length) delete messages[section];
  }
  if (!Object.keys(messages).length) delete result.messages;
  return result;
}

export function fromUiConfig(config) {
  if (!config || typeof config !== "object") return config;
  const result = structuredClone(config);
  const flags = Object.values(MESSAGE_FLAGS);
  const touched = result.messages || flags.some((prefix) => result[prefix]);
  if (!touched) return result;
  const messages = result.messages || {};
  const merged = {};
  for (const [section, entries] of Object.entries(messages)) {
    merged[section] = {};
    for (const [key, text] of Object.entries(entries)) {
      merged[section][key] = section === "titles" ? text : { text };
    }
  }
  for (const [flag, prefix] of Object.entries(MESSAGE_FLAGS)) {
    for (const [section, entries] of Object.entries(result[prefix] || {})) {
      merged[section] ??= {};
      for (const [key, value] of Object.entries(entries)) {
        merged[section][key] ??= {};
        merged[section][key][flag] = value;
      }
    }
    delete result[prefix];
  }
  result.messages = merged;
  return result;
}
