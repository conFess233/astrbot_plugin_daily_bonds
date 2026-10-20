import { configPage } from "./config-workspace.js";
import { initBatchImages } from "./batch-images.js";
import { confirmAction } from "./confirm-dialog.js";
import { initUploadCompression, prepareUploadImage } from "./upload-images.js";

import { configAt, configLeaves, displayValue, draftValues, changedPaths, serializeDraft, templateParts } from "./config-model.js";

initUploadCompression();

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
  draftValues: {},
  overridePaths: new Set(),
  view: "overview",
  scrollPositions: {},
  selectedTemplate: "",
  selectedReplies: new Set(),
  fonts: [],
  pendingSubmission: null,
  saveNeedsReload: false,
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
  if (result?.ok === false) {
    const error = new Error(result.error?.message || result.message || "操作失败");
    error.code = result.error?.code || result.code;
    throw error;
  }
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
  return withProgress(async () => {
    if (endpoint === "media/upload" || /^batch-images\/[^/]+\/upload$/.test(endpoint)) {
      file = await prepareUploadImage(file);
      if (file.size > 12 * 1024 * 1024) throw new Error("压缩后仍超过单张 12 MiB 限制。");
    }
    return normalizeApiResponse(await bridge.upload(endpoint, file));
  });
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
let overviewLoadSequence = 0;
let queryLoadSequence = 0;
let queryOffset = 0;
let currentQuery = null;
const fieldCards = new Map();
let beforeUnloadActive = false;

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
  const dismiss = () => {
    clearTimeout(timer); item.remove();
    if (!notice.children.length) notice.hidePopover?.();
  };
  close.addEventListener("click", dismiss);
  item.append(body, close);
  notice.prepend(item);
  placeNotice();
  const timer = window.setTimeout(dismiss, 10000);
  return item;
}

function placeNotice() {
  notice.hidePopover?.();
  // A modal makes nodes outside it inert, even when a toast uses the top layer.
  const modal = [...document.querySelectorAll("dialog[open]")].at(-1);
  (modal || document.body).append(notice);
  notice.style.top = `${Math.max(20, $(".topbar").getBoundingClientRect().bottom + 12)}px`;
  if (notice.children.length) notice.showPopover?.();
}

document.addEventListener("close", (event) => {
  if (event.target.contains(notice)) placeNotice();
}, true);

function setBusy(value) {
  state.busy = value;
  const locked = value || !!state.pendingSubmission || state.saveNeedsReload || !state.savedConfig;
  $("#config-controls").disabled = locked;
  for (const button of [$("#refresh"), $("#discard"), $("#preview"), $("#save"), scopeSelect]) button.disabled = value;
  scopeSelect.disabled = locked;
  $("#preview").disabled = locked;
  $("#discard").disabled = locked || !isConfigDirty();
  $("#save").disabled = value || !state.savedConfig;
}

function dirtyPaths() {
  return state.savedConfig ? changedPaths(state.savedConfig, state.savedOverride, state.draftValues, state.overridePaths, state.scopeId !== "global") : [];
}

function beforeUnload(event) {
  event.preventDefault();
  event.returnValue = "";
}

function updateDirtyState() {
  const paths = dirtyPaths();
  const dirty = paths.length > 0;
  const visible = dirty || !!state.pendingSubmission || state.saveNeedsReload;
  $("#save-bar").hidden = !visible;
  document.body.classList.toggle("has-save-bar", visible);
  $("#dirty-indicator").textContent = state.pendingSubmission ? "提交结果待确认，请重试" : state.saveNeedsReload ? "已保存，等待重新读取" : !state.savedConfig ? "尚未读取配置" : dirty ? `${paths.length} 项未保存` : "已保存";
  $("#dirty-indicator").classList.toggle("dirty", dirty || !!state.pendingSubmission);
  $("#save").textContent = state.pendingSubmission ? "重试本次提交" : state.saveNeedsReload ? "重新读取配置" : "保存配置";
  for (const [path, card] of fieldCards) {
    card.classList.toggle("modified", paths.includes(path));
    const badge = card.querySelector(".field-source");
    if (badge) badge.textContent = state.scopeId === "global" ? "全局值" : configIsGlobalOnly(path) ? "仅全局设置" : state.overridePaths.has(path) ? "本群覆盖" : "继承全局 · 编辑即覆盖";
    const input = card.querySelector("[data-config-path]");
    const toggleText = card.querySelector(".toggle-state");
    if (toggleText) toggleText.textContent = input.checked ? "已启用" : "已关闭";
  }
  const needsWarning = dirty || !!state.pendingSubmission;
  if (needsWarning !== beforeUnloadActive) {
    window[needsWarning ? "addEventListener" : "removeEventListener"]("beforeunload", beforeUnload);
    beforeUnloadActive = needsWarning;
  }
  setBusy(state.busy);
  updateReplySelection();
  updateConfigSummary();
  updateConditionalFields();
  for (const button of document.querySelectorAll("[data-template-path]")) {
    button.classList.toggle("modified", paths.includes(button.dataset.templatePath));
  }
}

function updateConditionalFields() {
  const cards = Object.keys(MODE_NAMES).map((mode) => fieldCards.get(`modes.${mode}.gift_timeout_seconds`)).filter(Boolean);
  for (const card of cards) {
    const path = card.querySelector("[data-config-path]").dataset.configPath;
    card.hidden = state.draftValues[path.replace("gift_timeout_seconds", "gift_mode")] !== "confirm";
    const note = card.parentElement?.querySelector(".conditional-note"); if (note) note.hidden = !card.hidden;
  }
  const row = cards[0]?.closest("tr");
  if (row) row.hidden = cards.every((card) => card.hidden);
}

function setScopeLabel() {
  const scope = state.scopes.find((item) => String(item.id) === state.scopeId);
  const label = scope ? `群 ${scope.group_id} · 机器人 ${scope.self_id}` : "全局配置";
  $("#editor-title").textContent = label;
  $("#save-scope").textContent = `当前范围：${label}`;
}

function configFeedback(message, kind = "error") {
  if (message) return showNotice(message, kind);
}

$("#save-bar-toggle").addEventListener("click", () => {
  const collapsed = $("#save-bar").classList.toggle("collapsed");
  $("#save-bar-toggle").setAttribute("aria-expanded", String(!collapsed));
  $("#save-bar-toggle").textContent = collapsed ? "展开" : "收起";
  $("#save-bar-actions").hidden = collapsed;
  $("#save-bar-note").hidden = collapsed;
  document.body.classList.toggle("save-bar-collapsed", collapsed);
});

function updateConfigSummary() {
  const host = $("#config-summary");
  host.replaceChildren();
  if (!state.savedConfig) { host.textContent = "配置尚未读取"; return; }
  let enabled = 0;
  for (const [mode, name] of [["wife", "今日老婆"], ["husband", "今日老公"], ["member", "娶群友"]]) {
    const path = `modes.${mode}.enabled`;
    const on = state.draftValues[path];
    if (on) enabled += 1;
    const line = document.createElement("p");
    const source = state.scopeId === "global" ? "全局" : state.overridePaths.has(path) ? "本群覆盖" : "继承全局";
    line.textContent = `${name} · ${on ? "已启用" : "已关闭"}（${source}）· 容量 ${state.draftValues[`modes.${mode}.capacity`]} · ${dirtyPaths().some((item) => item.startsWith(`modes.${mode}.`)) ? "含草稿" : "已保存"}`;
    host.append(line);
  }
  $("#mode-summary").textContent = `已启用玩法 ${enabled}/3 · ${dirtyPaths().length} 项未保存`;
}

async function loadScopes() {
  const result = await apiGet("scopes");
  state.scopes = result.data ?? [];
  const selected = state.scopeId;
  scopeSelect.replaceChildren(new Option("全局配置", "global"));
  for (const scope of state.scopes) {
    const option = new Option(`群 ${scope.group_id} · 机器人 ${scope.self_id}`, String(scope.id));
    scopeSelect.add(option);
  }
  if ([...scopeSelect.options].some((option) => option.value === selected)) {
    scopeSelect.value = selected;
  } else {
    scopeSelect.add(new Option(`当前作用域 ${selected}`, selected));
    scopeSelect.value = selected;
  }
  renderScopes();
}

function renderScopes() {
  const list = $("#scope-list");
  list.replaceChildren();
  const dataScope = $("#data-scope");
  const overviewScope = $("#overview-scope");
  const previousOverview = overviewScope.value;
  dataScope.replaceChildren();
  overviewScope.replaceChildren(new Option("全部作用域", ""));
  state.periodResetPlan = null;
  if ($("#period-reset-dialog").open) $("#period-reset-dialog").close();
  for (const scope of state.scopes) {
    const text = `群 ${scope.group_id} · 机器人 ${scope.self_id}`;
    dataScope.add(new Option(text, String(scope.id)));
    overviewScope.add(new Option(text, String(scope.id)));
  }
  if ([...dataScope.options].some((option) => option.value === state.dataScopeId)) dataScope.value = state.dataScopeId;
  state.dataScopeId = dataScope.value;
  overviewScope.value = [...overviewScope.options].some((option) => option.value === previousOverview) ? previousOverview : "";
  renderScopeList();
}

