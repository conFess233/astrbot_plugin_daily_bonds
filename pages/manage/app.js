const bridge = window.AstrBotPluginPage;
const $ = (selector) => document.querySelector(selector);
let activeRequests = 0;
let progressTimer;

async function withProgress(request) {
  activeRequests += 1;
  if (activeRequests === 1) {
    progressTimer = window.setTimeout(() => { $("#loading-progress").hidden = false; }, 150);
  }
  try {
    return await request();
  } finally {
    activeRequests -= 1;
    if (activeRequests === 0) {
      window.clearTimeout(progressTimer);
      $("#loading-progress").hidden = true;
    }
  }
}

const state = {
  context: null,
  scopes: [],
  scopeId: "global",
  savedConfig: null,
  globalConfig: null,
  savedOverride: {},
  savedSignature: "",
  configGroup: "enabled",
  revision: 0,
  globalRevision: 0,
  busy: false,
  characters: [],
  pools: [],
  characterRevision: 0,
  characterImages: [],
  characterProvenance: {},
  poolRevision: 0,
  dataScopeId: "",
  correctionPlan: null,
  periodResetPlan: null,
  importPlan: null,
  restorePlan: null,
};

function normalizeApiResponse(result) {
  if (result && typeof result === "object" && result.ok === true && Object.hasOwn(result, "data")) {
    return result;
  }
  const normalized = { ok: true, data: result };
  const revision = result?.revision ?? result?.data?.revision;
  if (revision && typeof revision === "object") normalized.revision = revision;
  return normalized;
}

async function apiGet(endpoint, params) {
  return withProgress(async () => normalizeApiResponse(await bridge.apiGet(endpoint, params)));
}

async function apiPost(endpoint, body) {
  return withProgress(async () => normalizeApiResponse(await bridge.apiPost(endpoint, body)));
}

async function apiUpload(endpoint, file) {
  return withProgress(async () => normalizeApiResponse(await bridge.upload(endpoint, file)));
}

const configFields = $("#config-fields");
const configTabs = $("#config-tabs");
const scopeSelect = $("#scope-select");
const notice = $("#notice");
const imagePreviewCache = new Map();

function showNotice(message, kind = "success") {
  const item = document.createElement("div");
  item.className = `notification ${kind}`;
  item.setAttribute("role", kind === "error" ? "alert" : "status");
  const body = document.createElement("span");
  body.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "notification-close";
  close.setAttribute("aria-label", "关闭通知");
  close.textContent = "×";
  close.addEventListener("click", () => item.remove());
  item.append(body, close);
  notice.prepend(item);
  if (kind !== "error") window.setTimeout(() => item.remove(), 4000);
}

function setBusy(value) {
  state.busy = value;
  for (const button of [$("#refresh"), $("#discard"), $("#preview"), $("#save"), scopeSelect]) {
    button.disabled = value || (button.id === "discard" && !isConfigDirty());
  }
}

function updateDirtyState() {
  const dirty = isConfigDirty();
  $("#dirty-indicator").textContent = dirty ? "有未保存草稿" : "已保存";
  $("#dirty-indicator").classList.toggle("dirty", dirty);
  $("#discard").disabled = state.busy || !dirty;
}

function setScopeLabel() {
  const scope = state.scopes.find((item) => String(item.id) === state.scopeId);
  $("#editor-title").textContent = scope
    ? `群 ${scope.group_id} 的生效配置草稿`
    : "全局配置";
}

async function loadScopes() {
  const result = await apiGet("scopes");
  state.scopes = result.data ?? [];
  const selected = state.scopeId;
  scopeSelect.replaceChildren(new Option("全局", "global"));
  for (const scope of state.scopes) {
    const option = new Option(`群 ${scope.group_id} · 机器人 ${scope.self_id}`, String(scope.id));
    scopeSelect.add(option);
  }
  if ([...scopeSelect.options].some((option) => option.value === selected)) {
    scopeSelect.value = selected;
  } else {
    state.scopeId = "global";
    scopeSelect.value = "global";
  }
  renderScopes();
}

function renderScopes() {
  const list = $("#scope-list");
  list.replaceChildren();
  const dataScope = $("#data-scope");
  const previousDataScope = state.dataScopeId;
  dataScope.replaceChildren();
  state.periodResetPlan = null;
  if ($("#period-reset-dialog").open) $("#period-reset-dialog").close();
  if (!state.scopes.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "尚无记录群；有群消息到达后会自动建立作用域。";
    list.append(empty);
    return;
  }
  for (const scope of state.scopes) {
    const dataOption = new Option(`群 ${scope.group_id} · 机器人 ${scope.self_id}`, String(scope.id));
    dataScope.add(dataOption);
    const item = document.createElement("div");
    item.className = "scope-item";
    const label = document.createElement("span");
    label.className = "scope-label";
    label.textContent = `群 ${scope.group_id}`;
    const detail = document.createElement("span");
    detail.className = "scope-detail";
    detail.textContent = `${scope.platform_id} · 机器人 ${scope.self_id}`;
    item.append(label, detail);
    list.append(item);
  }
  if ([...dataScope.options].some((option) => option.value === previousDataScope)) dataScope.value = previousDataScope;
  else if (dataScope.options.length) dataScope.value = dataScope.options[0].value;
  state.dataScopeId = dataScope.value;
}

