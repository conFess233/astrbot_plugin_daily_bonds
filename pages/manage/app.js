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
  scopes: [],
  scopeId: "global",
  savedConfig: null,
  globalConfig: null,
  savedOverride: {},
  templateFields: {},
  savedSignature: "",
  configGroup: "enabled",
  revision: 0,
  globalRevision: 0,
  busy: false,
  characters: [],
  characterOffset: 0,
  characterTotal: 0,
  catalogBusy: false,
  catalogSyncBusy: false,
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
  catalogSyncPlan: null,
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
let configLoadSequence = 0;
let catalogLoadSequence = 0;
let characterLoadSequence = 0;
let searchTimer;

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
  $("#config-controls").disabled = value;
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
  "messages.notifications": "赠送与重启通知", "messages.titles": "标题与统计标签",
  "reply_quote.results": "结果回复引用", "reply_quote.errors": "错误回复引用",
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
  admin_set_wife: "管理员设置老婆成功", already_held: "管理员已持有角色",
  admin_set_denied: "设置老婆权限不足", admin_set_missing: "设置老婆未找到角色",
  admin_set_ambiguous: "设置老婆角色不明确", admin_set_unavailable: "设置老婆角色不可用",
  steal_slot_full: "抢夺槽位已满", relationship_locked: "受保护关系不可操作",
  steal_slot_locked: "目标关系不可被抢", capacity_full_single_wife: "已有老婆",
  capacity_full_single_husband: "已有老公", designated_wife: "指定老婆成功",
  designated_husband: "指定老公成功", designated_disabled: "指定抽取关闭",
  designated_slot_full: "指定槽位已满", no_designated_quota: "指定次数用完",
  designated_occupied: "指定角色已被持有", designated_drawn: "指定抽取成功",
  designated_relationship_locked: "指定关系不可操作", designated_missing: "未找到指定角色",
  designated_ambiguous: "指定角色有多个匹配", affection_target_required: "好感度查询目标无效",
  list_wife: "老婆列表回复", list_husband: "老公列表回复", list_member: "群友列表回复",
  rank_intimacy: "亲密度排行回复", rank_activity: "活跃度排行回复", query_affection: "好感度查询回复",
  member_reconcile: "成员资格变化通知", member_redraw: "群友离开补抽说明",
  owner_ineligible: "持有人失去资格说明", participant_ineligible: "赠送参与者失去资格说明",
};
const TITLE_LABELS = {
  list_wife: "老婆列表标题", list_husband: "老公列表标题", list_member: "群友列表标题",
  existing: "已有关系标题", rank_intimacy: "个人亲密度排行标题",
  rank_group_intimacy: "全群亲密度排行标题", rank_activity: "活跃度排行标题",
  query_affection: "好感度查询标题", page: "分页标题", empty: "空榜文案",
  rank_summary: "排行摘要", intimacy_value: "亲密度数值标签", activity_value: "活跃度数值标签",
  affection_value: "有向好感度标签", normal_slot: "普通槽位标签", steal_slot: "抢夺槽位标签",
  designated_slot: "指定槽位标签", wife: "老婆玩法名称", husband: "老公玩法名称", member: "群友玩法名称",
};
const CONFIG_LABELS = {
  schema_version: "配置格式版本", enabled: "启用", mode: "名单模式", ids: "名单 ID",
  extra_bot_ids: "补充机器人 QQ 号", extra_admin_ids: "额外命令管理员 QQ 号", timezone: "时区", time: "重置时间",
  allow_bare: "允许裸关键词", allow_leading_bot_mention: "允许开头 @机器人",
  allow_host_prefix: "允许宿主命令前缀", extra_prefixes: "额外命令前缀",
  cooldown_seconds: "通用冷却（秒）", capacity: "每日容量", steal_enabled: "允许抢夺",
  designated_capacity: "每周期指定次数与槽位上限", designated_unique: "指定角色只能拥有一段关系",
  steal_slot_capacity: "每日抢夺槽位数量", rank_merge_rows: "小排行合并行数上限",
  rank_max_height: "排行图片最大高度（像素）",
  steal_attempt_limit: "抢夺尝试次数上限", steal_cooldown_seconds: "抢夺冷却（秒）",
  steal_probability: "抢夺成功率（%）", stolen_limit: "被抢次数上限",
  gift_enabled: "允许赠送", gift_mode: "赠送方式", gift_timeout_seconds: "赠送确认超时（秒）",
  divorce_enabled: "允许离婚或踹群友", divorce_limit: "离婚或踹群友次数上限",
  pool_ids: "启用的角色池", intimacy_enabled: "记录亲密度", activity_enabled: "记录活跃度",
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
  divorce_member: "踹群友", list_characters: "老婆列表", list_husband: "老公列表", list_members: "群友列表",
  rank_intimacy: "亲密度排行", rank_activity: "活跃度排行",
  gift_accept: "接受赠送", gift_reject: "拒绝赠送", gift_cancel: "取消赠送", query_affection: "好感度查询",
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
  "access.extra_admin_ids": "每行一个 QQ 数字 ID，可使用 /设置老婆 命令；仅全局设置，群级不能修改管理员身份。",
  "commands.extra_prefixes": "每行一个字面前缀。",
  "modes.wife.pool_ids": "每个角色池独立开关；全部关闭时没有可抽取角色。群级覆盖关闭时继承全局开关。",
  "modes.husband.pool_ids": "每个角色池独立开关；全部关闭时没有可抽取角色。群级覆盖关闭时继承全局开关。",
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
  "display.rank_merge_rows": "1～100；不超过此行数的小排行尝试合并为一张图片，仍受图片高度限制。",
  "display.rank_max_height": "300～16000 像素；超过最大高度的排行拆分为多张图片。",
  "modes.wife.designated_capacity": "0～100；0 关闭指定老婆。指定次数和槽位共用此上限，每周期重置；失败不扣次数，不占普通或抢夺槽位。",
  "modes.husband.designated_capacity": "0～100；0 关闭指定老公。指定次数和槽位共用此上限，每周期重置；失败不扣次数，不占普通或抢夺槽位。",
  "modes.wife.designated_unique": "开启时，已有有效关系的角色不能再次指定；关闭允许同一人或多人重复指定。只影响后续指定，已有关系保留。",
  "modes.husband.designated_unique": "开启时，已有有效关系的角色不能再次指定；关闭允许同一人或多人重复指定。只影响后续指定，已有关系保留。",
  "modes.wife.steal_slot_capacity": "0～100；0 表示沿用普通持有容量，正数使用独立抢夺槽位。",
  "modes.husband.steal_slot_capacity": "0～100；0 表示沿用普通持有容量，正数使用独立抢夺槽位。",
  "modes.member.steal_slot_capacity": "0～100；0 表示沿用普通持有容量，正数使用独立抢夺槽位。",
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
  if (["messages", "reply_quote"].includes(parts[0])) return parts.slice(0, 2).join(".");
  return parts[0] === "modes" ? parts.slice(0, 2).join(".") : parts[0];
}