function renderScopeList() {
  const list = $("#scope-list");
  list.replaceChildren();
  const search = $("#scope-search").value.trim().toLowerCase();
  const scopes = state.scopes.filter((scope) => `${scope.group_id} ${scope.self_id} ${scope.platform_id}`.toLowerCase().includes(search));
  for (const scope of scopes.slice(0, 50)) {
    const item = document.createElement("div");
    item.className = "scope-item";
    const label = document.createElement("span");
    label.textContent = `群 ${scope.group_id} · ${scope.platform_id} · 机器人 ${scope.self_id}`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button secondary";
    button.textContent = "编辑规则";
    button.addEventListener("click", async () => { if (await changeScope(String(scope.id))) openView("settings"); });
    item.append(label, button);
    list.append(item);
  }
  if (!scopes.length) list.textContent = state.scopes.length ? "未找到匹配的群。" : "尚无记录群；有群消息到达后会自动建立作用域。";
  else if (scopes.length > 50) {
    const note = document.createElement("p");
    note.textContent = `已显示前 50 / ${scopes.length} 个作用域，请搜索群号或机器人缩小范围。`;
    list.append(note);
  }
}
$("#scope-search").addEventListener("input", renderScopeList);

const CONFIG_GROUPS = {
  schema_version: "配置版本", enabled: "总开关", access: "群与名单", reset: "每日重置",
  commands: "指令与触发", "modes.wife": "今日老婆", "modes.husband": "今日老公",
  "modes.member": "娶群友", statistics: "统计", weights: "抽取权重",
  display: "展示与排行", members: "群成员缓存", history: "历史记录", resources: "资源限制",
  "messages.results": "群内操作结果", "messages.errors": "群内错误提示",
  "messages.notifications": "赠送与重启通知", "messages.titles": "标题与统计标签",
  "reply_quote.results": "结果回复引用", "reply_quote.errors": "错误回复引用",
  batch_import: "批量图片检测默认值",
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
  affection_value: "有向好感度标签", normal_slot: "抽取来源标签", gift_slot: "赠送来源标签", steal_slot: "抢夺来源标签",
  designated_slot: "指定槽位标签", wife: "老婆玩法名称", husband: "老公玩法名称", member: "群友玩法名称",
};
const CONFIG_LABELS = {
  font_id: "图片字体",
  schema_version: "配置格式版本", enabled: "启用", mode: "名单模式", ids: "名单 ID",
  male_keywords: "男类检测关键词", female_keywords: "女类检测关键词",
  male_pool_id: "默认男卡池 ID", female_pool_id: "默认女卡池 ID",
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
  activity_floor: "零活跃天数权重系数",
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

function configGroup(path) {
  if (path === "schema_version") return "enabled";
  const parts = path.split(".");
  if (["messages", "reply_quote", "reply_enabled"].includes(parts[0])) return parts.slice(0, 2).join(".");
  return parts[0] === "modes" ? parts.slice(0, 2).join(".") : parts[0];
}

function configIsGlobalOnly(path) {
  return path === "schema_version" || path.startsWith("access.groups.") || path === "access.extra_admin_ids" || path.startsWith("batch_import.") ||
    path.startsWith("resources.") || path.startsWith("history.");
}

function configLabel(path) {
  const parts = path.split(".");
  if (parts[0] === "reply_enabled") return `${MESSAGE_LABELS[parts.at(-1)] || parts.at(-1)}启用回复`;
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

function refreshPoolPickers() {
  for (const input of configFields.querySelectorAll("[data-config-path]")) {
    if (isPoolConfig(input.dataset.configPath)) renderPoolPicker(input.closest(".config-field"), input);
  }
}

function templateHint(path) {
  const fields = state.templateFields[path];
  const empty = path.startsWith("messages.titles.") ? "留空仅隐藏对应标题或标签。" : "留空或仅填空白则不发送此消息、附图或引用。";
  if (!fields) return `最多 1000 字符；${empty}模板变量由服务端校验。使用 {{ 和 }} 表示大括号。`;
  const allowed = fields.allowed.map((name) => `{${name}}`).join("、") || "无";
  const required = fields.required.length ? `非空时必填：${fields.required.map((name) => `{${name}}`).join("、")}。` : "";
  const image = fields.image ? "使用 {image} 选择图片位置，不填写则只发送文字。" : "";
  const replacement = fields.image && fields.required.length ? "含 {image} 时可省略上述必填变量。" : "";
  return `最多 1000 字符；${empty}可用变量：${allowed}。${required}${replacement}${image}使用 {{ 和 }} 表示大括号。`;
}

function writeInputValue(input, value) {
  if (input.type === "checkbox") input.checked = value;
  else input.value = value;
}

function createConfigField(path, value) {
  const card = document.createElement("div");
  card.className = "config-field";
  const label = document.createElement("label");
  label.className = "field";
  const title = document.createElement("span");
  title.textContent = configLabel(path);
  const globalOnly = configIsGlobalOnly(path);
  const overridden = state.scopeId !== "global" && state.overridePaths.has(path);
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
    input.setAttribute("aria-description", "最多 1000 字符；留空则不回复，标题留空隐藏文字");
    input.placeholder = path.startsWith("messages.titles.") ? "留空隐藏对应文字" : "留空则不回复";
    input.value = value;
  } else if (typeof value === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.setAttribute("role", "switch");
    input.checked = value;
  } else if (path === "resources.font_id") {
    input = document.createElement("select");
    input.add(new Option(state.fonts.length ? "自动选择本地字体" : "未读取到本地字体（使用文字回复）", "auto"));
    for (const font of state.fonts) input.add(new Option(font.name, font.id));
    if (value !== "auto" && !state.fonts.some((font) => font.id === value)) input.add(new Option("所选字体不可用（自动选择本地字体）", value));
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
  input.id = `field-${path.replaceAll(".", "-")}`;
  input.setAttribute("aria-label", configLabel(path));
  const control = document.createElement("span");
  control.className = input.type === "checkbox" ? "toggle-control" : "input-control";
  control.append(input);
  if (input.type === "checkbox") { const status = document.createElement("span"); status.className = "toggle-state"; control.append(status); }
  label.append(title, control);
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
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    card.append(picker);
  }
  if (state.scopeId !== "global" && !globalOnly) {
    const override = document.createElement("label");
    override.className = "config-override";
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.dataset.overridePath = path;
    toggle.setAttribute("aria-label", `${configLabel(path)} · 本群覆盖`);
    toggle.checked = overridden;
    input.disabled = false;
    toggle.addEventListener("change", () => {
      if (toggle.checked) state.overridePaths.add(path);
      else {
        state.overridePaths.delete(path);
        state.draftValues[path] = displayValue(path, configAt(state.globalConfig, path));
        writeInputValue(input, state.draftValues[path]);
        if (isPoolConfig(path)) renderPoolPicker(card, input);
      }
      card.classList.toggle("inherited", !toggle.checked);
      updateDirtyState();
      if (/^(messages|reply_quote|reply_enabled)\./.test(path)) renderTemplatePreview();
    });
    override.append(toggle, document.createTextNode("本群覆盖（关闭恢复继承）"));
    card.append(override);
    card.classList.toggle("inherited", !overridden);
  } else if (state.scopeId !== "global" || path === "schema_version") {
    input.disabled = true;
    const badge = document.createElement("small");
    badge.className = "config-note";
    badge.textContent = path === "schema_version" ? "只读" : "仅全局设置";
    card.append(badge);
  }
  writeInputValue(input, state.draftValues[path]);
  if (isPoolConfig(path)) renderPoolPicker(card, input);
  const source = document.createElement("small");
  source.className = "field-source";
  card.append(source);
  const hint = CONFIG_HINTS[path] ||
    (path.startsWith("messages.") ? templateHint(path) :
    path.startsWith("reply_quote.") ? "开启后，这条即时回复会引用触发消息；关闭则不引用，不改变消息文案。" :
    path.startsWith("commands.keywords.") ? "每行一个别名，至少一项。" : "");
  if (hint) {
    const help = document.createElement("small");
    help.className = "config-note";
    help.textContent = hint;
    help.id = `${input.id}-help`;
    card.append(help);
  }
  const error = document.createElement("small");
  error.className = "config-error";
  error.hidden = true;
  error.id = `${input.id}-error`;
  input.setAttribute("aria-describedby", `${hint ? input.id + "-help " : ""}${error.id}`);
  card.append(error);
  if (path === "resources.font_id") {
    const image = document.createElement("img"); image.className = "font-preview"; image.alt = "字体中文预览"; image.hidden = true;
    const button = document.createElement("button"); button.type = "button"; button.className = "button secondary"; button.textContent = "预览字体";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { const result = await apiGet("fonts/preview", { font_id: input.value }); image.src = result.data.src; image.hidden = false; }
      catch (error) { showNotice(error.message || "预览失败。", "error"); }
      finally { button.disabled = false; }
    }); card.append(button, image);
  }
  fieldCards.set(path, card);
  return card;
}