const CONFIG_GROUPS = {
  schema_version: "配置版本", enabled: "总开关", access: "群与名单", reset: "每日重置",
  commands: "指令与触发", "modes.wife": "今日老婆", "modes.husband": "今日老公",
  "modes.member": "娶群友", statistics: "统计", weights: "抽取权重",
  display: "展示与排行", members: "群成员缓存", history: "历史记录", resources: "资源限制",
  "messages.results": "群内操作结果", "messages.errors": "群内错误提示",
  "messages.notifications": "赠送与重启通知",
};
const MESSAGE_LABELS = {
  draw_wife: "抽到老婆", draw_husband: "抽到老公", draw_member: "抽到群友",
  capacity_full: "持有名额已满", no_draw_quota: "普通抽取额度用完", no_candidate: "没有候选对象",
  steal_failed: "抢夺失败", stolen: "抢夺成功", divorced: "解除关系成功",
  gifted: "赠送成功", invite_created: "赠送邀请已创建", gift_accepted: "赠送已接受",
  invite_rejected: "赠送已拒绝", invite_cancelled: "赠送已取消",
  invite_expired: "赠送邀请过期", invite_invalidated: "赠送邀请失效",
  invite_not_owned: "无权处理邀请", steal_cooldown: "抢夺冷却", steal_limit: "抢夺次数用完",
  target_protected: "目标受保护", divorce_limit: "解除关系次数用完",
  relationship_not_found: "关系不存在", invalid_target: "目标无效",
  no_target_relationship: "目标没有关系", mode_disabled: "玩法已关闭",
  invalid_probability: "抢夺概率无效", invalid_timeout: "赠送超时无效",
  invite_already_pending: "已有待处理邀请", invite_limit: "待处理邀请达到上限",
  invite_invalid: "邀请无效", unknown_result: "其他操作结果",
  syntax_error: "指令格式错误", operation_error: "操作错误", mode_closed: "玩法关闭",
  command_cooldown: "通用指令冷却", steal_disabled: "抢夺关闭",
  steal_target_required: "抢夺目标无效", steal_relation_ambiguous: "抢夺关系不明确",
  gift_disabled: "赠送关闭", gift_target_required: "赠送目标无效",
  gift_relation_ambiguous: "赠送关系不明确", divorce_ambiguous: "离婚关系不明确",
  divorce_disabled: "解除关系关闭", divorce_relation_missing: "解除关系未找到",
  invite_multiple: "有多条待处理邀请", invite_none: "没有待处理邀请",
  invite_invalid_id: "邀请编号无效", rank_intimacy_disabled: "亲密度排行关闭",
  rank_activity_disabled: "活跃度排行关闭", activity_disabled: "活跃度统计关闭",
  page_out_of_range: "页码超出范围", list_empty: "关系列表为空",
  restart_invalidated: "重启后邀请失效",
};
const MESSAGE_VARIABLES = {
  draw_wife: "name", draw_husband: "name", draw_member: "name",
  capacity_full: "names", invite_created: "invite_id、accept_keyword",
  invite_already_pending: "invite_id", invite_limit: "limit",
  unknown_result: "code", syntax_error: "detail", operation_error: "detail",
  command_cooldown: "remaining_seconds",
  page_out_of_range: "pages", list_empty: "title",
  restart_invalidated: "amount", invite_expired: "invite_ids",
};
const MESSAGE_REQUIRED = new Set([
  "draw_wife", "draw_husband", "draw_member", "capacity_full",
  "invite_created", "page_out_of_range", "invite_expired",
]);
const CONFIG_LABELS = {
  schema_version: "配置格式版本", enabled: "启用", mode: "名单模式", ids: "名单 ID",
  extra_bot_ids: "补充机器人 QQ 号", timezone: "时区", time: "重置时间",
  allow_bare: "允许裸关键词", allow_leading_bot_mention: "允许开头 @机器人",
  allow_host_prefix: "允许宿主命令前缀", extra_prefixes: "额外命令前缀",
  cooldown_seconds: "通用冷却（秒）", capacity: "每日容量", steal_enabled: "允许抢夺",
  steal_attempt_limit: "抢夺尝试次数上限", steal_cooldown_seconds: "抢夺冷却（秒）",
  steal_probability: "抢夺成功率（%）", stolen_limit: "被抢次数上限",
  gift_enabled: "允许赠送", gift_mode: "赠送方式", gift_timeout_seconds: "赠送确认超时（秒）",
  divorce_enabled: "允许离婚或踹群友", divorce_limit: "离婚或踹群友次数上限",
  pool_ids: "启用的角色池 ID", intimacy_enabled: "记录亲密度", activity_enabled: "记录活跃度",
  mention_active_points: "主动 @ 加分", mention_passive_points: "被 @ 加分",
  poke_active_points: "主动戳一戳加分", poke_passive_points: "被戳一戳加分",
  activity_window_days: "活跃度窗口（天）", base: "基础权重",
  per_intimacy_point: "每点亲密度增加权重", maximum: "最大权重",
  activity_floor: "零活跃权重系数", activity_full_messages: "满活跃消息数",
  pagination_enabled: "启用分页", page_size: "每页条数",
  intimacy_rank_mode: "亲密度排行范围", intimacy_rank_enabled: "显示亲密度排行",
  activity_rank_enabled: "显示活跃度排行", cache_ttl_seconds: "缓存时间（秒）",
  retention_days: "历史保留天数", http_timeout_seconds: "HTTP 超时（秒）",
  image_max_bytes: "单图大小上限（字节）", image_max_pixels: "单图像素上限",
  import_max_bytes: "导入包大小上限（字节）", import_max_entries: "导入条目上限",
  import_max_expanded_bytes: "导入解压上限（字节）", render_width: "渲染宽度（像素）",
  render_max_height: "单图最大高度（像素）", render_concurrency: "并发渲染数",
  text_chunk_chars: "文字分片字符数", send_interval_seconds: "分片发送间隔（秒）",
  pending_invites_per_user: "每人待处理邀请上限",
  avatar_cache_ttl_seconds: "头像缓存时间（秒）", avatar_cache_max_entries: "头像缓存人数上限",
  dedupe_retention_days: "事件去重保留天数", backup_keep_count: "备份保留数量",
  download_concurrency: "并发下载数",
  draw_wife: "抽老婆", draw_husband: "抽老公", draw_member: "娶群友",
  steal_wife: "抢老婆", steal_husband: "抢老公", steal_member: "抢群友",
  gift_wife: "送老婆", gift_husband: "送老公", gift_member: "送群友",
  divorce_character: "离婚", divorce_wife: "离婚老婆", divorce_husband: "离婚老公",
  divorce_member: "踹群友", list_characters: "老婆列表", list_members: "群友列表",
  rank_intimacy: "亲密度排行", rank_activity: "活跃度排行",
  gift_accept: "接受赠送", gift_reject: "拒绝赠送", gift_cancel: "取消赠送",
};
const CONFIG_OPTIONS = {
  "access.groups.mode": [["unrestricted", "不限制"], ["blacklist", "黑名单"], ["whitelist", "白名单"]],
  "access.users.mode": [["unrestricted", "不限制"], ["blacklist", "黑名单"], ["whitelist", "白名单"]],
  "display.intimacy_rank_mode": [["personal", "个人"], ["group_directed", "全群有向"]],
};
const FLOAT_CONFIG_PATHS = new Set([
  "weights.base", "weights.per_intimacy_point", "weights.maximum",
  "weights.activity_floor", "resources.send_interval_seconds",
]);
const CONFIG_HINTS = {
  "access.groups.ids": "每行一个群身份 ID。",
  "access.users.ids": "每行一个 QQ 数字 ID；空白名单会拒绝全部用户。",
  "access.extra_bot_ids": "每行一个机器人 QQ 数字 ID。",
  "commands.extra_prefixes": "每行一个字面前缀。",
  "modes.wife.pool_ids": "每行一个角色池 ID；空列表表示无候选池。",
  "modes.husband.pool_ids": "每行一个角色池 ID；空列表表示无候选池。",
  "modes.wife.steal_attempt_limit": "0 表示不限次数。",
  "modes.husband.steal_attempt_limit": "0 表示不限次数。",
  "modes.member.steal_attempt_limit": "0 表示不限次数。",
  "modes.wife.stolen_limit": "0 表示不限次数。",
  "modes.husband.stolen_limit": "0 表示不限次数。",
  "modes.member.stolen_limit": "0 表示不限次数。",
  "modes.wife.divorce_limit": "0 表示不限次数。",
  "modes.husband.divorce_limit": "0 表示不限次数。",
  "modes.member.divorce_limit": "0 表示不限次数。",
  "reset.time": "使用 HH:mm 格式；旧周期结束后生效。",
};

function configAt(config, path) {
  return path.split(".").reduce((value, key) => value?.[key], config);
}

function setConfigAt(config, path, value) {
  const keys = path.split(".");
  let current = config;
  for (const key of keys.slice(0, -1)) current = current[key] ??= {};
  current[keys.at(-1)] = value;
}

function hasConfigAt(config, path) {
  let current = config;
  for (const key of path.split(".")) {
    if (!current || !Object.hasOwn(current, key)) return false;
    current = current[key];
  }
  return true;
}