function configIsGlobalOnly(path) {
  return path === "schema_version" || path.startsWith("access.groups.") || path === "access.extra_admin_ids" ||
    path.startsWith("resources.") || path.startsWith("history.");
}

function configLabel(path) {
  const parts = path.split(".");
  if (parts[0] === "reply_quote") return `${MESSAGE_LABELS[parts.at(-1)] || parts.at(-1)}引用消息源`;
  if (parts[0] === "messages") return (parts[1] === "titles" ? TITLE_LABELS : MESSAGE_LABELS)[parts.at(-1)] || parts.at(-1);
  const name = CONFIG_LABELS[parts.at(-1)] || parts.at(-1);
  if (parts[0] === "commands" && parts[1] === "keywords") return `${name}关键词`;
  if (path === "access.groups.mode") return "群准入模式";
  if (path === "access.users.mode") return "用户准入模式";
  if (path === "access.groups.ids") return "群身份名单";
  if (path === "access.users.ids") return "用户 QQ 名单";
  if (path.endsWith(".enabled") && parts[0] === "modes") return "启用玩法";
  return name;
}

function isPoolConfig(path) {
  return path === "modes.wife.pool_ids" || path === "modes.husband.pool_ids";
}

function renderPoolPicker(card, input) {
  const path = input.dataset.configPath;
  const mode = path.split(".")[1];
  const ids = input.value.split(/\r?\n/).map((id) => id.trim()).filter(Boolean);
  const available = state.pools.filter((pool) => pool.mode === mode);
  const picker = card.querySelector(".pool-picker");
  picker.replaceChildren();
  for (const pool of available) {
    const label = document.createElement("label");
    label.className = "pool-option";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.value = pool.id;
    check.checked = ids.includes(pool.id);
    check.disabled = input.disabled;
    const name = document.createElement("span");
    name.textContent = `${pool.name} · ${pool.character_count} 个角色`;
    const id = document.createElement("small");
    id.textContent = pool.id;
    label.append(check, name, id);
    picker.append(label);
  }
  for (const id of ids.filter((item) => !available.some((pool) => pool.id === item))) {
    const label = document.createElement("label");
    label.className = "pool-option pool-option-missing";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.value = id;
    check.checked = true;
    check.disabled = input.disabled;
    const name = document.createElement("span");
    name.textContent = `未找到角色池 · ${id}`;
    label.append(check, name);
    picker.append(label);
  }
  if (!picker.children.length) {
    const empty = document.createElement("small");
    empty.className = "config-note";
    empty.textContent = "暂无此玩法的角色池，请先在角色目录创建或同步。";
    picker.append(empty);
  }
}