function renderConfigFields() {
  const previousEditor = fieldCards.get(state.selectedTemplate)?.querySelector("textarea");
  const ui = {
    mode: configFields.querySelector(".modes-workspace")?.dataset.mode || "wife",
    expanded: [...configFields.querySelectorAll(".mode-details[open]")].map((item) => item.dataset.modeGroup),
    search: $("#template-search")?.value || "", category: $("#template-category")?.value || "全部",
    listScroll: $("#template-list")?.scrollTop || 0,
    pane: $(".reply-edit-area")?.dataset.pane || "edit",
    selection: previousEditor ? [previousEditor.selectionStart, previousEditor.selectionEnd] : null,
  };
  fieldCards.clear();
  configFields.replaceChildren();
  configTabs.replaceChildren();
  if (!state.savedConfig) return;
  for (const [path, value] of configLeaves(state.savedConfig)) createConfigField(path, value);
  renderModesWorkspace();
  renderRepliesWorkspace();
  renderAdvancedWorkspace();
  renderConfigSearch();
  $(".modes-workspace").dataset.mode = ui.mode;
  for (const button of document.querySelectorAll("[data-mode-choice]")) button.setAttribute("aria-pressed", String(button.dataset.modeChoice === ui.mode));
  for (const details of configFields.querySelectorAll(".mode-details")) details.open = ui.expanded.includes(details.dataset.modeGroup);
  $("#template-search").value = ui.search; $("#template-category").value = ui.category;
  filterTemplates(); $("#template-list").scrollTop = ui.listScroll;
  $(".reply-edit-area").dataset.pane = ui.pane;
  for (const pane of ["edit", "preview"]) $("#reply-" + pane + "-tab").setAttribute("aria-pressed", String(pane === ui.pane));
  if (ui.selection && state.selectedTemplate) fieldCards.get(state.selectedTemplate).querySelector("textarea").setSelectionRange(...ui.selection);
  openView(state.view);
  updateDirtyState();
}

const VIEW_INFO = {
  overview: ["概览", "今日姻缘 · 插件概览", "查看实际数据状态与常用操作。"],
  settings: ["玩法设置", "玩法设置", "调整三种玩法的开关与数值，保存后生效。"],
  catalog: ["角色与角色池", "角色与角色池", "维护角色、图片与角色池，保留独立提交和导入预检。"],
  replies: ["消息与回复词", "消息与回复词", "编辑模板、变量与引用；模拟预览不会实际发送。"],
  data: ["数据查询", "数据查询", "按机器人与群查询关系、配额、邀请及统计。"],
  advanced: ["高级设置", "高级设置", "调整准入、周期、统计、显示和资源限制。"],
};

function openView(view) {
  if (!VIEW_INFO[view]) return;
  const previousView = state.view;
  if (view !== previousView) state.scrollPositions[previousView] = window.scrollY;
  state.view = view;
  document.body.dataset.view = view;
  const [name, title, description] = VIEW_INFO[view];
  $("#breadcrumb-page").textContent = name;
  $("#page-title").textContent = title;
  $("#page-description").textContent = description;
  for (const button of document.querySelectorAll(".tab[data-tab]")) {
    const active = button.dataset.tab === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  }
  for (const id of ["overview", "catalog", "data"]) $(`#${id}-panel`).hidden = id !== view;
  const isConfig = ["settings", "replies", "advanced"].includes(view);
  $("#settings-panel").hidden = !isConfig;
  $("#settings-panel").setAttribute("aria-labelledby", `tab-${view}`);
  configTabs.hidden = view !== "advanced";
  for (const section of configFields.querySelectorAll("[data-config-view]")) section.hidden = section.dataset.configView !== view;
  if (view === "advanced") selectConfigGroup(state.configGroup);
  if (view !== previousView) window.scrollTo({ top: state.scrollPositions[view] || 0, behavior: "instant" });
}

function configView(view, className = "") {
  const section = document.createElement("div");
  section.dataset.configView = view;
  section.className = className;
  configFields.append(section);
  return section;
}

const MODE_NAMES = { wife: "今日老婆", husband: "今日老公", member: "娶群友" };
const COMMON_MODE_KEYS = ["enabled", "capacity", "steal_enabled", "steal_probability", "steal_attempt_limit"];
const MODE_ROWS = {
  enabled: ["玩法启用", "控制该玩法是否可用"],
  capacity: ["普通容量", "普通抽取总额度", "个"],
  steal_enabled: ["抢夺启用", "是否允许抢夺"],
  steal_probability: ["抢夺成功率", "单次有效抢夺的基础概率", "%"],
  steal_attempt_limit: ["抢夺次数", "每周期尝试上限，0 为不限", "次"],
};

function modeMatrix(keys) {
  const wrap = document.createElement("div");
  wrap.className = "matrix-scroll";
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", "三玩法参数对照，可横向滚动");
  const table = document.createElement("table");
  table.className = "setting-matrix";
  const head = document.createElement("thead");
  const headers = document.createElement("tr");
  for (const title of ["配置项", ...Object.values(MODE_NAMES)]) {
    const th = document.createElement("th"); th.scope = "col"; th.textContent = title; headers.append(th);
  }
  head.append(headers); table.append(head);
  const body = document.createElement("tbody");
  for (const key of keys) {
    const row = document.createElement("tr");
    const label = document.createElement("th"); label.scope = "row";
    const title = document.createElement("strong"); title.textContent = MODE_ROWS[key]?.[0] || CONFIG_LABELS[key] || key;
    const hint = document.createElement("small"); hint.textContent = MODE_ROWS[key]?.[1] || CONFIG_HINTS[`modes.wife.${key}`] || "";
    label.append(title, hint); row.append(label);
    for (const [mode, name] of Object.entries(MODE_NAMES)) {
      const cell = document.createElement("td");
      const card = fieldCards.get(`modes.${mode}.${key}`);
      if (card) {
        card.classList.add("matrix-cell");
        card.querySelector("[data-config-path]").setAttribute("aria-label", `${name} · ${MODE_ROWS[key]?.[0] || CONFIG_LABELS[key] || key}`);
        if (MODE_ROWS[key]?.[2]) {
          const unit = document.createElement("span"); unit.className = "input-unit"; unit.textContent = MODE_ROWS[key][2];
          card.querySelector(".input-control").append(unit);
        }
        cell.append(card);
        if (key === "gift_timeout_seconds") { const note = document.createElement("small"); note.className = "conditional-note muted"; note.textContent = "直接赠送，无需确认超时"; cell.append(note); }
      } else { cell.className = "muted"; cell.textContent = "— 此玩法不适用"; }
      row.append(cell);
    }
    body.append(row);
  }
  table.append(body); wrap.append(table);
  return wrap;
}