function configLeaves(config, prefix = "") {
  return Object.entries(config).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return value && !Array.isArray(value) && typeof value === "object"
      ? configLeaves(value, path) : [[path, value]];
  });
}

function configGroup(path) {
  if (path === "schema_version") return "enabled";
  const parts = path.split(".");
  if (parts[0] === "messages") return parts.slice(0, 2).join(".");
  return parts[0] === "modes" ? parts.slice(0, 2).join(".") : parts[0];
}

function configIsGlobalOnly(path) {
  return path === "schema_version" || path.startsWith("access.groups.") ||
    path.startsWith("resources.") || path.startsWith("history.");
}

function configLabel(path) {
  const parts = path.split(".");
  if (parts[0] === "messages") return MESSAGE_LABELS[parts.at(-1)] || parts.at(-1);
  const name = CONFIG_LABELS[parts.at(-1)] || parts.at(-1);
  if (parts[0] === "commands" && parts[1] === "keywords") return `${name}关键词`;
  if (path === "access.groups.mode") return "群准入模式";
  if (path === "access.users.mode") return "用户准入模式";
  if (path === "access.groups.ids") return "群身份名单";
  if (path === "access.users.ids") return "用户 QQ 名单";
  if (path.endsWith(".enabled") && parts[0] === "modes") return "启用玩法";
  return name;
}

function renderConfigFields() {
  configFields.replaceChildren();
  configTabs.replaceChildren();
  if (!state.savedConfig) return;
  const groups = new Map();
  for (const [path, value] of configLeaves(state.savedConfig)) {
    const group = configGroup(path);
    if (!groups.has(group)) {
      const section = document.createElement("section");
      section.className = "config-group";
      section.id = `config-group-${group.replaceAll(".", "-")}`;
      section.setAttribute("role", "tabpanel");
      const heading = document.createElement("h4");
      heading.textContent = CONFIG_GROUPS[group] || group;
      const fields = document.createElement("div");
      fields.className = "config-grid";
      section.append(heading, fields);
      configFields.append(section);
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = "config-tab";
      tab.setAttribute("role", "tab");
      tab.dataset.configGroup = group;
      tab.id = `config-tab-${group.replaceAll(".", "-")}`;
      tab.setAttribute("aria-controls", section.id);
      tab.textContent = CONFIG_GROUPS[group] || group;
      section.setAttribute("aria-labelledby", tab.id);
      configTabs.append(tab);
      groups.set(group, { section, fields, tab });
    }
    const card = document.createElement("div");
    card.className = "config-field";
    const label = document.createElement("label");
    label.className = "field";
    const title = document.createElement("span");
    title.textContent = configLabel(path);
    const globalOnly = configIsGlobalOnly(path);
    const overridden = state.scopeId !== "global" && hasConfigAt(state.savedOverride, path);
    let input;
    if (Array.isArray(value)) {
      input = document.createElement("textarea");
      input.rows = Math.min(Math.max(value.length + 1, 2), 5);
      input.value = value.join("\n");
      input.placeholder = "每行一项";
    } else if (path.startsWith("messages.")) {
      input = document.createElement("textarea");
      input.rows = 3;
      input.maxLength = 500;
      input.value = value;
    } else if (typeof value === "boolean") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = value;
    } else if (CONFIG_OPTIONS[path] || path.endsWith(".gift_mode")) {
      input = document.createElement("select");
      const choices = CONFIG_OPTIONS[path] || [["direct", "直接赠送"], ["confirm", "等待确认"]];
      for (const [optionValue, text] of choices) input.add(new Option(text, optionValue));
      input.value = value;
    } else {
      input = document.createElement("input");
      input.type = typeof value === "number" ? "number" : path === "reset.time" ? "time" : "text";
      input.value = path.endsWith(".steal_probability") ? String(value * 100) : String(value);
      if (input.type === "number") input.step = Number.isInteger(value) && !FLOAT_CONFIG_PATHS.has(path) && !path.endsWith(".steal_probability") ? "1" : "any";
      if (path.endsWith(".steal_probability")) { input.min = "0"; input.max = "100"; }
    }
    input.dataset.configPath = path;
    input.setAttribute("aria-label", configLabel(path));
    label.append(title, input);
    card.append(label);
    if (state.scopeId !== "global" && !globalOnly) {
      const override = document.createElement("label");
      override.className = "config-override";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.dataset.overridePath = path;
      toggle.checked = overridden;
      input.disabled = !overridden;
      toggle.addEventListener("change", () => {
        input.disabled = !toggle.checked;
        if (!toggle.checked) {
          const globalValue = configAt(state.globalConfig, path);
          if (Array.isArray(globalValue)) input.value = globalValue.join("\n");
          else if (input.type === "checkbox") input.checked = globalValue;
          else input.value = path.endsWith(".steal_probability") ? String(globalValue * 100) : String(globalValue);
        }
        card.classList.toggle("inherited", !toggle.checked);
        updateDirtyState();
      });
      override.append(toggle, document.createTextNode("本群覆盖（关闭则继承全局）"));
      card.append(override);
      card.classList.toggle("inherited", !overridden);
    } else if (state.scopeId !== "global" || path === "schema_version") {
      input.disabled = true;
      const badge = document.createElement("small");
      badge.className = "config-note";
      badge.textContent = path === "schema_version" ? "只读" : "仅全局设置";
      card.append(badge);
    }
    const variableNames = MESSAGE_VARIABLES[path.split(".").at(-1)];
    const hint = CONFIG_HINTS[path] ||
      (path.startsWith("messages.") ? `最多 500 字；${MESSAGE_REQUIRED.has(path.split(".").at(-1)) ? "必填变量" : "可用变量"}：${variableNames || "无"}。使用 {{ 和 }} 表示大括号。` :
      path.startsWith("commands.keywords.") ? "每行一个别名，至少一项。" : "");
    if (hint) {
      const help = document.createElement("small");
      help.className = "config-note";
      help.textContent = hint;
      card.append(help);
    }
    const error = document.createElement("small");
    error.className = "config-error";
    error.hidden = true;
    card.append(error);
    groups.get(group).fields.append(card);
  }
  if (!groups.has(state.configGroup)) state.configGroup = groups.has("enabled") ? "enabled" : groups.keys().next().value;
  selectConfigGroup(state.configGroup);
}