function setPoolPickerValue(card, input, ids) {
  input.value = ids.join("\n");
  renderPoolPicker(card, input);
}

function refreshPoolPickers() {
  for (const input of configFields.querySelectorAll("[data-config-path]")) {
    if (isPoolConfig(input.dataset.configPath)) renderPoolPicker(input.closest(".config-field"), input);
  }
}

function templateHint(path) {
  const fields = state.templateFields[path];
  if (!fields) return "最多 1000 字符；模板变量由服务端校验。使用 {{ 和 }} 表示大括号。";
  const allowed = fields.allowed.map((name) => `{${name}}`).join("、") || "无";
  const required = fields.required.length ? `必填：${fields.required.map((name) => `{${name}}`).join("、")}。` : "";
  const image = fields.image ? "使用 {image} 选择图片位置，不填写则只发送文字。" : "";
  const replacement = fields.image && fields.required.length ? "含 {image} 时可省略上述必填变量。" : "";
  return `最多 1000 字符；可用变量：${allowed}。${required}${replacement}${image}使用 {{ 和 }} 表示大括号。`;
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
    if (isPoolConfig(path)) {
      input = document.createElement("input");
      input.type = "hidden";
      input.value = value.join("\n");
    } else if (Array.isArray(value)) {
      input = document.createElement("textarea");
      input.rows = Math.min(Math.max(value.length + 1, 2), 5);
      input.value = value.join("\n");
      input.placeholder = "每行一项";
    } else if (path.startsWith("messages.")) {
      input = document.createElement("textarea");
      input.rows = 3;
      input.maxLength = 1000;
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
      if (path.endsWith(".steal_probability") || path.endsWith(".designated_capacity") || path.endsWith(".steal_slot_capacity")) { input.min = "0"; input.max = "100"; }
      if (path === "display.rank_merge_rows") { input.min = "1"; input.max = "100"; }
      if (path === "display.rank_max_height") { input.min = "300"; input.max = "16000"; }
    }
    input.dataset.configPath = path;
    input.setAttribute("aria-label", configLabel(path));
    label.append(title, input);
    card.append(label);
    if (isPoolConfig(path)) {
      const picker = document.createElement("div");
      picker.className = "pool-picker";
      picker.setAttribute("role", "group");
      picker.setAttribute("aria-label", configLabel(path));
      picker.addEventListener("change", (event) => {
        if (!event.target.matches('input[type="checkbox"]')) return;
        const selected = [...picker.querySelectorAll('input[type="checkbox"]:checked')].map((check) => check.value);
        input.value = selected.join("\n");
        picker.dispatchEvent(new Event("input", { bubbles: true }));
      });
      card.append(picker);
    }
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
          if (isPoolConfig(path)) setPoolPickerValue(card, input, globalValue);
          else if (Array.isArray(globalValue)) input.value = globalValue.join("\n");
          else if (input.type === "checkbox") input.checked = globalValue;
          else input.value = path.endsWith(".steal_probability") ? String(globalValue * 100) : String(globalValue);
        }
        if (isPoolConfig(path)) renderPoolPicker(card, input);
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
    if (isPoolConfig(path)) renderPoolPicker(card, input);
    const hint = CONFIG_HINTS[path] ||
      (path.startsWith("messages.") ? templateHint(path) :
      path.startsWith("reply_quote.") ? "开启后，这条即时回复会引用触发消息；关闭则不引用，不改变消息文案。" :
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
  queueMicrotask(() => {
    if (isPoolConfig(path)) input.closest(".config-field").querySelector('.pool-picker input:not(:disabled)')?.focus();
    else input.focus();
  });
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

async function loadConfig(scopeId = state.scopeId) {
  const sequence = ++configLoadSequence;
  const params = scopeId === "global" ? {} : { scope_id: scopeId };
  const result = await apiGet("config", params);
  if (sequence !== configLoadSequence) return;
  state.scopeId = scopeId;
  scopeSelect.value = scopeId;
  state.globalRevision = result.revision.global;
  state.revision = state.scopeId === "global" ? result.revision.global : result.revision.scope;
  const shown = state.scopeId === "global" ? result.data.global : result.data.effective;
  state.savedConfig = structuredClone(shown);
  state.globalConfig = structuredClone(result.data.global);
  state.savedOverride = structuredClone(result.data.override || {});
  state.templateFields = result.data.template_fields || {};
  renderConfigFields();
  state.savedSignature = configSignature();
  $("#revision").textContent = `全局版本 ${state.globalRevision} · 当前范围版本 ${state.revision}`;
  setScopeLabel();
  updateDirtyState();
}

async function refreshAll() {
  if (state.busy) return;
  setBusy(true);
  notice.replaceChildren();
  try {
    await loadScopes();
    await loadOverview();
    state.pools = (await apiGet("pools")).data;
    renderPools();
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
  if (state.busy) return;
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
  if (state.busy) return;
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
  for (const button of document.querySelectorAll(".tab")) {
    const selected = button === tab;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  }
  $("#overview-panel").hidden = tab.dataset.tab !== "overview";
  $("#settings-panel").hidden = tab.dataset.tab !== "settings";
  $("#catalog-panel").hidden = tab.dataset.tab !== "catalog";
  $("#data-panel").hidden = tab.dataset.tab !== "data";
  if (tab.dataset.tab === "catalog") loadCatalog().catch((error) => showNotice(error.message, "error"));
});
$(".tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...document.querySelectorAll(".tab")];
  const current = tabs.indexOf(event.target);
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
    (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  event.preventDefault();
  tabs[next].click();
  tabs[next].focus();
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
  if (state.busy) { scopeSelect.value = state.scopeId; return; }
  if (isConfigDirty() && !window.confirm("当前草稿尚未保存。切换范围并放弃草稿吗？")) {
    scopeSelect.value = state.scopeId;
    return;
  }
  const nextScope = scopeSelect.value;
  setBusy(true);
  try {
    await loadConfig(nextScope);
  } catch (error) {
    scopeSelect.value = state.scopeId;
    showNotice(error.message || "切换范围失败，已保留原配置。", "error");
  } finally {
    setBusy(false);
    updateDirtyState();
  }
});
window.addEventListener("beforeunload", (event) => {
  if (isConfigDirty()) {
    event.preventDefault();
    event.returnValue = "";
  }
});

await bridge.ready();
bridge.onContext(() => {
  document.title = bridge.t("pages.manage.title", "今日姻缘 · 管理");
});
await refreshAll();
checkCatalogSync(false).catch((error) => {
  $("#catalog-sync-banner").textContent = error.message || "仓库更新检查失败，可稍后手动重查。";
  showNotice(error.message || "仓库更新检查失败，可稍后手动重查。", "error");
});

function syncItemLabel(item) {
  return `${item.kind === "character" ? "角色" : "角色池"} · ${item.name} (${item.id})`;
}

function syncChangeText(item) {
  const changes = item.changes;
  if (item.status === "removed") return changes.note;
  const fields = Object.entries(changes.fields || {}).map(([key, value]) => `${key}: ${JSON.stringify(value.local)} → ${JSON.stringify(value.remote)}`);
  if (item.kind === "character") fields.push(`新增图片 ${changes.images_to_add} 张`);
  else fields.push(`新增成员 ${changes.members_to_add.join("、") || "无"}；移除成员 ${changes.members_to_remove.join("、") || "无"}`);
  return fields.join("\n");
}

function renderCatalogSync(plan) {
  const host = $("#catalog-sync-items");
  host.replaceChildren();
  const actionable = plan.items.filter((item) => ["new", "update", "conflict"].includes(item.status));
  $("#catalog-sync-controls").hidden = !actionable.length;
  $("#catalog-sync-banner").textContent = plan.unavailable
    ? "仓库默认分支尚未发布新的角色与角色池目录；本地目录照常使用。"
    : actionable.length
      ? `发现 ${actionable.length} 项仓库配置更新 · 提交 ${plan.commit_sha.slice(0, 8)}`
      : `仓库配置已是最新 · 提交 ${plan.commit_sha.slice(0, 8)}`;
  for (const item of plan.items) {
    const label = document.createElement("label");
    label.className = `sync-item ${item.status}`;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.kind = item.kind;
    input.dataset.id = item.id;
    input.dataset.status = item.status;
    input.disabled = item.status === "blocked" || item.status === "removed";
    input.checked = item.status === "new" || item.status === "update";
    const title = document.createElement("strong");
    const statuses = { new: "新增", update: "可安全更新", conflict: "本地改动冲突，默认跳过", blocked: "本地已删除，不能同步", removed: "仓库已移除，本地保留" };
    title.textContent = `${syncItemLabel(item)} · ${statuses[item.status]}`;
    const detail = document.createElement("pre");
    detail.textContent = syncChangeText(item);
    label.append(input, title, detail);
    host.append(label);
  }
  host.querySelectorAll("input").forEach((input) => input.addEventListener("change", updateCatalogSyncSelection));
  updateCatalogSyncSelection();
}

function updateCatalogSyncSelection() {
  const count = $("#catalog-sync-items").querySelectorAll("input:checked").length;
  $("#catalog-sync-commit").disabled = state.catalogSyncBusy || count === 0;
  $("#catalog-sync-commit").textContent = count ? `同步所选 ${count} 项` : "同步所选项目";
}

async function checkCatalogSync(force) {
  $("#catalog-sync-check").disabled = true;
  $("#catalog-sync-banner").textContent = "正在检查仓库更新…";
  try {
    const result = await apiPost("catalog-sync/check", { force });
    state.catalogSyncPlan = result.data;
    renderCatalogSync(result.data);
    if (result.data.updates) showNotice(`发现 ${result.data.updates} 项仓库角色配置更新。`, "success");
    else if (force && !result.data.unavailable) showNotice("仓库角色配置已是最新。", "success");
  } catch (error) {
    state.catalogSyncPlan = null;
    $("#catalog-sync-items").replaceChildren();
    $("#catalog-sync-controls").hidden = true;
    $("#catalog-sync-commit").disabled = true;
    $("#catalog-sync-banner").textContent = error.message || "检查失败。";
    throw error;
  } finally {
    $("#catalog-sync-check").disabled = false;
  }
}

$("#catalog-sync-check").addEventListener("click", () => checkCatalogSync(true).catch((error) => showNotice(error.message || "检查失败。", "error")));
for (const [id, predicate] of [
  ["catalog-sync-safe", (item) => item.status === "new" || item.status === "update"],
  ["catalog-sync-all", (item) => !["blocked", "removed"].includes(item.status)],
  ["catalog-sync-characters", (item) => item.kind === "character" && !["blocked", "removed"].includes(item.status)],
  ["catalog-sync-pools", (item) => item.kind === "pool" && !["blocked", "removed"].includes(item.status)],
  ["catalog-sync-none", () => false],
]) {
  $("#" + id).addEventListener("click", () => {
    $("#catalog-sync-items").querySelectorAll("input").forEach((input) => {
      input.checked = !input.disabled && predicate({ kind: input.dataset.kind, status: input.dataset.status });
    });
    updateCatalogSyncSelection();
  });
}
$("#catalog-sync-commit").addEventListener("click", async () => {
  const plan = state.catalogSyncPlan;
  if (!plan || state.catalogSyncBusy) return;
  const chosen = [...$("#catalog-sync-items").querySelectorAll("input:checked")];
  const characters = chosen.filter((input) => input.dataset.kind === "character").map((input) => input.dataset.id);
  const pools = chosen.filter((input) => input.dataset.kind === "pool").map((input) => input.dataset.id);
  state.catalogSyncBusy = true;
  $("#catalog-sync-commit").disabled = true;
  try {
    const result = await apiPost("catalog-sync/commit", { preview_id: plan.preview_id, characters, pools });
    showNotice(`已同步 ${result.data.characters} 个角色、${result.data.pools} 个角色池及 ${result.data.images_added} 张新图片。`, "success");
    await loadCatalog();
    await checkCatalogSync(true);
  } catch (error) {
    showNotice(error.message || "同步失败，未写入所选项目。", "error");
  } finally {
    state.catalogSyncBusy = false;
    updateCatalogSyncSelection();
  }
});

function renderCharacters() {
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
  if (!state.characters.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = $("#character-search").value.trim() ? "没有匹配的角色。" : "暂无角色，可新建、同步仓库或导入角色包。";
    characters.append(empty);
  }
  const start = state.characterTotal ? state.characterOffset + 1 : 0;
  $("#character-page").textContent = `${start}–${state.characterOffset + state.characters.length} / ${state.characterTotal}`;
  $("#character-prev").disabled = state.characterOffset === 0;
  $("#character-next").disabled = state.characterOffset + state.characters.length >= state.characterTotal;
}

function renderPools() {
  const selectedPools = new Set([...$("#character-pools").querySelectorAll("input:checked")].map((input) => input.value));
  const selectedExport = new Set([...$("#export-pools").selectedOptions].map((option) => option.value));
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
    exportPools.add(new Option(`${pool.name} · ${pool.mode} · ${pool.character_count}`, pool.id, false, selectedExport.has(pool.id)));
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
    input.checked = selectedPools.has(pool.id);
    const text = document.createTextNode(`${pool.name} (${pool.mode})`);
    label.append(input, text);
    poolChecks.append(label);
  }
}