function renderModesWorkspace() {
  const view = configView("settings", "modes-workspace");
  const main = document.createElement("div"); main.className = "mode-main";
  view.dataset.mode = "wife";
  const switcher = document.createElement("div"); switcher.className = "mode-switcher"; switcher.setAttribute("aria-label", "窄屏玩法切换");
  for (const [key, name] of [...Object.entries(MODE_NAMES), ["all", "三玩法对照"]]) {
    const button = document.createElement("button"); button.type = "button"; button.className = "button secondary"; button.textContent = name; button.dataset.modeChoice = key; button.setAttribute("aria-pressed", String(key === "wife"));
    button.addEventListener("click", () => { view.dataset.mode = key; for (const other of switcher.children) other.setAttribute("aria-pressed", String(other === button)); }); switcher.append(button);
  }
  main.append(switcher);
  const common = document.createElement("section"); common.className = "card common-modes";
  const title = document.createElement("h3"); title.textContent = "常用参数";
  const note = document.createElement("p"); note.className = "muted"; note.textContent = "修改先进入草稿，保存后生效。";
  common.append(title, note, modeMatrix(COMMON_MODE_KEYS)); main.append(common);
  const allKeys = [...new Set(Object.values(state.savedConfig.modes).flatMap((mode) => Object.keys(mode)))].filter((key) => !COMMON_MODE_KEYS.includes(key));
  const groups = [
    ["抢夺进阶", (key) => key.startsWith("steal_") || key === "stolen_limit"],
    ["赠送设置", (key) => key.startsWith("gift_")],
    ["离婚设置", (key) => key.startsWith("divorce_")],
    ["指定设置（仅今日老婆 / 今日老公）", (key) => key.startsWith("designated_")],
    ["角色池与候选", (key) => key === "pool_ids"],
  ];
  const assigned = new Set();
  for (const [name, accepts] of [...groups, ["其他玩法参数", (key) => !assigned.has(key)]]) {
    const keys = allKeys.filter(accepts); if (!keys.length) continue;
    keys.forEach((key) => assigned.add(key));
    const details = document.createElement("details"); details.className = "card mode-details"; details.dataset.modeGroup = name;
    const summary = document.createElement("summary"); summary.textContent = name;
    details.append(summary, modeMatrix(keys));
    if (name === "角色池与候选") {
      const link = document.createElement("button"); link.type = "button"; link.className = "button secondary"; link.textContent = "查看候选资格与统计权重";
      link.addEventListener("click", () => { openView("advanced"); selectConfigGroup("weights"); }); details.append(link);
    }
    main.append(details);
  }
  const help = document.createElement("aside"); help.className = "card mode-help";
  help.innerHTML = '<h3>规则说明</h3><div><strong>当前配置范围</strong><p>全局配置只影响继承该字段的群；群级覆盖仅作用于所选群。</p></div><div><strong>开关与数值</strong><p>关闭玩法不会清空已配置数值。</p></div><div><strong>次数为 0</strong><p>抢夺次数为 0 表示不限。抢夺槽位为 0 表示沿用普通容量。</p></div><div><strong>提交范围</strong><p>仅保存配置，角色、角色池和图片单独提交。</p></div>';
  const copy = document.createElement("details"); copy.className = "card mode-copy";
  copy.innerHTML = '<summary>同步本次修改的玩法参数</summary><p class="hint">只同步来源玩法本次修改的参数，专属字段仅同步到支持的玩法。</p><label class="field"><span>来源玩法</span><select id="copy-source"></select></label><div id="copy-targets" class="actions"></div><button id="copy-parameters" type="button" class="button secondary">同步参数改动</button>';
  for (const [mode, name] of Object.entries(MODE_NAMES)) {
    copy.querySelector("select").add(new Option(name, mode));
    const label = document.createElement("label"); label.className = "check-field";
    const check = document.createElement("input"); check.type = "checkbox"; check.dataset.copyMode = mode; label.append(check, document.createTextNode(name)); copy.querySelector("#copy-targets").append(label);
  }
  const updateTargets = () => { for (const check of copy.querySelectorAll("[data-copy-mode]")) { check.disabled = check.dataset.copyMode === copy.querySelector("select").value; if (check.disabled) check.checked = false; } };
  copy.querySelector("select").addEventListener("change", updateTargets); updateTargets();
  copy.querySelector("button").addEventListener("click", () => {
    const source = copy.querySelector("select").value;
    const paths = dirtyPaths().filter((path) => path.startsWith(`modes.${source}.`) && configPage(path, configAt(state.savedConfig, path)) === "parameters");
    const targets = [...copy.querySelectorAll("[data-copy-mode]:checked")].map((check) => check.dataset.copyMode);
    try {
      serializeConfig();
      let count = 0;
      for (const mode of targets) for (const path of paths) if (assignDraftField(path.replace(`modes.${source}.`, `modes.${mode}.`), state.draftValues[path])) count++;
      showNotice(count ? `已同步 ${count} 项参数到草稿。` : "请先修改来源玩法的参数，并勾选目标玩法。", count ? "success" : "error");
    } catch (error) { showConfigError(error.message); showNotice(error.message, "error"); }
  });
  main.append(copy); view.append(main, help);
}

function templateCategory(path) {
  const key = path.split(".").at(-1);
  if (path.startsWith("messages.errors.")) return "错误提示";
  if (path.startsWith("messages.notifications.")) return "通知";
  if (path.startsWith("messages.titles.")) return "标题标签";
  if (/steal|stolen/.test(key)) return "抢夺";
  if (/gift|invite/.test(key)) return "赠送";
  if (/divorce/.test(key)) return "离婚";
  if (/rank|list|affection/.test(key)) return "列表与统计";
  if (/draw|designated|capacity/.test(key)) return "抽取";
  return "其他";
}

function renderRepliesWorkspace() {
  const view = configView("replies");
  view.innerHTML = '<div class="reply-tabs"><button type="button" class="button secondary" id="reply-templates-tab" aria-pressed="true">回复模板</button><button type="button" class="button secondary" id="reply-keywords-tab" aria-pressed="false">指令关键词</button></div><div id="reply-templates" class="replies-workspace"><section class="card template-directory"><h3>模板目录 <small id="template-total"></small></h3><button type="button" class="button secondary reply-directory-back" id="reply-directory-back">返回编辑</button><label class="field"><span>搜索模板</span><input id="template-search" type="search" placeholder="名称、路径或文案" /></label><label class="field"><span>分类</span><select id="template-category"></select></label><div id="template-list" class="template-list"></div></section><div class="reply-edit-area"><div class="reply-pane-tabs"><button type="button" class="button secondary reply-directory-toggle" id="reply-directory-toggle">模板目录</button><button type="button" class="button secondary" id="reply-edit-tab" aria-pressed="true">编辑</button><button type="button" class="button secondary" id="reply-preview-tab" aria-pressed="false">预览</button></div><section id="reply-editor-panel" class="card"><h3 id="template-name"></h3><p id="template-path" class="muted template-path"></p><div id="reply-editor"></div><small id="template-count" class="muted"></small><h4>插入变量</h4><div id="template-variables" class="variable-buttons"></div><div id="reply-quote"></div></section><aside id="reply-preview-panel" class="card"><h3>消息预览</h3><p class="preview-label">模拟预览 · 不会实际发送</p><div class="chat-preview"><div class="chat-user">示例用户消息</div><div id="template-preview" class="chat-bubble"></div></div><p class="hint">演示数据；实际图片、引用和消息样式以 QQ 客户端为准。</p></aside></div></div><section id="reply-keywords" class="card" hidden><h3>指令关键词</h3><p class="hint">每行一个别名，冲突由服务端检查；与回复模板共享同一配置草稿。</p><div class="config-grid"></div></section><div id="reply-field-store" hidden></div>';
  const store = $("#reply-field-store");
  for (const [path, card] of fieldCards) {
    if (/^(messages|reply_quote|reply_enabled)\./.test(path)) store.append(card);
    if (path.startsWith("commands.keywords.")) $("#reply-keywords .config-grid").append(card);
  }
  const categories = ["全部", "抽取", "抢夺", "赠送", "离婚", "错误提示", "通知", "列表与统计", "标题标签", "其他"];
  for (const category of categories) $("#template-category").add(new Option(category, category));
  $("#template-search").addEventListener("input", filterTemplates);
  $("#template-category").addEventListener("change", filterTemplates);
  for (const kind of ["templates", "keywords"]) $(`#reply-${kind}-tab`).addEventListener("click", () => showReplySection(kind));
  for (const pane of ["edit", "preview"]) $(`#reply-${pane}-tab`).addEventListener("click", () => {
    $(".reply-edit-area").dataset.pane = pane;
    for (const key of ["edit", "preview"]) $(`#reply-${key}-tab`).setAttribute("aria-pressed", String(key === pane));
  });
  $("#reply-directory-toggle").addEventListener("click", () => { $(".replies-workspace").dataset.directory = "true"; });
  $("#reply-directory-back").addEventListener("click", () => { $(".replies-workspace").dataset.directory = "false"; });
  initReplyBulk();
  filterTemplates();
  if (!fieldCards.has(state.selectedTemplate)) state.selectedTemplate = [...fieldCards.keys()].find((path) => path.startsWith("messages."));
  if (state.selectedTemplate) selectTemplate(state.selectedTemplate);
}

function assignDraftField(path, raw, inherited = false) {
  const card = fieldCards.get(path); if (!card || state.scopeId !== "global" && configIsGlobalOnly(path)) return false;
  const input = card.querySelector("[data-config-path]");
  if (inherited) { const flag = card.querySelector("[data-override-path]"); if (!flag) return false; flag.checked = false; flag.dispatchEvent(new Event("change", { bubbles: true })); }
  else { writeInputValue(input, raw); if (isPoolConfig(path)) renderPoolPicker(card, input); input.dispatchEvent(new Event("input", { bubbles: true })); }
  return true;
}

function updateReplySelection() {
  const summary = $("#reply-selection-count"); if (!summary) return;
  const selected = [...state.selectedReplies].filter((path) => fieldCards.has(path));
  summary.textContent = `已选 ${selected.length} 条回复（含其他分类）· 只修改草稿`;
  for (const button of document.querySelectorAll("[data-reply-bulk]")) button.disabled = !selected.length || button.dataset.replyBulk === "inherit" && state.scopeId === "global";
}