function selectConfigGroup(group) {
  state.configGroup = group;
  for (const tab of configTabs.querySelectorAll("[data-config-group]")) {
    const selected = tab.dataset.configGroup === group;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  for (const section of configFields.querySelectorAll(".config-group")) {
    section.hidden = section.id !== `config-group-${group.replaceAll(".", "-")}`;
  }
}

function readConfigInput(input) {
  const path = input.dataset.configPath;
  const original = configAt(state.savedConfig, path);
  if (Array.isArray(original)) return input.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  if (typeof original === "boolean") return input.checked;
  if (typeof original === "number") {
    const raw = input.value.trim();
    const value = Number(raw);
    if (!raw || !Number.isFinite(value) ||
        (Number.isInteger(original) && !FLOAT_CONFIG_PATHS.has(path) && !path.endsWith(".steal_probability") && !Number.isInteger(value))) {
      throw new Error(`${path} 必须填写有效数字。`);
    }
    return path.endsWith(".steal_probability") ? value / 100 : value;
  }
  return input.value;
}

function readOverride() {
  const override = {};
  for (const toggle of configFields.querySelectorAll("[data-override-path]:checked")) {
    const path = toggle.dataset.overridePath;
    const input = [...configFields.querySelectorAll("[data-config-path]")].find((item) => item.dataset.configPath === path);
    setConfigAt(override, path, readConfigInput(input));
  }
  return override;
}

function configSignature() {
  return JSON.stringify([...configFields.querySelectorAll("[data-config-path], [data-override-path]")]
    .map((input) => [input.dataset.configPath || input.dataset.overridePath, input.type === "checkbox" ? input.checked : input.value]));
}

function isConfigDirty() {
  return !!state.savedConfig && configSignature() !== state.savedSignature;
}

function showConfigError(message) {
  for (const error of configFields.querySelectorAll(".config-error")) error.hidden = true;
  const paths = [...configFields.querySelectorAll("[data-config-path]")]
    .map((input) => input.dataset.configPath).sort((left, right) => right.length - left.length);
  const path = paths.find((candidate) => message.includes(candidate));
  if (!path) return;
  const input = [...configFields.querySelectorAll("[data-config-path]")]
    .find((item) => item.dataset.configPath === path);
  const error = input.closest(".config-field").querySelector(".config-error");
  error.textContent = message;
  error.hidden = false;
  selectConfigGroup(configGroup(path));
  input.focus();
}

async function describeConfigConflict() {
  const params = state.scopeId === "global" ? {} : { scope_id: state.scopeId };
  const latest = await apiGet("config", params);
  const current = state.scopeId === "global" ? latest.data.global : latest.data.effective;
  const changed = configLeaves(current).filter(([path, value]) =>
    JSON.stringify(value) !== JSON.stringify(configAt(state.savedConfig, path))).map(([path]) => configLabel(path));
  const detail = changed.length ? `其他窗口已修改：${changed.slice(0, 12).join("、")}${changed.length > 12 ? "等" : ""}。` : "版本已变化。";
  return `${detail}当前草稿仍保留，请核对后再刷新。`;
}

async function loadOverview() {
  const grid = $("#overview-cards");
  if (!grid.children.length) {
    for (let index = 0; index < 8; index += 1) {
      const skeleton = document.createElement("div");
      skeleton.className = "metric metric-skeleton";
      skeleton.setAttribute("aria-hidden", "true");
      skeleton.innerHTML = '<span class="skeleton"></span><span class="skeleton"></span>';
      grid.append(skeleton);
    }
  }
  const scopeId = $("#data-scope").value;
  const result = await apiGet("overview", scopeId ? { scope_id: scopeId } : {});
  const metrics = [
    ["机器人与群作用域", result.data.scopes],
    ["当前有效关系", result.data.active_relationships],
    ["待处理赠送邀请", result.data.pending_invites],
    ["当前周期", result.data.current_periods],
    ["有效角色池", result.data.enabled_pool_count ?? "—"],
    ["待处理故障", (result.data.failed_notifications || 0) + (result.data.failed_jobs || 0)],
    ["配置版本", result.data.scope_revision ?? result.data.global_revision],
    ["已录入角色", result.data.catalog.characters],
  ];
  grid.replaceChildren();
  for (const [labelText, value] of metrics) {
    const card = document.createElement("article");
    card.className = "metric";
    const label = document.createElement("p");
    label.className = "metric-label";
    label.textContent = labelText;
    const number = document.createElement("p");
    number.className = "metric-value";
    number.textContent = String(value);
    card.append(label, number);
    grid.append(card);
  }
  const period = result.data.current_period;
  $("#period-summary").textContent = period
    ? `当前周期 ${period.sequence} · ${new Date(period.starts_at * 1000).toLocaleString()} – ${new Date(period.ends_at * 1000).toLocaleString()} · ${period.timezone} ${period.reset_time}`
    : "当前未选择群，或该群还没有已创建的周期。";
}

async function loadConfig() {
  const params = state.scopeId === "global" ? {} : { scope_id: state.scopeId };
  const result = await apiGet("config", params);
  state.globalRevision = result.revision.global;
  state.revision = state.scopeId === "global" ? result.revision.global : result.revision.scope;
  const shown = state.scopeId === "global" ? result.data.global : result.data.effective;
  state.savedConfig = structuredClone(shown);
  state.globalConfig = structuredClone(result.data.global);
  state.savedOverride = structuredClone(result.data.override || {});
  renderConfigFields();
  state.savedSignature = configSignature();
  $("#revision").textContent = `全局版本 ${state.globalRevision} · 当前范围版本 ${state.revision}`;
  setScopeLabel();
  updateDirtyState();
}

async function refreshAll() {
  setBusy(true);
  notice.replaceChildren();
  try {
    await loadScopes();
    await loadOverview();
    await loadConfig();
    showNotice("数据已刷新。", "success");
  } catch (error) {
    showNotice(error.message || "读取管理数据失败。", "error");
  } finally {
    setBusy(false);
    updateDirtyState();
  }
}

function readDraft() {
  const draft = structuredClone(state.savedConfig);
  for (const input of configFields.querySelectorAll("[data-config-path]")) {
    const path = input.dataset.configPath;
    const inherited = state.scopeId !== "global" && !configFields.querySelector(`[data-override-path="${path}"]`)?.checked;
    const value = inherited ? configAt(state.globalConfig, path) : readConfigInput(input);
    setConfigAt(draft, path, value);
  }
  return draft;
}

async function previewDraft() {
  setBusy(true);
  try {
    const result = await apiPost("config/preview", {
      scope_id: state.scopeId,
      draft: readDraft(),
      ...(state.scopeId === "global" ? {} : { override: readOverride() }),
    });
    const groups = result.data.affected_scopes.length;
    const message = state.scopeId === "global"
      ? `配置校验通过。全局变更将影响 ${groups} 个已记录群。`
      : "配置校验通过。这份草稿只作用于所选群。";
    showNotice(message, "success");
  } catch (error) {
    const message = error.message || "配置校验失败。";
    showConfigError(message);
    showNotice(message, "error");
  } finally {
    setBusy(false);
    updateDirtyState();
  }
}

async function saveDraft() {
  setBusy(true);
  try {
    const result = await apiPost("config/save", {
      scope_id: state.scopeId,
      draft: readDraft(),
      ...(state.scopeId === "global" ? {} : { override: readOverride() }),
      expected_revision: state.revision,
      request_id: crypto.randomUUID(),
    });
    state.revision = result.revision.scope;
    state.globalRevision = result.revision.global;
    await loadConfig();
    showNotice("配置已保存。后续群事件将读取新配置。", "success");
  } catch (error) {
    let message = error.message || "保存失败。草稿仍保留，请刷新版本后检查差异。";
    if (message.includes("版本冲突")) {
      try { message = `${message} ${await describeConfigConflict()}`; } catch { /* Keep the original error. */ }
    }
    showConfigError(message);
    showNotice(message, "error");
  } finally {
    setBusy(false);
    updateDirtyState();
  }
}

$(".tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-tab]");
  if (!tab) return;
  for (const button of document.querySelectorAll(".tab")) button.classList.toggle("active", button === tab);
  $("#overview-panel").hidden = tab.dataset.tab !== "overview";
  $("#settings-panel").hidden = tab.dataset.tab !== "settings";
  $("#catalog-panel").hidden = tab.dataset.tab !== "catalog";
  $("#data-panel").hidden = tab.dataset.tab !== "data";
  if (tab.dataset.tab === "catalog") loadCatalog().catch((error) => showNotice(error.message, "error"));
});
configTabs.addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-config-group]");
  if (tab) selectConfigGroup(tab.dataset.configGroup);
});
configTabs.addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...configTabs.querySelectorAll("button[data-config-group]")];
  const current = tabs.findIndex((tab) => tab.dataset.configGroup === state.configGroup);
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
    (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  if (!tabs[next]) return;
  event.preventDefault();
  selectConfigGroup(tabs[next].dataset.configGroup);
  tabs[next].focus();
});
$("#refresh").addEventListener("click", () => {
  if (isConfigDirty() && !window.confirm("当前配置草稿尚未保存。刷新并放弃草稿吗？")) return;
  refreshAll();
});
$("#preview").addEventListener("click", previewDraft);
$("#save").addEventListener("click", saveDraft);
$("#discard").addEventListener("click", () => {
  renderConfigFields();
  updateDirtyState();
  showNotice("草稿已放弃。", "success");
});
configFields.addEventListener("input", (event) => {
  const error = event.target.closest(".config-field")?.querySelector(".config-error");
  if (error) error.hidden = true;
  updateDirtyState();
});
configFields.addEventListener("change", updateDirtyState);
scopeSelect.addEventListener("change", async () => {
  if (isConfigDirty() && !window.confirm("当前草稿尚未保存。切换范围并放弃草稿吗？")) {
    scopeSelect.value = state.scopeId;
    return;
  }
  state.scopeId = scopeSelect.value;
  await loadConfig();
});
window.addEventListener("beforeunload", (event) => {
  if (isConfigDirty()) {
    event.preventDefault();
    event.returnValue = "";
  }
});