async function loadCatalog({ refreshPools = true } = {}) {
  const sequence = ++catalogLoadSequence;
  const query = $("#character-search").value.trim();
  const offset = state.characterOffset;
  const [characters, pools] = await Promise.all([
    apiGet("characters", { q: query, limit: 100, offset }),
    refreshPools ? apiGet("pools") : Promise.resolve(null),
  ]);
  if (sequence !== catalogLoadSequence) return;
  if (offset && !characters.data.items.length && characters.data.total <= offset) {
    state.characterOffset = Math.max(0, Math.floor((characters.data.total - 1) / 100) * 100);
    return loadCatalog({ refreshPools });
  }
  state.characters = characters.data.items;
  state.characterTotal = characters.data.total;
  renderCharacters();
  if (pools) {
    state.pools = pools.data;
    renderPools();
    refreshPoolPickers();
  }
}

async function loadCharacter(id, afterSave = false) {
  const sequence = ++characterLoadSequence;
  const response = await apiGet("character", { id });
  if (sequence !== characterLoadSequence || (state.catalogBusy && !afterSave)) return;
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
  characterLoadSequence += 1;
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

$("#character-image").addEventListener("change", (event) => withCatalogWrite(async () => {
  const files = [...(event.target.files || [])];
  if (!files.length) return;
  const uploaded = [];
  const failed = [];
  try {
    for (const file of files) {
      if (state.characterImages.length >= 20) {
        failed.push(`${file.name}：角色最多关联 20 张图片`);
        continue;
      }
      try {
        const result = await apiUpload("media/upload", file);
        const item = result.data || result;
        if (!state.characterImages.includes(item.hash)) state.characterImages.push(item.hash);
        uploaded.push(file.name);
      } catch (error) {
        failed.push(`${file.name}：${error.message || "上传失败"}`);
      }
    }
    renderCharacterImages();
    showNotice(`成功上传 ${uploaded.length} 张${failed.length ? `；失败 ${failed.length} 张：${failed.join("；")}` : ""}`, failed.length ? "error" : "success");
  } finally {
    event.target.value = "";
  }
}));

async function withCatalogWrite(action) {
  if (state.catalogBusy) return;
  state.catalogBusy = true;
  characterLoadSequence += 1;
  $("#catalog-controls").disabled = true;
  try {
    await action();
  } finally {
    state.catalogBusy = false;
    $("#catalog-controls").disabled = false;
  }
}

$("#save-character").addEventListener("click", () => withCatalogWrite(async () => {
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
    await loadCharacter(result.data.id, true);
  } catch (error) { showNotice(error.message || "角色保存失败。", "error"); }
}));

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