function initReplyBulk() {
  const bulk = document.createElement("details"); bulk.className = "reply-bulk";
  bulk.innerHTML = '<summary>批量调整回复</summary><div class="actions"><button type="button" class="button secondary" data-select-replies="visible">全选当前分类</button><button type="button" class="button secondary" data-select-replies="all">全选所有回复</button><button type="button" class="button secondary" data-select-replies="none">清空选择</button></div><div class="actions"><button type="button" class="button secondary" data-reply-bulk="enable">开启回复</button><button type="button" class="button secondary" data-reply-bulk="disable">关闭回复</button><button type="button" class="button secondary" data-reply-bulk="quote">开启引用</button><button type="button" class="button secondary" data-reply-bulk="unquote">关闭引用</button><button type="button" class="button secondary" data-reply-bulk="inherit">选中回复恢复继承</button></div><p id="reply-selection-count" class="hint" role="status"></p>';
  $(".template-directory").insertBefore(bulk, $("#template-list"));
  bulk.addEventListener("click", (event) => {
    const button = event.target.closest("button"); if (!button) return;
    const selection = button.dataset.selectReplies;
    if (selection) {
      if (selection === "none") state.selectedReplies.clear();
      else for (const path of fieldCards.keys()) if (path.startsWith("messages.") && fieldCards.has(path.replace("messages.", "reply_enabled.")) && (selection === "all" || [...$("#template-list").querySelectorAll("[data-template-path]")].some((item) => item.dataset.templatePath === path))) state.selectedReplies.add(path);
      filterTemplates(); return;
    }
    const action = button.dataset.replyBulk; if (!action) return;
    for (const path of state.selectedReplies) {
      if (action === "inherit") for (const prefix of ["messages.", "reply_enabled.", "reply_quote."]) assignDraftField(path.replace("messages.", prefix), null, true);
      else assignDraftField(path.replace("messages.", ["enable", "disable"].includes(action) ? "reply_enabled." : "reply_quote."), ["enable", "quote"].includes(action));
    }
    renderTemplatePreview(); updateReplySelection();
  });
}

function showReplySection(kind) {
  $("#reply-templates").hidden = kind !== "templates";
  $("#reply-keywords").hidden = kind !== "keywords";
  for (const key of ["templates", "keywords"]) $(`#reply-${key}-tab`).setAttribute("aria-pressed", String(key === kind));
}

function filterTemplates() {
  const list = $("#template-list"); list.replaceChildren();
  const search = $("#template-search").value.trim().toLowerCase();
  const category = $("#template-category").value;
  const paths = [...fieldCards.keys()].filter((path) => path.startsWith("messages."));
  const visible = paths.filter((path) => (category === "全部" || templateCategory(path) === category) && `${path} ${configLabel(path)} ${state.draftValues[path]}`.toLowerCase().includes(search));
  $("#template-total").textContent = `${visible.length} / ${paths.length}`;
  for (const path of visible) {
    const button = document.createElement("button"); button.type = "button"; button.dataset.templatePath = path;
    button.className = "template-item";
    button.classList.toggle("modified", dirtyPaths().includes(path));
    button.setAttribute("aria-pressed", String(path === state.selectedTemplate));
    const name = document.createElement("strong"); name.textContent = configLabel(path);
    const group = document.createElement("small"); group.textContent = `${templateCategory(path)} · ${path.split(".").at(-1)}`;
    button.append(name, group); button.addEventListener("click", () => selectTemplate(path));
    const row = document.createElement("div"); row.className = "template-row";
    if (fieldCards.has(path.replace("messages.", "reply_enabled."))) {
      const check = document.createElement("input"); check.type = "checkbox"; check.dataset.selectReply = path; check.checked = state.selectedReplies.has(path); check.setAttribute("aria-label", `选择${configLabel(path)}`);
      check.addEventListener("change", () => { check.checked ? state.selectedReplies.add(path) : state.selectedReplies.delete(path); updateReplySelection(); }); row.append(check);
    }
    row.append(button); list.append(row);
  }
  updateReplySelection();
  if (!visible.length) list.textContent = "没有匹配模板；当前编辑内容仍保留。";
}

function selectTemplate(path) {
  state.selectedTemplate = path;
  $(".replies-workspace").dataset.directory = "false";
  const store = $("#reply-field-store");
  for (const id of ["reply-editor", "reply-quote"]) for (const card of [...$("#" + id).children]) store.append(card);
  $("#reply-editor").append(fieldCards.get(path));
  for (const prefix of ["reply_enabled.", "reply_quote."]) {
    const optionPath = path.replace(/^messages\./, prefix);
    if (fieldCards.has(optionPath)) $("#reply-quote").append(fieldCards.get(optionPath));
  }
  $("#template-name").textContent = configLabel(path);
  $("#template-path").textContent = path;
  const variables = $("#template-variables"); variables.replaceChildren();
  const textarea = fieldCards.get(path).querySelector("textarea");
  for (const variable of state.templateFields[path]?.allowed || []) {
    const button = document.createElement("button"); button.type = "button"; button.className = "variable-button"; button.textContent = state.templateFields[path].descriptions?.[variable] ? `{${variable}} · ${state.templateFields[path].descriptions[variable]}` : `{${variable}}`;
    button.addEventListener("click", () => {
      textarea.setRangeText(`{${variable}}`, textarea.selectionStart, textarea.selectionEnd, "end");
      textarea.focus(); textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }); variables.append(button);
  }
  if (!variables.children.length) variables.textContent = "本条没有可插入变量。";
  for (const button of document.querySelectorAll("[data-template-path]")) button.setAttribute("aria-pressed", String(button.dataset.templatePath === path));
  $(".reply-edit-area").dataset.pane = "edit";
  $("#reply-edit-tab").setAttribute("aria-pressed", "true"); $("#reply-preview-tab").setAttribute("aria-pressed", "false");
  renderTemplatePreview();
}

function renderTemplatePreview() {
  const path = state.selectedTemplate;
  if (!path || !$("#template-preview")) return;
  const text = state.draftValues[path];
  $("#template-count").textContent = `${Array.from(text).length} / 1000 字符`;
  const host = $("#template-preview"); host.replaceChildren();
  const metadata = state.templateFields[path];
  if (!metadata) { host.textContent = "模板规则尚未获取，请重新读取配置后预览。"; return; }
  try {
    const parts = templateParts(text, metadata, path);
    if (!text.trim() || state.draftValues[path.replace(/^messages\./, "reply_enabled.")] === false) {
      host.textContent = path.startsWith("messages.titles.") ? "空文字，隐藏对应标题或标签" : "此回复不会发送消息、附图或引用"; return;
    }
    if (state.draftValues[path.replace(/^messages\./, "reply_quote.")]) {
      const quote = document.createElement("blockquote"); quote.textContent = "引用：示例用户消息"; host.append(quote);
    }
    for (const part of parts) {
      if (part.variable === "image") {
        const image = document.createElement("div"); image.className = "preview-image"; image.textContent = "图片占位 · 演示素材"; host.append(image);
      } else host.append(document.createTextNode(part.text ?? ({ name: "示例角色", names: "示例角色", target_name: "示例群友", group_name: "示例群", 群名: "示例群", days: "7", accept_keyword: "接受赠送", invite_id: "示例邀请" }[part.variable] || `示例${part.variable}`)));
    }
  } catch { host.textContent = "请校验并修改模板后查看预览。"; }
}

function renderAdvancedWorkspace() {
  const view = configView("advanced");
  const groups = new Map();
  for (const [path, card] of fieldCards) {
    if (path.startsWith("modes.") || /^(messages|reply_quote|reply_enabled)\./.test(path) || path.startsWith("commands.keywords.")) continue;
    const group = configGroup(path);
    if (!groups.has(group)) {
      const section = document.createElement("section"); section.className = "config-group card"; section.dataset.group = group;
      section.id = `config-group-${group.replaceAll(".", "-")}`;
      const heading = document.createElement("h3"); heading.textContent = CONFIG_GROUPS[group] || group;
      const grid = document.createElement("div"); grid.className = "config-grid";
      section.append(heading, grid); view.append(section); groups.set(group, grid);
      const button = document.createElement("button"); button.type = "button"; button.className = "config-tab"; button.dataset.configGroup = group;
      button.id = `config-tab-${group.replaceAll(".", "-")}`; button.setAttribute("role", "tab"); button.setAttribute("aria-controls", section.id); button.textContent = CONFIG_GROUPS[group] || group;
      section.setAttribute("role", "tabpanel"); section.setAttribute("aria-labelledby", button.id); configTabs.append(button);
    }
    groups.get(group).append(card);
  }
  if (!groups.has(state.configGroup)) state.configGroup = groups.keys().next().value;
  selectConfigGroup(state.configGroup);
}

function renderConfigSearch() {
  const options = $("#config-search-options"); options.replaceChildren();
  for (const path of fieldCards.keys()) {
    const option = document.createElement("option"); option.value = `${configLabel(path)} · ${path}`; options.append(option);
  }
}