state.context = await bridge.ready();
bridge.onContext((context) => {
  state.context = context;
  document.title = bridge.t("pages.manage.title", "今日姻缘 · 管理");
});
await refreshAll();

function renderCatalog() {
  const characters = $("#character-list");
  characters.replaceChildren();
  for (const item of state.characters) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "catalog-item";
    button.textContent = `${item.name} · ${item.id}${item.enabled ? "" : "（停用）"}`;
    button.addEventListener("click", () => loadCharacter(item.id).catch((error) => showNotice(error.message, "error")));
    characters.append(button);
  }
  const pools = $("#pool-list");
  pools.replaceChildren();
  const exportPools = $("#export-pools");
  exportPools.replaceChildren();
  for (const pool of state.pools) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "catalog-item";
    button.textContent = `${pool.name} · ${pool.mode} · ${pool.character_count}`;
    button.addEventListener("click", () => editPool(pool));
    pools.append(button);
    exportPools.add(new Option(`${pool.name} · ${pool.mode} · ${pool.character_count}`, pool.id));
  }
  const poolChecks = $("#character-pools");
  poolChecks.replaceChildren();
  for (const pool of state.pools) {
    const label = document.createElement("label");
    label.className = "check-field";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = pool.id;
    input.dataset.mode = pool.mode;
    const text = document.createTextNode(`${pool.name} (${pool.mode})`);
    label.append(input, text);
    poolChecks.append(label);
  }
  renderCharacterImages();
}

async function loadCatalog() {
  const query = $("#character-search").value.trim();
  const [characters, pools] = await Promise.all([
    apiGet("characters", { q: query, limit: 100 }),
    apiGet("pools"),
  ]);
  state.characters = characters.data.items;
  state.pools = pools.data;
  renderCatalog();
}

async function loadCharacter(id) {
  const response = await apiGet("character", { id });
  const item = response.data;
  $("#character-form-title").textContent = `编辑角色 · ${item.name}`;
  $("#character-id").value = item.id;
  $("#character-id").readOnly = true;
  $("#character-name").value = item.name;
  $("#character-gender").value = item.gender;
  $("#character-aliases").value = item.aliases.join(", ");
  $("#character-enabled").checked = item.enabled;
  state.characterRevision = item.revision;
  state.characterImages = item.images.map((image) => image.media_hash);
  state.characterProvenance = item.provenance || {};
  $("#delete-character").disabled = false;
  for (const check of $("#character-pools").querySelectorAll("input")) {
    check.checked = item.pools.some((pool) => pool.id === check.value);
  }
  renderCharacterImages();
}

function newCharacter() {
  $("#character-form-title").textContent = "新建角色";
  $("#character-id").readOnly = false;
  $("#character-id").value = "";
  $("#character-name").value = "";
  $("#character-gender").value = "female";
  $("#character-aliases").value = "";
  $("#character-enabled").checked = true;
  state.characterRevision = 0;
  state.characterImages = [];
  state.characterProvenance = {};
  $("#delete-character").disabled = true;
  for (const check of $("#character-pools").querySelectorAll("input")) check.checked = false;
  renderCharacterImages();
}

function renderCharacterImages() {
  const host = $("#character-images");
  host.replaceChildren();
  if (!state.characterImages.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "尚无角色图。上传后可在这里预览。";
    host.append(empty);
  }
  for (const hash of state.characterImages) {
    const card = document.createElement("div");
    card.className = "image-card";
    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "image-thumb";
    preview.disabled = true;
    preview.textContent = "正在读取图片…";
    const caption = document.createElement("small");
    caption.textContent = `${hash.slice(0, 12)}…`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "移除";
    remove.addEventListener("click", () => {
      state.characterImages = state.characterImages.filter((value) => value !== hash);
      renderCharacterImages();
    });
    card.append(preview, caption, remove);
    host.append(card);
    if (!imagePreviewCache.has(hash)) {
      imagePreviewCache.set(hash, apiGet(`media/preview/${encodeURIComponent(hash)}`).then((response) => response.data.src));
    }
    imagePreviewCache.get(hash).then((src) => {
      if (!card.isConnected) return;
      const image = document.createElement("img");
      image.src = src;
      image.alt = `角色图 ${hash.slice(0, 12)}`;
      image.addEventListener("error", () => {
        preview.replaceChildren(document.createTextNode("图片无法显示"));
        preview.disabled = true;
      });
      preview.replaceChildren(image);
      preview.disabled = false;
      preview.addEventListener("click", () => {
        $("#image-preview-content").src = src;
        $("#image-preview-content").alt = image.alt;
        $("#image-preview-caption").textContent = `${$("#character-name").value || "角色"} · ${hash}`;
        $("#image-preview").showModal();
      });
    }).catch(() => {
      imagePreviewCache.delete(hash);
      if (card.isConnected) preview.textContent = "图片读取失败";
    });
  }
}

$("#image-preview-close").addEventListener("click", () => $("#image-preview").close());
$("#image-preview").addEventListener("close", () => {
  $("#image-preview-content").removeAttribute("src");
});

$("#character-image").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const result = await apiUpload("media/upload", file);
    const item = result.data || result;
    if (!state.characterImages.includes(item.hash)) state.characterImages.push(item.hash);
    renderCharacterImages();
    showNotice(`图片已验证并上传：${item.width} × ${item.height}`, "success");
  } catch (error) {
    showNotice(error.message || "图片上传失败。", "error");
  } finally {
    event.target.value = "";
  }
});