$("#delete-character").addEventListener("click", () => withCatalogWrite(async () => {
  try { await removeCatalogItem("character", $("#character-id").value, "characters"); }
  catch (error) { showNotice(error.message || "删除失败。", "error"); }
}));

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
}

$("#save-pool").addEventListener("click", () => withCatalogWrite(async () => {
  try {
    const mode = $("#pool-mode").value;
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
}));

$("#delete-pool").addEventListener("click", () => withCatalogWrite(async () => {
  try { await removeCatalogItem("pool", $("#pool-id").value, "pools"); }
  catch (error) { showNotice(error.message || "删除失败。", "error"); }
}));
$("#new-character").addEventListener("click", newCharacter);
$("#new-pool")?.addEventListener("click", newPool);
$("#catalog-refresh").addEventListener("click", () => loadCatalog().catch((error) => showNotice(error.message, "error")));
$("#character-search").addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  catalogLoadSequence += 1;
  state.characterOffset = 0;
  searchTimer = window.setTimeout(() => loadCatalog({ refreshPools: false })
    .catch((error) => showNotice(error.message || "角色搜索失败。", "error")), 250);
});
for (const [id, direction] of [["character-prev", -1], ["character-next", 1]]) {
  $("#" + id).addEventListener("click", () => {
    const previousOffset = state.characterOffset;
    state.characterOffset = Math.max(0, previousOffset + direction * 100);
    $("#character-prev").disabled = true;
    $("#character-next").disabled = true;
    loadCatalog({ refreshPools: false }).catch((error) => {
      state.characterOffset = previousOffset;
      renderCharacters();
      showNotice(error.message || "读取角色列表失败。", "error");
    });
  });
}

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
    return `${group} · ${mode}\n当前周期：${plan.before.period_id}\n将结束 ${plan.before.active_relationships} 段关系（普通 ${plan.before.normal_relationships}，抢夺槽位 ${plan.before.steal_slot_relationships}，指定槽位 ${plan.before.designated_relationships || 0}）。\n保留 ${plan.before.pending_invites_retained} 个待处理赠送、每日计数、补抽资格、历史和统计。`;
  }
  return `${group} · ${mode}\n当前周期：${plan.before.period_id}\n将清零 ${plan.before.counter_rows} 条每日计数记录（含普通抽取、指定抽取与抢夺计数）。\n保留关系、补抽资格、待处理赠送、历史和统计。`;
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
      scope_mapping, request_id: plan.request_id ??= crypto.randomUUID(),
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