function revealField(path) {
  if (path.endsWith(".gift_timeout_seconds") && state.draftValues[path.replace("gift_timeout_seconds", "gift_mode")] !== "confirm") {
    showNotice("赠送超时仅在确认模式中生效，请先选择等待确认。");
    return revealField(path.replace("gift_timeout_seconds", "gift_mode"));
  }
  if (path.startsWith("modes.")) {
    const mode = path.split(".")[1];
    $(".modes-workspace").dataset.mode = mode;
    for (const button of document.querySelectorAll("[data-mode-choice]")) button.setAttribute("aria-pressed", String(button.dataset.modeChoice === mode));
  }
  const view = path.startsWith("modes.") ? "settings" : /^(messages|reply_quote|reply_enabled)\./.test(path) || path.startsWith("commands.keywords.") ? "replies" : "advanced";
  openView(view);
  if (view === "replies") {
    if (path.startsWith("commands.keywords.")) showReplySection("keywords");
    else { showReplySection("templates"); selectTemplate(path.replace(/^reply_(?:quote|enabled)\./, "messages.")); }
  } else if (view === "advanced") selectConfigGroup(configGroup(path));
  const card = fieldCards.get(path);
  const details = card.closest("details"); if (details) details.open = true;
  card.scrollIntoView({ block: "center", behavior: "instant" });
  card.querySelector("[data-config-path]").focus({ preventScroll: true });
}