$("#save-character").addEventListener("click", async () => {
  const poolIds = [...$("#character-pools").querySelectorAll("input:checked")].map((item) => item.value);
  try {
    const result = await apiPost("characters/save", {
      id: $("#character-id").value.trim(), name: $("#character-name").value.trim(),
      gender: $("#character-gender").value, enabled: $("#character-enabled").checked,
      aliases: $("#character-aliases").value.split(",").map((value) => value.trim()).filter(Boolean),
      pool_ids: poolIds, image_hashes: state.characterImages, provenance: state.characterProvenance,
      expected_revision: state.characterRevision, request_id: crypto.randomUUID(),
    });
    showNotice(`角色已保存，版本 ${result.data.revision}`, "success");
    await loadCatalog();
    await loadCharacter(result.data.id);
  } catch (error) { showNotice(error.message || "角色保存失败。", "error"); }
});

async function removeCatalogItem(kind, id, endpoint) {
  const preview = await apiPost(`${endpoint}/delete-preview`, { id });
  const impact = preview.data.impact;
  if (!window.confirm(`将软删除 ${id}。\n影响：${JSON.stringify(impact)}\n继续吗？`)) return;
  const result = await apiPost(`${endpoint}/delete`, {
    entity_kind: kind, entity_id: id, preflight_id: preview.data.preflight_id,
    reason: "AstrBot 管理页面删除", request_id: crypto.randomUUID(),
  });
  showNotice(`已软删除 ${id}`, "success");
  if (kind === "character") newCharacter(); else newPool();
  await loadCatalog();
}

$("#delete-character").addEventListener("click", async () => {
  try { await removeCatalogItem("character", $("#character-id").value, "characters"); }
  catch (error) { showNotice(error.message || "删除失败。", "error"); }
});

function newPool() {
  $("#pool-id").readOnly = false;
  $("#pool-id").value = "";
  $("#pool-name").value = "";
  $("#pool-mode").value = "wife";
  state.poolRevision = 0;
  $("#pool-revision").textContent = "新建的角色池默认不启用；需在配置管理中选择。";
  $("#delete-pool").disabled = true;
}

function editPool(pool) {
  $("#pool-id").value = pool.id;
  $("#pool-id").readOnly = true;
  $("#pool-name").value = pool.name;
  $("#pool-mode").value = pool.mode;
  state.poolRevision = pool.revision;
  $("#pool-revision").textContent = `版本 ${pool.revision} · ${pool.character_count} 个角色`;
  $("#delete-pool").disabled = false;
  state.editingPoolCharacters = pool.character_ids ? pool.character_ids.split(String.fromCharCode(31)) : [];
}

$("#save-pool").addEventListener("click", async () => {
  try {
    const mode = $("#pool-mode").value;
    const expectedGender = mode === "wife" ? "female" : "male";
    const characters = state.characters.filter((item) => item.gender === expectedGender || item.gender === "unspecified");
    const current = state.pools.find((item) => item.id === $("#pool-id").value);
    const existing = current?.character_ids ? current.character_ids.split(String.fromCharCode(31)) : [];
    const result = await apiPost("pools/save", {
      id: $("#pool-id").value.trim(), name: $("#pool-name").value.trim(), mode,
      character_ids: existing,
      expected_revision: state.poolRevision, request_id: crypto.randomUUID(),
    });
    showNotice(`角色池已保存，版本 ${result.data.revision}`, "success");
    await loadCatalog();
    editPool(state.pools.find((item) => item.id === result.data.id));
  } catch (error) { showNotice(error.message || "角色池保存失败。", "error"); }
});

$("#delete-pool").addEventListener("click", async () => {
  try { await removeCatalogItem("pool", $("#pool-id").value, "pools"); }
  catch (error) { showNotice(error.message || "删除失败。", "error"); }
});
$("#new-character").addEventListener("click", newCharacter);
$("#new-pool")?.addEventListener("click", newPool);
$("#catalog-refresh").addEventListener("click", () => loadCatalog().catch((error) => showNotice(error.message, "error")));
$("#character-search").addEventListener("input", () => loadCatalog().catch((error) => showNotice(error.message, "error")));

document.querySelectorAll("button[data-query]").forEach((button) => button.addEventListener("click", async () => {
  const scopeId = $("#data-scope").value;
  const userId = $("#data-user").value.trim();
  const mode = $("#data-mode").value;
  const params = { scope_id: scopeId, limit: 50 };
  let endpoint = button.dataset.query;
  try {
    if (!scopeId) throw new Error("先选择一个已记录的群作用域。");
    if (endpoint === "relationships") {
      if (userId) params.q = userId;
      params.mode = mode;
    } else if (endpoint === "quota") {
      if (!userId) throw new Error("用户配额查询需要填写用户 ID。");
      endpoint = `users/${encodeURIComponent(userId)}/quota`;
      params.mode = mode;
    } else if (endpoint === "intimacy") {
      if (userId) params.source_id = userId;
    } else if (endpoint === "activity") {
      params.window_days = $("#data-window").value;
      if (userId) params.user_id = userId;
    } else if (endpoint === "invites") {
      params.state = "pending";
    }
    const result = await apiGet(endpoint, params);
    $("#query-result").textContent = JSON.stringify(result.data ?? result, null, 2);
  } catch (error) {
    $("#query-result").textContent = error.message || "查询失败。";
    showNotice(error.message || "查询失败。", "error");
  }
}));
$("#data-scope").addEventListener("change", () => {
  state.dataScopeId = $("#data-scope").value;
  state.correctionPlan = null;
  $("#correction-commit").disabled = true;
  state.periodResetPlan = null;
  if ($("#period-reset-dialog").open) $("#period-reset-dialog").close();
  loadOverview().catch((error) => showNotice(error.message, "error"));
});

const correctionExamples = {
  relationship_end: '{"relationship_id":"关系 ID","compensation":0}',
  credit_grant: '{"mode":"member","user_id":"QQ号","quantity":1}',
  intimacy_set: '{"source_id":"QQ号","target_id":"QQ号","score":0}',
  activity_set: '{"user_id":"QQ号","observed_second":1780000000,"message_count":0}',
  counter_reset: '{"mode":"member","user_id":"QQ号","fields":["normal_draws"]}',
};
$("#correction-spec").value = correctionExamples[$("#correction-action").value];
$("#correction-action").addEventListener("change", () => {
  $("#correction-spec").value = correctionExamples[$("#correction-action").value];
  state.correctionPlan = null;
  $("#correction-commit").disabled = true;
});
$("#correction-preview").addEventListener("click", async () => {
  try {
    const params = JSON.parse($("#correction-spec").value);
    if (!params || Array.isArray(params) || typeof params !== "object") throw new Error("目标参数必须是 JSON 对象。");
    const result = await apiPost("admin/preview", {
      ...params, action: $("#correction-action").value, scope_id: $("#data-scope").value,
      reason: $("#correction-reason").value.trim(),
    });
    state.correctionPlan = result.data;
    $("#correction-result").textContent = JSON.stringify(result.data, null, 2);
    $("#correction-commit").disabled = false;
    showNotice("纠错预览已生成。", "success");
  } catch (error) {
    state.correctionPlan = null;
    $("#correction-result").textContent = error.message || "纠错预检失败。";
    $("#correction-commit").disabled = true;
    showNotice(error.message || "纠错预检失败。", "error");
  }
});
$("#correction-commit").addEventListener("click", async () => {
  const plan = state.correctionPlan;
  if (!plan) return;
  if (!window.confirm(`将执行 ${plan.action}。\n修改前：${JSON.stringify(plan.before)}\n修改后：${JSON.stringify(plan.after)}\n继续吗？`)) return;
  $("#correction-commit").disabled = true;
  try {
    const result = await apiPost("admin/commit", {
      preflight_id: plan.preflight_id, expected_revision: plan.config_revision, request_id: crypto.randomUUID(),
    });
    $("#correction-result").textContent = JSON.stringify(result.data || result, null, 2);
    state.correctionPlan = null;
    showNotice("纠错已提交。", "success");
  } catch (error) {
    $("#correction-result").textContent = error.message || "纠错提交失败，请重新预检。";
    state.correctionPlan = null;
    showNotice(error.message || "纠错提交失败，请重新预检。", "error");
  }
});