function selectConfigGroup(group) {
  state.configGroup = group;
  for (const tab of configTabs.querySelectorAll("[data-config-group]")) {
    const selected = tab.dataset.configGroup === group;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  for (const section of configFields.querySelectorAll(".config-group")) section.hidden = section.dataset.group !== group;
}

function serializeConfig() {
  return serializeDraft(state.savedConfig, state.globalConfig, state.draftValues, state.overridePaths, state.scopeId !== "global", FLOAT_CONFIG_PATHS);
}

function isConfigDirty() { return dirtyPaths().length > 0; }

function showConfigError(message) {
  for (const error of configFields.querySelectorAll(".config-error")) error.hidden = true;
  const path = [...fieldCards.keys()].sort((a, b) => b.length - a.length).find((candidate) => message.includes(candidate));
  if (!path) return;
  const card = fieldCards.get(path);
  const error = card.querySelector(".config-error");
  error.hidden = true;
  revealField(path);
  card.querySelector("[data-config-path]").setAttribute("aria-invalid", "true");
}

async function describeConfigConflict() {
  const latest = await apiGet("config", state.scopeId === "global" ? {} : { scope_id: state.scopeId });
  const current = state.scopeId === "global" ? latest.data.global : latest.data.effective;
  const toast = configFeedback("配置版本冲突。草稿已保留，请核对差异；放弃配置更改可重新读取服务端值。");
  const differences = configLeaves(current).filter(([path, value]) => JSON.stringify(value) !== JSON.stringify(configAt(state.savedConfig, path)))
    .map(([path, value]) => ({ 字段: path, 服务端最新值: value, 当前草稿: state.draftValues[path] }));
  if (differences.length) {
    const details = document.createElement("details"); const summary = document.createElement("summary"); summary.textContent = "查看配置差异"; details.append(summary, dataTable(differences)); toast.querySelector("span").append(details);
  }
  return "配置版本冲突，当前草稿已保留。";
}

async function loadOverview() {
  const sequence = ++overviewLoadSequence;
  const scopeId = $("#overview-scope").value;
  const grid = $("#overview-cards");
  if (grid.dataset.scope !== scopeId) { grid.replaceChildren(); $("#overview-tasks").replaceChildren(); $("#period-summary").textContent = ""; $("#overview-updated").textContent = ""; }
  grid.dataset.scope = scopeId;
  $("#overview-refresh").disabled = true;
  $("#overview-updated").textContent = "正在读取统计…";
  try {
    const result = await apiGet("overview", scopeId ? { scope_id: scopeId } : {});
    if (sequence !== overviewLoadSequence) return;
    const data = result.data;
    const scope = state.scopes.find((item) => String(item.id) === scopeId);
    const range = scopeId ? `群 ${scope?.group_id || scopeId}` : "全部作用域";
    const metrics = [
      ["管理接口", "本次读取成功", "本次请求"],
      ...(!scopeId ? [["已记录作用域", data.scopes, "全部记录；不等于在线群"]] : []),
      ["当前有效关系", data.active_relationships, range],
      ["待处理赠送邀请", data.pending_invites, range],
      ["已录入角色", data.catalog?.characters, "全局资产"],
      ...(scopeId ? [["配置引用角色池数", data.enabled_pool_count, "所选群；不代表池可用性"]] : []),
      ["通知故障", data.failed_notifications, range],
    ];
    grid.replaceChildren();
    for (const [label, value, source] of metrics) {
      const card = document.createElement("article"); card.className = "metric";
      const title = document.createElement("p"); title.className = "metric-label"; title.textContent = label;
      const number = document.createElement("p"); number.className = "metric-value"; number.textContent = value ?? "—"; number.classList.toggle("metric-text", typeof value === "string");
      const note = document.createElement("small"); note.textContent = source;
      card.append(title, number, note); grid.append(card);
    }
    const period = data.current_period;
    const time = (value) => new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short", timeZone: period.timezone }).format(new Date(value * 1000));
    $("#period-summary").textContent = !scopeId ? `当前周期数量 ${data.current_periods ?? "未获取"} · 选择一个群查看周期起止时间与重置安排。` : period ? `当前周期 ${period.sequence} · ${time(period.starts_at)} – ${time(period.ends_at)} · ${period.timezone} ${period.reset_time}` : "该群还没有已创建的当前周期。";
    const tasks = $("#overview-tasks"); tasks.replaceChildren();
    for (const [label, count] of [[`${range}待处理赠送邀请`, data.pending_invites], [`${range}通知故障`, data.failed_notifications], ["全局维护任务故障", data.failed_jobs]]) {
      const line = document.createElement("p"); line.textContent = `${label}：${count ?? "未获取"}`; tasks.append(line);
    }
    if (data.pending_invites > 0) {
      const button = document.createElement("button"); button.type = "button"; button.className = "button secondary"; button.textContent = "查询赠送邀请";
      button.addEventListener("click", () => {
        openView("data");
        if (scopeId) { $("#data-scope").value = scopeId; $("#data-scope").dispatchEvent(new Event("change")); document.querySelector('[data-query="invites"]').click(); }
        else showNotice("请选择要查询的机器人与群，再点击赠送邀请。");
      }); tasks.append(button);
    }
    if (!state.scopes.length) { const empty = document.createElement("p"); empty.textContent = "目前没有已记录群；接收到群消息后出现。全局资产仍可维护。"; tasks.prepend(empty); }
    const refreshed = new Date().toLocaleTimeString();
    grid.dataset.refreshed = refreshed;
    $("#overview-updated").textContent = `本页读取时间 ${refreshed}`;
  } catch (error) {
    if (sequence !== overviewLoadSequence) return;
    showNotice(`统计读取失败：${error.message || "请重试"}。${grid.children.length ? "保留上次成功数据。" : "当前没有可显示的数据。"}`, "error");
    $("#overview-updated").textContent = grid.children.length ? `上次数据 ${grid.dataset.refreshed || "时间未知"} · 待刷新` : "尚无统计数据";
  } finally { if (sequence === overviewLoadSequence) $("#overview-refresh").disabled = false; }
}

async function loadConfig(scopeId = state.scopeId) {
  const sequence = ++configLoadSequence;
  const result = await apiGet("config", scopeId === "global" ? {} : { scope_id: scopeId });
  if (sequence !== configLoadSequence) return;
  const shown = scopeId === "global" ? result.data.global : result.data.effective;
  const values = draftValues(shown);
  state.scopeId = scopeId;
  scopeSelect.value = scopeId;
  state.globalRevision = result.revision.global;
  state.revision = scopeId === "global" ? result.revision.global : result.revision.scope;
  state.savedConfig = structuredClone(shown);
  state.globalConfig = structuredClone(result.data.global);
  state.savedOverride = structuredClone(result.data.override || {});
  state.draftValues = values;
  state.overridePaths = new Set(configLeaves(state.savedOverride).map(([path]) => path));
  state.templateFields = result.data.template_fields || {};
  state.selectedReplies.clear();
  state.pendingSubmission = null;
  state.saveNeedsReload = false;
  configFeedback("");
  renderConfigFields();
  $("#revision").textContent = `全局版本 ${state.globalRevision} · 当前范围版本 ${state.revision}`;
  setScopeLabel();
  updateDirtyState();
}

async function refreshAll() {
  if (state.busy || state.pendingSubmission) return;
  setBusy(true);
  try {
    try { await loadScopes(); } catch (error) { showNotice(error.message || "作用域读取失败。", "error"); }
    await loadOverview();
    try { state.pools = (await apiGet("pools")).data; renderPools(); } catch (error) { showNotice(error.message || "角色池读取失败。", "error"); }
    try { state.fonts = (await apiGet("fonts")).data; } catch (error) { showNotice(error.message || "字体读取失败。", "error"); }
    await loadConfig();
  } catch (error) { configFeedback(error.message || "读取配置失败，可重试；已有草稿仍保留。"); }
  finally { setBusy(false); updateDirtyState(); }
}

function validateDraft() {
  const serialized = serializeConfig();
  for (const [path, fields] of Object.entries(state.templateFields)) templateParts(configAt(serialized.draft, path), fields, path);
  return serialized;
}

async function previewDraft() {
  if (state.busy || !state.savedConfig) return;
  setBusy(true);
  try {
    const { draft, override } = validateDraft();
    const result = await apiPost("config/preview", { scope_id: state.scopeId, draft, ...(state.scopeId === "global" ? {} : { override }) });
    configFeedback(state.scopeId === "global" ? `校验通过，影响 ${result.data.affected_scopes.length} 个已记录群。草稿尚未保存。` : "校验通过，仅作用于所选群；草稿尚未保存。", "success");
  } catch (error) { configFeedback(error.message || "配置校验失败。"); showConfigError(error.message || ""); }
  finally { setBusy(false); updateDirtyState(); }
}

async function saveDraft() {
  if (state.busy || !state.savedConfig) return false;
  setBusy(true);
  try {
    if (state.saveNeedsReload) { await loadConfig(); return true; }
    if (!state.pendingSubmission) {
      const { draft, override } = validateDraft();
      const preview = await apiPost("config/preview", { scope_id: state.scopeId, draft, ...(state.scopeId === "global" ? {} : { override }) });
      const scopeText = state.scopeId === "global" ? `全局配置，影响 ${preview.data.affected_scopes.length} 个已记录群` : $("#editor-title").textContent;
      if (!await confirmAction(`将保存 ${dirtyPaths().length} 项配置修改。\n范围：${scopeText}\n角色、角色池和图片不包含在此提交中。继续吗？`)) return false;
      state.pendingSubmission = { scope_id: state.scopeId, draft, ...(state.scopeId === "global" ? {} : { override }), expected_revision: state.revision, request_id: crypto.randomUUID() };
    }
    const payload = state.pendingSubmission;
    let result;
    try { result = await apiPost("config/save", payload); }
    catch (error) {
      const code = error.code || error.error?.code || "";
      if (code === "CONFIG_SYNC_FAILED" || error.message?.includes("配置已保存")) configFeedback("配置已保存，宿主同步失败。请点击重试本次提交完成同步。");
      else if (/CONFLICT|INVALID|DENIED|FORBIDDEN|UNAUTHORIZED/.test(code) || error.message?.includes("版本冲突")) {
        state.pendingSubmission = null;
        if (/CONFLICT/.test(code) || error.message?.includes("版本冲突")) await describeConfigConflict();
        else { configFeedback(error.message); showConfigError(error.message); }
      } else configFeedback(`提交结果尚未确认：${error.message || "网络中断"}。原草稿和本次提交已保留，请重试。`);
      return false;
    }
    state.pendingSubmission = null;
    state.savedConfig = structuredClone(payload.draft);
    state.savedOverride = structuredClone(payload.override || {});
    if (state.scopeId === "global") state.globalConfig = structuredClone(payload.draft);
    state.revision = state.scopeId === "global" ? result.revision.global : result.revision.scope;
    state.globalRevision = result.revision.global;
    $("#revision").textContent = `全局版本 ${state.globalRevision} · 当前范围版本 ${state.revision}`;
    try { await loadConfig(); configFeedback("配置已保存，后续群事件将读取新配置。", "success"); }
    catch { state.saveNeedsReload = true; configFeedback("配置已保存，重新读取失败。当前内容保留，请点击重新读取配置。", "success"); }
    return true;
  } catch (error) { configFeedback(error.message || "配置校验失败，草稿仍保留。"); showConfigError(error.message || ""); return false; }
  finally { setBusy(false); updateDirtyState(); }
}

async function resolveDraft() {
  if (state.pendingSubmission || state.saveNeedsReload) { showNotice("请先确认本次提交结果或重新读取已保存配置。", "error"); return false; }
  if (!isConfigDirty()) return true;
  const dialog = $("#draft-dialog");
  const choice = await new Promise((resolve) => {
    dialog.returnValue = "cancel";
    dialog.addEventListener("close", () => resolve(dialog.returnValue), { once: true });
    for (const [id, value] of [["draft-cancel", "cancel"], ["draft-discard", "discard"], ["draft-save", "save"]]) $("#" + id).onclick = () => dialog.close(value);
    dialog.showModal();
  });
  if (choice === "save") return await saveDraft() && !state.saveNeedsReload;
  return choice === "discard";
}

async function changeScope(nextScope) {
  scopeSelect.value = state.scopeId;
  if (state.busy || !await resolveDraft()) return false;
  setBusy(true);
  try { await loadConfig(nextScope); return true; }
  catch (error) { scopeSelect.value = state.scopeId; showNotice(error.message || "切换范围失败，已保留原草稿。", "error"); return false; }
  finally { setBusy(false); updateDirtyState(); }
}

$(".tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-tab]"); if (!tab) return;
  openView(tab.dataset.tab);
  if (tab.dataset.tab === "catalog") loadCatalog().catch((error) => showNotice(error.message, "error"));
});
$(".tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
  const tabs = [...document.querySelectorAll(".tab")]; const current = tabs.indexOf(event.target);
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1) + tabs.length) % tabs.length;
  event.preventDefault(); tabs[next].click(); tabs[next].focus();
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
$("#refresh").addEventListener("click", async () => { if (await resolveDraft()) await refreshAll(); });
$("#overview-refresh").addEventListener("click", loadOverview);
$("#overview-scope").addEventListener("change", loadOverview);
$("#preview").addEventListener("click", previewDraft);
$("#save").addEventListener("click", saveDraft);
$("#discard").addEventListener("click", async () => {
  if (state.busy) return;
  setBusy(true);
  try { await loadConfig(); showNotice("配置草稿已放弃，已读取服务端最新值。"); }
  catch (error) { configFeedback(error.message || "读取失败，草稿仍保留。"); }
  finally { setBusy(false); updateDirtyState(); }
});
let templatePreviewTimer;
configFields.addEventListener("input", (event) => {
  const input = event.target; const path = input.dataset.configPath; if (!path || configIsGlobalOnly(path) && state.scopeId !== "global" || path === "schema_version") return;
  state.draftValues[path] = input.type === "checkbox" ? input.checked : input.value;
  if (state.scopeId !== "global") {
    state.overridePaths.add(path);
    const toggle = fieldCards.get(path).querySelector("[data-override-path]"); if (toggle) toggle.checked = true;
    fieldCards.get(path).classList.remove("inherited");
  }
  const error = input.closest(".config-field")?.querySelector(".config-error"); if (error) error.hidden = true;
  input.removeAttribute("aria-invalid");
  updateDirtyState();
  if (/^(messages|reply_quote|reply_enabled)\./.test(path)) { clearTimeout(templatePreviewTimer); templatePreviewTimer = setTimeout(renderTemplatePreview, 120); }
});
configFields.addEventListener("change", updateDirtyState);
scopeSelect.addEventListener("change", () => changeScope(scopeSelect.value));
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-open]"); if (!button) return;
  $("#tab-" + button.dataset.open).click();
  if (button.dataset.scopeFocus) scopeSelect.focus();
  if (button.dataset.importFocus) { const details = document.querySelector(".maintenance-card"); details.open = true; details.scrollIntoView({ block: "start" }); }
});
function searchConfig() {
  const search = $("#config-search").value.trim(); if (!search) return;
  const path = [...fieldCards.keys()].find((key) => search === `${configLabel(key)} · ${key}` || search === key) || [...fieldCards.keys()].find((key) => `${configLabel(key)} ${key}`.includes(search));
  if (path) revealField(path); else showNotice("没有找到匹配配置项。", "error");
}
$("#config-search").addEventListener("change", searchConfig);
$("#config-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); searchConfig(); } });


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
    $("#catalog-sync-banner").textContent = "尚无仓库对比数据";
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
      if (card.isConnected) preview.textContent = "暂无图片预览";
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
  if (!await confirmAction(`将软删除 ${id}。\n影响：${JSON.stringify(impact)}\n继续吗？`)) return;
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

const QUERY_COLUMNS = {
  relationships: ["id", "snapshot_name", "owner_id", "mode", "slot_kind", "state", "acquired_at"],
  intimacy: ["source_id", "target_id", "score", "updated_at"],
  activity: ["user_id", "score", "messages", "active_days", "first_observed", "last_observed"],
  invites: ["snapshot_name", "sender_id", "recipient_id", "state", "expires_at"],
  history: ["id", "kind", "actor_id", "mode", "result_code", "created_at", "data"],
};
const COLUMN_LABELS = { id: "ID", kind: "操作", result_code: "结果代码", snapshot_name: "角色/群友", owner_id: "持有者", mode: "玩法", slot_kind: "槽位", state: "状态", acquired_at: "获得时间", source_id: "发起用户", target_id: "目标用户", score: "分数", updated_at: "更新时间", user_id: "用户", messages: "消息数", active_days: "活跃天数", first_observed: "首次记录", last_observed: "最近记录", sender_id: "赠送者", recipient_id: "接收者", expires_at: "到期时间", action: "操作", actor_id: "操作者", created_at: "记录时间", data: "详情" };
function dataTable(records, columns) {
  const table = document.createElement("table"); table.className = "data-table";
  const rows = records.map((item) => item && typeof item === "object" ? item : { 值: item });
  const keys = columns?.filter((key) => rows.some((row) => Object.hasOwn(row, key))) || [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const head = document.createElement("thead"); const header = document.createElement("tr");
  for (const key of keys) { const th = document.createElement("th"); th.scope = "col"; th.textContent = COLUMN_LABELS[key] || key; header.append(th); }
  head.append(header); table.append(head);
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const key of keys) {
      const td = document.createElement("td"); const value = row[key];
      td.textContent = value === null || value === undefined ? "—" : typeof value === "object" ? JSON.stringify(value, null, 2) : typeof value === "number" && /(_at|_observed)$/.test(key) ? new Date(value * 1000).toLocaleString() : String(value);
      tr.append(td);
    }
    body.append(tr);
  }
  table.append(body); return table;
}

async function runQuery(query, offset = 0) {
  const sequence = ++queryLoadSequence;
  $("#query-prev").disabled = true; $("#query-next").disabled = true;
  $("#query-table").textContent = "正在查询…";
  $("#query-page").textContent = "查询中…";
  $("#query-result").textContent = "正在查询…";
  try {
    const result = await apiGet(query.endpoint, { ...query.params, offset });
    if (sequence !== queryLoadSequence) return;
    currentQuery = query; queryOffset = offset;
    const data = result.data ?? result;
    $("#query-result").textContent = JSON.stringify(data, null, 2);
    const rows = Array.isArray(data.items) ? data.items : Array.isArray(data) ? data : Object.entries(data).map(([字段, 值]) => ({ 字段, 值 }));
    const host = $("#query-table"); host.replaceChildren();
    const scope = state.scopes.find((item) => String(item.id) === query.params.scope_id);
    const label = document.createElement("p"); label.className = "muted"; label.textContent = `群 ${scope?.group_id || query.params.scope_id} · 机器人 ${scope?.self_id || "—"} · ${query.name}${query.params.q || query.params.user_id || query.params.source_id ? " · 已应用用户过滤" : ""}`; host.append(label);
    if (rows.length) host.append(dataTable(rows, Array.isArray(data.items) ? QUERY_COLUMNS[query.kind] : undefined));
    else host.append(document.createTextNode("暂无记录。"));
    const total = data.total ?? rows.length;
    $("#query-page").textContent = total ? `${offset + 1}–${offset + rows.length} / ${total}` : "暂无记录";
    $("#query-prev").disabled = offset === 0;
    $("#query-next").disabled = !Array.isArray(data.items) || offset + rows.length >= total;
  } catch (error) {
    if (sequence !== queryLoadSequence) return;
    $("#query-table").textContent = "暂无查询结果。";
    $("#query-result").textContent = "";
    $("#query-page").textContent = "";
    showNotice(error.message || "查询失败。", "error");
  }
}

document.querySelectorAll("button[data-query]").forEach((button) => button.addEventListener("click", async () => {
  const scopeId = $("#data-scope").value;
  const userId = $("#data-user").value.trim();
  const mode = $("#data-mode").value;
  const params = { scope_id: scopeId, limit: 50 };
  const kind = button.dataset.query;
  let endpoint = kind;
  try {
    if (!scopeId) throw new Error("先选择一个已记录的群作用域。");
    if (kind === "relationships") { if (userId) params.q = userId; params.mode = mode; }
    else if (kind === "quota") {
      if (!userId) throw new Error("用户配额查询需要填写用户 ID。");
      endpoint = `users/${encodeURIComponent(userId)}/quota`; params.mode = mode;
    } else if (kind === "intimacy") { if (userId) params.source_id = userId; }
    else if (kind === "activity") { params.window_days = $("#data-window").value; if (userId) params.user_id = userId; }
    else if (kind === "invites") params.state = "pending";
    if (["invites", "history"].includes(kind) && userId) throw new Error("此查询接口不支持用户过滤，请清空用户 ID 后按群查询。");
    await runQuery({ endpoint, params, kind, name: button.textContent });
  } catch (error) { showNotice(error.message, "error"); }
}));
$("#query-prev").addEventListener("click", () => { if (currentQuery) runQuery(currentQuery, Math.max(0, queryOffset - 50)); });
$("#query-next").addEventListener("click", () => { if (currentQuery) runQuery(currentQuery, queryOffset + 50); });
for (const id of ["data-scope", "data-user", "data-mode", "data-window"]) $("#" + id).addEventListener("input", () => {
  queryLoadSequence += 1; currentQuery = null;
  $("#query-prev").disabled = true; $("#query-next").disabled = true;
  $("#query-table").textContent = "查询条件已改变，请重新查询。"; $("#query-page").textContent = ""; $("#query-result").textContent = "";
});

$("#data-scope").addEventListener("change", () => {
  state.dataScopeId = $("#data-scope").value;
  state.correctionPlan = null;
  $("#correction-commit").disabled = true;
  state.periodResetPlan = null;
  if ($("#period-reset-dialog").open) $("#period-reset-dialog").close();
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
    $("#correction-commit").disabled = true;
    showNotice(error.message || "纠错预检失败。", "error");
  }
});
$("#correction-commit").addEventListener("click", async () => {
  const plan = state.correctionPlan;
  if (!plan) return;
  if (!await confirmAction(`将执行 ${plan.action}。\n修改前：${JSON.stringify(plan.before)}\n修改后：${JSON.stringify(plan.after)}\n继续吗？`)) return;
  $("#correction-commit").disabled = true;
  try {
    const result = await apiPost("admin/commit", {
      preflight_id: plan.preflight_id, expected_revision: plan.config_revision, request_id: crypto.randomUUID(),
    });
    $("#correction-result").textContent = JSON.stringify(result.data || result, null, 2);
    state.correctionPlan = null;
    showNotice("纠错已提交。", "success");
  } catch (error) {
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
    $("#import-report").textContent = "尚无导入预检数据。";
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
  if (!await confirmAction(`提交角色包 ${plan.pack_id} ${plan.pack_version}。同 ID 未选择的条目将跳过；图片策略：${strategy}。继续吗？`)) return;
  $("#import-commit").disabled = true;
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
    $("#maintenance-report").textContent = "尚无恢复预检数据。";
    showNotice(error.message || "备份预检失败。", "error");
  } finally {
    button.disabled = false;
  }
});

$("#restore-commit").addEventListener("click", async () => {
  const plan = state.restorePlan;
  if (!plan || state.busy || !await resolveDraft()) return;
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
  if (!await confirmAction(`将用 ${plan.backup_created_at} 的备份覆盖插件数据库和素材。恢复前会自动备份当前数据。备份表数据量：\n${counts}\n确定继续？`)) return;
  const button = $("#restore-commit");
  button.disabled = true;
  setBusy(true);
  try {
    const response = await apiPost("restore/commit", {
      job_id: plan.job_id, expected_revision: plan.expected_revision, confirm: true,
      scope_mapping, request_id: plan.request_id ??= crypto.randomUUID(),
    });
    const data = response.data || response;
    state.restorePlan = null;
    renderMaintenanceJob(data, "恢复已完成，旧数据自动备份已保留");
    setBusy(false);
    await refreshAll();
    showNotice("备份恢复完成。", "success");
  } catch (error) {
    showNotice(error.message || "恢复失败。", "error");
  } finally {
    button.disabled = !state.restorePlan;
    setBusy(false);
    updateDirtyState();
  }
});

initBatchImages({ apiGet, apiPost, apiUpload, showNotice,
  onImported: () => loadCatalog(),
  onDefaultsSaved: async () => {
    if (!state.busy && !state.pendingSubmission && !state.saveNeedsReload && !isConfigDirty()) await loadConfig();
    else { showNotice("全局默认值已独立保存，配置版本已变化；当前草稿或提交状态保留，请核对保存状态与版本。", "success"); }
  },
});

await bridge.ready();
bridge.onContext(() => {
  document.title = bridge.t("pages.manage.title", "今日姻缘 · 管理");
});
await refreshAll();
checkCatalogSync(false).catch((error) => {
  $("#catalog-sync-banner").textContent = "尚无仓库对比数据";
  showNotice(error.message || "仓库更新检查失败，可稍后手动重查。", "error");
});