function clearPeriodResetPlan() {
  state.periodResetPlan = null;
  $("#period-reset-confirm").disabled = true;
}

$("#period-reset-mode").addEventListener("change", clearPeriodResetPlan);

const resetModeLabels = { wife: "老婆", husband: "老公", member: "娶群友", all: "全部玩法" };

function periodResetSummary(plan) {
  const mode = resetModeLabels[$("#period-reset-mode").value];
  const scope = state.scopes.find((item) => String(item.id) === String(plan.scope_id));
  const group = scope ? `群 ${scope.group_id}` : `群作用域 ${plan.scope_id}`;
  if (plan.action === "period_reset_relations") {
    return `${group} · ${mode}\n当前周期：${plan.before.period_id}\n将结束 ${plan.before.active_relationships} 段关系（普通 ${plan.before.normal_relationships}，抢夺槽位 ${plan.before.steal_slot_relationships}）。\n保留 ${plan.before.pending_invites_retained} 个待处理赠送、每日计数、补抽资格、历史和统计。`;
  }
  return `${group} · ${mode}\n当前周期：${plan.before.period_id}\n将清零 ${plan.before.counter_rows} 条每日计数记录。\n保留关系、补抽资格、待处理赠送、历史和统计。`;
}

async function previewPeriodReset(action) {
  clearPeriodResetPlan();
  try {
    if (!$("#data-scope").value) throw new Error("请先选择群作用域。");
    const result = await apiPost("admin/preview", {
      action,
      scope_id: Number($("#data-scope").value),
      mode: $("#period-reset-mode").value,
    });
    state.periodResetPlan = result.data;
    $("#period-reset-dialog-title").textContent = action === "period_reset_relations" ? "确认重置关系" : "确认重置每日数据";
    $("#period-reset-summary").textContent = periodResetSummary(result.data);
    $("#period-reset-confirm").disabled = false;
    $("#period-reset-dialog").showModal();
  } catch (error) {
    showNotice(error.message || "重置预检失败。", "error");
  }
}

$("#period-reset-relations-preview").addEventListener("click", () => previewPeriodReset("period_reset_relations"));
$("#period-reset-counters-preview").addEventListener("click", () => previewPeriodReset("period_reset_counters"));
$("#period-reset-cancel").addEventListener("click", () => $("#period-reset-dialog").close());
$("#period-reset-dialog").addEventListener("close", clearPeriodResetPlan);
$("#period-reset-confirm").addEventListener("click", async () => {
  const plan = state.periodResetPlan;
  if (!plan) return;
  const operation = plan.action === "period_reset_relations" ? "重置关系" : "重置每日数据";
  $("#period-reset-confirm").disabled = true;
  try {
    await apiPost("admin/commit", {
      preflight_id: plan.preflight_id,
      expected_revision: plan.config_revision,
      request_id: crypto.randomUUID(),
    });
    $("#period-reset-dialog").close();
    showNotice(`${operation}已完成。`);
  } catch (error) {
    $("#period-reset-dialog").close();
    showNotice(error.message || "重置失败，请重新预检。", "error");
    return;
  }
  loadOverview().catch((error) => showNotice(error.message || "概况刷新失败。", "error"));
});

function renderImportPreview(data) {
  const host = $("#import-report");
  host.replaceChildren();
  const summary = document.createElement("p");
  summary.className = "muted";
  summary.textContent = `包 ${data.pack_id} ${data.pack_version} · 角色有效 ${data.characters_valid}/${data.characters_total} · 冲突 ${data.characters_conflict} · 无效 ${data.characters_invalid} · 图片 ${data.images_staged}`;
  host.append(summary);
  const list = document.createElement("div");
  list.className = "import-items";
  for (const item of data.items.characters) {
    const card = document.createElement("div");
    card.className = "import-item";
    const description = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = `${item.name || "（无名称）"} · ${item.id || `项目 ${item._index}`}`;
    const details = document.createElement("p");
    details.className = "muted";
    details.textContent = item.valid
      ? `状态：${item.status_before_commit} · 图片 ${item.images.length} · 未加入角色池：${(item.unresolved_pool_ids || []).join(", ") || "无"}${item.diff ? ` · 差异 ${JSON.stringify(item.diff)}` : ""}`
      : `无效：${(item.errors || []).join("；")}`;
    description.append(name, details);
    card.append(description);
    if (item.valid && item.status_before_commit === "conflict") {
      const label = document.createElement("label");
      label.className = "field import-decision";
      const span = document.createElement("span");
      span.textContent = "同 ID 冲突处理";
      const select = document.createElement("select");
      select.dataset.characterId = item.id;
      select.add(new Option("跳过并保留现有角色", "skip"));
      select.add(new Option("更新此角色", "update"));
      label.append(span, select);
      card.append(label);
    }
    list.append(card);
  }
  for (const pool of data.items.pools) {
    const item = document.createElement("p");
    item.className = "muted";
    item.textContent = `角色池 ${pool.id}：${pool.name} · ${pool.status}${pool.errors ? ` · ${pool.errors.join("；")}` : ""}`;
    list.append(item);
  }
  host.append(list);
}

$("#import-preview").addEventListener("click", async () => {
  const file = $("#import-file").files?.[0];
  if (!file) { showNotice("先选择 JSON 或 ZIP 角色包。", "error"); return; }
  const button = $("#import-preview");
  button.disabled = true;
  $("#import-commit").disabled = true;
  state.importPlan = null;
  $("#import-report").textContent = "正在校验清单并验证所有选定素材，请等待完成…";
  try {
    const response = await apiUpload("imports/preview", file);
    const data = response.data || response;
    state.importPlan = data;
    renderImportPreview(data);
    $("#import-commit").disabled = data.characters_valid === 0 && data.pools_invalid === data.pools_total;
    showNotice("导入预检已完成。逐项选择冲突策略后可以提交。", "success");
  } catch (error) {
    $("#import-report").textContent = error.message || "导入预检失败。";
    showNotice(error.message || "导入预检失败。", "error");
  } finally {
    button.disabled = false;
  }
});

$("#import-commit").addEventListener("click", async () => {
  const plan = state.importPlan;
  if (!plan) return;
  const conflicts = {};
  for (const select of $("#import-report").querySelectorAll("select[data-character-id]")) {
    conflicts[select.dataset.characterId] = select.value;
  }
  const strategy = $("#import-image-strategy").value;
  if (!window.confirm(`提交角色包 ${plan.pack_id} ${plan.pack_version}。同 ID 未选择的条目将跳过；图片策略：${strategy}。继续吗？`)) return;
  $("#import-commit").disabled = true;
  $("#import-report").prepend(Object.assign(document.createElement("p"), { textContent: "正在提交有效条目…", className: "muted" }));
  try {
    const result = await apiPost("imports/commit", {
      job_id: plan.job_id, conflicts, image_strategy: strategy, request_id: crypto.randomUUID(),
    });
    const data = result.data || result;
    $("#import-report").textContent = JSON.stringify(data, null, 2);
    state.importPlan = null;
    await loadCatalog();
    showNotice(`导入完成：新建 ${data.created}，更新 ${data.updated}，跳过 ${data.skipped}，冲突 ${data.conflicts}`, "success");
  } catch (error) {
    $("#import-report").textContent = error.message || "导入提交失败；可通过作业状态检查结果。";
    showNotice(error.message || "导入提交失败。", "error");
  } finally {
    $("#import-commit").disabled = !state.importPlan;
  }
});

function renderRestoreScopeMappings(plan) {
  const host = $("#restore-scope-mappings");
  host.replaceChildren();
  const mappings = (plan.scope_mappings || []).filter((item) => item.requires_mapping);
  host.hidden = mappings.length === 0;
  if (!mappings.length) return;
  const heading = document.createElement("p");
  heading.className = "hint";
  heading.textContent = "备份中的作用域与当前机器人不一致。请将每个来源作用域映射到当前已知作用域；目标 UMO 会沿用当前数据库记录。";
  host.append(heading);
  for (const item of mappings) {
    const source = item.source;
    const label = document.createElement("label");
    label.className = "field";
    const caption = document.createElement("span");
    caption.textContent = `来源 ${source.platform_id} / ${source.self_id} / ${source.group_id}`;
    const select = document.createElement("select");
    select.dataset.sourceScopeId = String(source.id);
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "选择当前目标作用域";
    select.append(empty);
    for (const target of (plan.current_scopes || [])) {
      const option = document.createElement("option");
      option.value = String(target.id);
      option.textContent = `${target.platform_id} / ${target.self_id} / ${target.group_id} / ${target.umo}`;
      select.append(option);
    }
    label.append(caption, select);
    host.append(label);
  }
}

function renderMaintenanceJob(data, title) {
  const host = $("#maintenance-report");
  host.replaceChildren();
  const summary = document.createElement("p");
  summary.textContent = `${title} · 作业 ${data.job_id} · ${data.bytes ?? ""} bytes · SHA-256 ${data.sha256 ?? ""}`;
  host.append(summary);
  if (data.download) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button secondary";
    button.textContent = "下载 ZIP";
    button.addEventListener("click", () => bridge.download(data.download, {}, `${title}-${data.job_id}.zip`)
      .catch((error) => showNotice(error.message || "文件下载失败。", "error")));
    host.append(button);
  }
  const details = document.createElement("pre");
  details.textContent = JSON.stringify(data, null, 2);
  host.append(details);
}

$("#export-create").addEventListener("click", async () => {
  const poolIds = Array.from($("#export-pools").selectedOptions, (option) => option.value);
  if (!poolIds.length) { showNotice("先选择至少一个角色池。", "error"); return; }
  const button = $("#export-create");
  button.disabled = true;
  try {
    const response = await apiPost("exports/create", { pool_ids: poolIds, format: "zip" });
    renderMaintenanceJob(response.data || response, "角色包已生成");
    showNotice("角色包已经过哈希校验并准备下载。", "success");
  } catch (error) {
    $("#maintenance-report").textContent = error.message || "角色包导出失败。";
    showNotice(error.message || "角色包导出失败。", "error");
  } finally {
    button.disabled = false;
  }
});

$("#backup-create").addEventListener("click", async () => {
  const button = $("#backup-create");
  button.disabled = true;
  try {
    const response = await apiPost("backups/create", { request_id: crypto.randomUUID() });
    renderMaintenanceJob(response.data || response, "完整备份已生成");
    showNotice("数据库快照和素材已打包，可下载保存。", "success");
  } catch (error) {
    $("#maintenance-report").textContent = error.message || "完整备份失败。";
    showNotice(error.message || "完整备份失败。", "error");
  } finally {
    button.disabled = false;
  }
});

$("#restore-preview").addEventListener("click", async () => {
  const file = $("#restore-file").files?.[0];
  if (!file) { showNotice("先选择完整备份 ZIP。", "error"); return; }
  const button = $("#restore-preview");
  button.disabled = true;
  $("#restore-commit").disabled = true;
  state.restorePlan = null;
  $("#maintenance-report").textContent = "正在验证备份清单、摘要、数据库完整性和外键…";
  try {
    const response = await apiUpload("restore/preview", file);
    state.restorePlan = response.data || response;
    renderRestoreScopeMappings(state.restorePlan);
    const host = $("#maintenance-report");
    host.replaceChildren();
    const title = document.createElement("p");
    title.textContent = `恢复预检完成 · revision ${state.restorePlan.expected_revision} · 创建时间 ${state.restorePlan.backup_created_at}`;
    const details = document.createElement("pre");
    details.textContent = JSON.stringify(state.restorePlan, null, 2);
    host.append(title, details);
    $("#restore-commit").disabled = false;
  } catch (error) {
    $("#maintenance-report").textContent = error.message || "备份预检失败。";
    showNotice(error.message || "备份预检失败。", "error");
  } finally {
    button.disabled = false;
  }
});

$("#restore-commit").addEventListener("click", async () => {
  const plan = state.restorePlan;
  if (!plan) return;
  const scope_mapping = {};
  for (const item of (plan.scope_mappings || []).filter((entry) => entry.requires_mapping)) {
    const select = $(`#restore-scope-mappings select[data-source-scope-id="${item.source.id}"]`);
    const target = (plan.current_scopes || []).find((scope) => String(scope.id) === select?.value);
    if (!target) {
      showNotice("请先为所有来源作用域选择当前目标作用域。", "error");
      return;
    }
    scope_mapping[String(item.source.id)] = Object.fromEntries(
      ["platform_id", "self_id", "group_id", "umo"].map((key) => [key, target[key]]),
    );
  }
  const counts = JSON.stringify(plan.database.tables, null, 2);
  if (!window.confirm(`将用 ${plan.backup_created_at} 的备份覆盖插件数据库和素材。恢复前会自动备份当前数据。备份表数据量：\n${counts}\n确定继续？`)) return;
  const button = $("#restore-commit");
  button.disabled = true;
  try {
    const response = await apiPost("restore/commit", {
      job_id: plan.job_id, expected_revision: plan.expected_revision, confirm: true,
      scope_mapping, request_id: crypto.randomUUID(),
    });
    const data = response.data || response;
    state.restorePlan = null;
    renderMaintenanceJob(data, "恢复已完成，旧数据自动备份已保留");
    await refreshAll();
    showNotice("备份恢复完成。", "success");
  } catch (error) {
    $("#maintenance-report").textContent = error.message || "恢复失败；旧数据仍应保留，可查看自动备份。";
    showNotice(error.message || "恢复失败。", "error");
  } finally {
    button.disabled = !state.restorePlan;
  }
});
