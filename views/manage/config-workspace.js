// 配置模块：草稿、条件显示、玩法/消息/高级三类编辑区、改动列表与保存胶囊。
import { $, CONFIG_HINTS, CONFIG_OPTIONS, FLOAT_CONFIG_PATHS, apiGet, apiPost, configFields, configTabs, fieldCards, finishSave, loadConfig, placeIslandPill, renderFormField, renderFormSync, scopeSelect, setBusy, showNotice, state, submitDraft, updateDirtyState } from "./app.js";
import { changedPaths, configAt, configLeaves, displayValue, fromUiConfig, serializeDraft, templateParts, toUiConfig } from "./config-model.js";
import { confirmAction } from "./confirm-dialog.js";
import { dataTable } from "./data-workspace.js";
import { icon } from "./icons.js";
import { nudge } from "./motion.js";

export function dirtyPaths() {
  return state.savedConfig ? changedPaths(state.savedConfig, state.savedOverride, state.draftValues, state.overridePaths, state.scopeId !== "global") : [];
}

export function beforeUnload(event) {
  event.preventDefault();
  event.returnValue = "";
}

// 次级设置的前置条件：[控制项, 判断, 收起时的说明]。全部满足才显示，数值始终保留。
function fieldConditions(path) {
  const parts = path.split(".");
  const value = (target) => state.draftValues[target];
  if (parts[0] === "modes") {
    const at = (key) => `modes.${parts[1]}.${key}`;
    const key = parts[2];
    const list = [];
    if ((key.startsWith("steal_") && key !== "steal_enabled") || key === "stolen_limit") list.push([at("steal_enabled"), () => value(at("steal_enabled")) !== false, "抢夺已关闭"]);
    if (key === "steal_probability_max") list.push([at("steal_intimacy_bonus"), () => Number(value(at("steal_intimacy_bonus"))) > 0, "未开启亲密度加成"]);
    if (key.startsWith("gift_") && key !== "gift_enabled") list.push([at("gift_enabled"), () => value(at("gift_enabled")) !== false, "赠送已关闭"]);
    if (key === "gift_timeout_seconds") list.push([at("gift_mode"), () => value(at("gift_mode")) === "confirm", "直接赠送，无需确认超时"]);
    if (key === "divorce_limit") list.push([at("divorce_enabled"), () => value(at("divorce_enabled")) !== false, "离婚已关闭"]);
    if (key === "designated_unique") list.push([at("designated_limit"), () => Number(value(at("designated_limit"))) > 0, "指定次数为 0，已关闭指定"]);
    return list;
  }
  const simple = {
    "access.groups.ids": ["access.groups.mode", (mode) => mode !== "unrestricted"],
    "access.users.ids": ["access.users.mode", (mode) => mode !== "unrestricted"],
    "display.page_size": ["display.pagination_enabled", (on) => on !== false],
    "statistics.activity_window_days": ["statistics.activity_enabled", (on) => on !== false],
  }[path];
  return simple ? [[simple[0], () => simple[1](value(simple[0])), ""]] : [];
}

function failedCondition(path) {
  return fieldConditions(path).find(([, test]) => !test());
}

export function updateConditionalFields() {
  for (const [path, card] of fieldCards) {
    const failed = failedCondition(path);
    card.classList.toggle("cond-hidden", !!failed);
    const note = card.parentElement?.querySelector(":scope > .conditional-note");
    if (note) { note.hidden = !failed; note.textContent = failed?.[2] || ""; }
  }
  // 整行在所有玩法都收起时隐藏；整组都收起时显示一句说明。
  for (const row of configFields.querySelectorAll(".setting-matrix tbody tr")) {
    const cards = [...row.querySelectorAll(".config-field")];
    row.hidden = cards.length > 0 && cards.every((card) => card.classList.contains("cond-hidden"));
  }
  for (const details of configFields.querySelectorAll(".mode-details")) {
    const rows = [...details.querySelectorAll(".setting-matrix tbody tr")];
    const closed = rows.length > 0 && rows.every((row) => row.hidden);
    details.querySelector(".matrix-scroll").hidden = closed;
    const empty = details.querySelector(".group-closed");
    if (empty) { empty.hidden = !closed; empty.textContent = `${details.dataset.modeGroup.replace(/（.*）/, "")}的前置开关在所有玩法都已关闭，开启后显示这里的设置。`; }
  }
}

export function setScopeLabel() {
  const scope = state.scopes.find((item) => String(item.id) === state.scopeId);
  const label = scope ? `群 ${scope.group_id} · 机器人 ${scope.self_id}` : "全局配置";
  $("#editor-title").textContent = label;
}

// 保存失败的原因同时显示在胶囊上方，靠近唯一的保存入口。
export function configFeedback(message, kind = "error") {
  const error = $("#save-error");
  if (!message) { error.hidden = true; return undefined; }
  if (kind === "error" && !$("#save-bar").hidden) { error.textContent = message; error.hidden = false; }
  else if (kind !== "error") error.hidden = true;
  return showNotice(message, kind);
}

// 配置项所在的配置分类：玩法、消息与关键词或高级。
function viewOfPath(path) {
  if (path.startsWith("modes.")) return "settings";
  if (/^(messages|reply_quote|reply_enabled)\./.test(path) || path.startsWith("commands.keywords.")) return "replies";
  return "advanced";
}

function diffText(path, value) {
  if (value === undefined || value === null) return "继承全局";
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  const choices = CONFIG_OPTIONS[path] || (path.endsWith(".gift_mode") ? [["direct", "直接赠送"], ["confirm", "等待确认"]] : null);
  const choice = choices?.find(([key]) => key === value);
  if (choice) return choice[1];
  if (isPoolConfig(path)) {
    const ids = String(value).split("\n").filter(Boolean);
    return ids.map((id) => state.pools.find((pool) => pool.id === id)?.name || id).join("、") || "（未选择）";
  }
  const text = Array.isArray(value) ? value.join("、") : String(value).replaceAll("\n", "、");
  if (!text.trim()) return "（空）";
  return Array.from(text).length > 36 ? `${Array.from(text).slice(0, 36).join("")}…` : text;
}

function savedValue(path) {
  const value = configAt(state.savedConfig, path);
  return value === undefined ? undefined : displayValue(path, value);
}

// 右侧卡片列出全部改动：中文字段名、原值（删除线）、新值；点击跳到对应配置项。
export function renderDraftDiff(paths) {
  const list = $("#draft-diff");
  const previous = new Set([...list.children].map((item) => item.dataset.key));
  list.replaceChildren();
  $("#draft-title").textContent = state.pendingSubmission ? "提交结果待确认" : paths.length ? `${paths.length} 项改动待保存` : "没有未保存的改动";
  $("#draft-empty").hidden = paths.length > 0;
  const entries = [];
  for (const path of paths) {
    const mode = path.startsWith("modes.") ? MODE_NAMES[path.split(".")[1]] : "";
    const inherited = state.scopeId !== "global" && !configIsGlobalOnly(path) && !state.overridePaths.has(path);
    entries.push({ path, mode, label: configLabel(path), from: diffText(path, savedValue(path)), to: inherited ? "继承全局" : diffText(path, state.draftValues[path]) });
  }
  // 统一数值时，三种玩法相同的改动合并成一条“三种玩法 · 字段”。
  const merged = [];
  for (const entry of entries) {
    const key = entry.path.startsWith("modes.") ? entry.path.split(".")[2] : "";
    const twin = state.unifiedModes && key && !PER_MODE_KEYS.has(key) && merged.find((item) => item.key === key && item.from === entry.from && item.to === entry.to);
    if (twin) { twin.count += 1; continue; }
    merged.push({ ...entry, key, count: 1 });
  }
  for (const entry of merged) {
    const path = entry.path;
    const item = document.createElement("li");
    item.dataset.key = `${entry.count > 1 ? "all" : path}|${entry.to}`;
    if (previous.has(item.dataset.key)) item.classList.add("settled");
    const button = document.createElement("button");
    button.type = "button";
    const name = document.createElement("span"); name.className = "name";
    name.textContent = entry.count > 1 ? `三种玩法 · ${entry.label}` : entry.mode ? `${entry.mode} · ${entry.label}` : entry.label;
    const from = document.createElement("span"); from.className = "from"; from.textContent = entry.from;
    const to = document.createElement("span"); to.className = "to"; to.textContent = entry.to;
    button.append(name, from, to);
    button.addEventListener("click", () => revealField(path));
    item.append(button);
    list.append(item);
  }
  const badge = $("#nav-config-badge");
  const badgeWasHidden = badge.hidden;
  badge.textContent = String(paths.length);
  badge.hidden = !paths.length;
  if (badgeWasHidden !== badge.hidden) {
    if (!badge.hidden) { badge.classList.remove("pop"); void badge.offsetWidth; badge.classList.add("pop"); }
    placeIslandPill();
  }
  const views = new Set(paths.map(viewOfPath));
  for (const button of document.querySelectorAll(".segmented [data-config-view]")) button.querySelector(".mod").hidden = !views.has(button.dataset.configView);
  const groups = new Set(paths.filter((path) => viewOfPath(path) === "advanced").map(advancedGroup));
  for (const tab of configTabs.querySelectorAll("[data-config-group]")) tab.classList.toggle("modified", groups.has(tab.dataset.configGroup));
}

export function updateConfigSummary() {
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
    line.textContent = `${name} · ${on ? "已启用" : "已关闭"}（${source}）· 持有上限 ${state.draftValues[`modes.${mode}.capacity`]} · ${dirtyPaths().some((item) => item.startsWith(`modes.${mode}.`)) ? "含草稿" : "已保存"}`;
    host.append(line);
  }
  $("#mode-summary").textContent = `已启用玩法 ${enabled}/3 · ${dirtyPaths().length} 项未保存`;
}

export async function loadScopes() {
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
  overviewScope.replaceChildren(new Option("全部群", ""));
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

export function renderScopeList() {
  const list = $("#scope-list");
  list.replaceChildren();
  const search = $("#scope-search").value.trim().toLowerCase();
  const scopes = state.scopes.filter((scope) => `${scope.group_id} ${scope.self_id} ${scope.platform_id}`.toLowerCase().includes(search));
  for (const scope of scopes.slice(0, 50)) {
    const item = document.createElement("div");
    item.className = "scope-item";
    const label = document.createElement("span");
    label.textContent = `群 ${scope.group_id} · 机器人 ${scope.self_id}`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-ghost btn-sm";
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

const CONFIG_GROUPS = {
  schema_version: "配置版本", enabled: "总开关", access: "群与名单", reset: "每日重置",
  commands: "指令与触发", "modes.wife": "今日老婆", "modes.husband": "今日老公",
  "modes.member": "娶群友", statistics: "统计", weights: "抽取权重",
  display: "展示与排行", members: "群成员缓存", history: "历史记录", resources: "资源限制",
  "messages.results": "群内操作结果", "messages.errors": "群内错误提示",
  "messages.notifications": "赠送与重启通知", "messages.titles": "标题与统计标签",
  "reply_quote.results": "结果回复引用", "reply_quote.errors": "错误回复引用",
  batch_import: "批量图片检测默认值", render: "回复形式",
};

const MESSAGE_LABELS = {
  draw_wife: "抽到老婆", draw_husband: "抽到老公", draw_member: "抽到群友",
  draw_member_mutual: "群友双向奔赴", my_status: "我的姻缘", help: "姻缘帮助", my_collection: "我的图鉴",
  status: "我的姻缘标题", collection: "我的图鉴标题",
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
  rank_intimacy: "全群亲密度排行回复", rank_my_intimacy: "我的亲密度排行回复", rank_activity: "活跃度排行回复", query_affection: "好感度查询回复",
  member_reconcile: "成员资格变化通知", member_redraw: "群友离开补抽说明",
  owner_ineligible: "持有人失去资格说明", participant_ineligible: "赠送参与者失去资格说明",
};

const TITLE_LABELS = {
  list_wife: "老婆列表标题", list_husband: "老公列表标题", list_member: "群友列表标题",
  existing: "已有关系标题", rank_intimacy: "我的亲密度排行标题",
  rank_group_intimacy: "全群亲密度排行标题", rank_activity: "活跃度排行标题",
  query_affection: "好感度查询标题", page: "分页标题", empty: "空榜文案",
  rank_summary: "排行摘要", intimacy_value: "亲密度数值标签", activity_value: "活跃度数值标签",
  affection_value: "有向好感度标签", normal_slot: "抽取来源标签", gift_slot: "赠送来源标签", steal_slot: "抢夺来源标签",
  designated_slot: "指定槽位标签", wife: "老婆玩法名称", husband: "老公玩法名称", member: "群友玩法名称",
};

const CONFIG_LABELS = {
  schema_version: "配置格式版本", enabled: "启用", mode: "名单模式", ids: "名单 ID",
  male_keywords: "男类检测关键词", female_keywords: "女类检测关键词",
  male_pool_id: "默认男卡池 ID", female_pool_id: "默认女卡池 ID",
  extra_bot_ids: "补充机器人 QQ 号", extra_admin_ids: "额外命令管理员 QQ 号", timezone: "时区", time: "重置时间",
  allow_bare: "允许裸关键词", allow_leading_bot_mention: "允许开头 @机器人",
  allow_host_prefix: "允许宿主命令前缀", extra_prefixes: "额外命令前缀",
  cooldown_seconds: "通用冷却（秒）", capacity: "持有上限", steal_enabled: "允许抢夺",
  designated_limit: "每周期指定次数", designated_unique: "指定角色只能拥有一段关系",
  rank_merge_rows: "每张卡片最多行数",
  steal_intimacy_bonus: "每点亲密度增加成功率", steal_probability_max: "亲密度加成后的成功率上限",
  types: "按类型设置", draw: "抽取结果", status: "我的姻缘", help: "姻缘帮助", list: "关系列表",
  rank: "排行", affection: "好感度", collection: "我的图鉴",
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
  intimacy_rank_enabled: "显示亲密度排行",
  activity_rank_enabled: "显示活跃度排行", cache_ttl_seconds: "缓存时间（秒）",
  retention_days: "历史保留天数", http_timeout_seconds: "HTTP 超时（秒）",
  image_max_bytes: "单图大小上限（字节）", image_max_pixels: "单图像素上限",
  import_max_bytes: "导入包大小上限（字节）", import_max_entries: "导入条目上限",
  import_max_expanded_bytes: "导入解压上限（字节）",
  text_chunk_chars: "文字分片字符数",
  pending_invites_per_user: "每人待处理邀请上限",
  avatar_cache_ttl_seconds: "头像缓存时间（秒）", avatar_cache_max_entries: "头像缓存人数上限",
  dedupe_retention_days: "事件去重保留天数", backup_keep_count: "备份保留数量",
  download_concurrency: "并发下载数",
  draw_wife: "抽老婆", draw_husband: "抽老公", draw_member: "娶群友",
  steal_wife: "抢老婆", steal_husband: "抢老公", steal_member: "抢群友",
  gift_wife: "送老婆", gift_husband: "送老公", gift_member: "送群友",
  divorce_character: "离婚", divorce_wife: "离婚老婆", divorce_husband: "离婚老公",
  divorce_member: "踹群友", list_characters: "老婆列表", list_husband: "老公列表", list_members: "群友列表",
  rank_intimacy: "亲密度排行（全群）", rank_my_intimacy: "我的亲密度排行", rank_activity: "活跃度排行",
  gift_accept: "接受赠送", gift_reject: "拒绝赠送", gift_cancel: "取消赠送", query_affection: "好感度查询",
  my_status: "我的姻缘", my_collection: "我的图鉴",
};

function configGroup(path) {
  if (path === "schema_version") return "enabled";
  const parts = path.split(".");
  if (["messages", "reply_quote", "reply_enabled"].includes(parts[0])) return parts.slice(0, 2).join(".");
  return parts[0] === "modes" ? parts.slice(0, 2).join(".") : parts[0];
}

export function configIsGlobalOnly(path) {
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
  if (path === "render.mode") return "默认回复形式";
  if (parts[0] === "render" && parts[1] === "types") return `${name}回复形式`;
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

export function refreshPoolPickers() {
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
  // 两列配置项：左侧名称与“已修改”标记，右侧控件、来源与帮助；不显示配置键名。
  const card = document.createElement("div");
  card.className = "config-field";
  const labelColumn = document.createElement("div");
  labelColumn.className = "lbl";
  const title = document.createElement("label");
  title.textContent = configLabel(path);
  const controls = document.createElement("div");
  controls.className = "ctl";
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
    if (path.endsWith(".steal_probability") || path.endsWith(".designated_limit")) { input.min = "0"; input.max = "100"; }
    if (path.endsWith(".steal_intimacy_bonus") || path.endsWith(".steal_probability_max")) { input.min = "0"; input.max = "1"; }
    if (path === "display.rank_merge_rows") { input.min = "1"; input.max = "100"; }
  }
  input.dataset.configPath = path;
  input.id = `field-${path.replaceAll(".", "-")}`;
  input.setAttribute("aria-label", configLabel(path));
  title.htmlFor = input.id;
  const control = document.createElement("span");
  control.className = input.type === "checkbox" ? "toggle-control" : "input-control";
  control.append(input);
  if (input.type === "checkbox") { const status = document.createElement("span"); status.className = "toggle-state"; control.append(status); }
  const source = document.createElement("small");
  source.className = "field-source";
  labelColumn.append(title, source);
  controls.append(control);
  card.append(labelColumn, controls);
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
    controls.append(picker);
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
    controls.append(override);
    card.classList.toggle("inherited", !overridden);
  } else if (state.scopeId !== "global" || path === "schema_version") {
    input.disabled = true;
    const badge = document.createElement("small");
    badge.className = "config-note";
    badge.textContent = path === "schema_version" ? "只读" : "仅全局设置";
    controls.append(badge);
  }
  writeInputValue(input, state.draftValues[path]);
  if (isPoolConfig(path)) renderPoolPicker(card, input);
  const hint = CONFIG_HINTS[path] ||
    (path.startsWith("messages.") ? templateHint(path) :
    path.startsWith("reply_quote.") ? "开启后，这条即时回复会引用触发消息；关闭则不引用，不改变消息文案。" :
    path.startsWith("commands.keywords.") ? "每行一个别名，至少一项。" : "");
  if (hint) {
    const help = document.createElement("small");
    help.className = "config-note";
    help.textContent = hint;
    help.id = `${input.id}-help`;
    controls.append(help);
  }
  const error = document.createElement("small");
  error.className = "config-error";
  error.hidden = true;
  error.id = `${input.id}-error`;
  input.setAttribute("aria-describedby", `${hint ? input.id + "-help " : ""}${error.id}`);
  controls.append(error);
  fieldCards.set(path, card);
  return card;
}

export function renderConfigFields() {
  const previousEditor = fieldCards.get(state.selectedTemplate)?.querySelector("textarea");
  const ui = {
    mode: configFields.querySelector(".modes-workspace")?.dataset.mode || "wife",
    expanded: [...configFields.querySelectorAll(".mode-details[open]")].map((item) => item.dataset.modeGroup),
    search: $("#template-search")?.value || "", category: $("#template-category")?.value || "全部",
    listScroll: $("#template-list")?.scrollTop || 0,
    selection: previousEditor ? [previousEditor.selectionStart, previousEditor.selectionEnd] : null,
  };
  fieldCards.clear();
  configFields.replaceChildren();
  configTabs.replaceChildren();
  $("#side-context").replaceChildren();
  if (!state.savedConfig) return;
  for (const [path, value] of configLeaves(state.savedConfig)) createConfigField(path, value);
  syncUiFlags(state.uiScope !== state.scopeId);
  state.uiScope = state.scopeId;
  renderModesWorkspace();
  renderRepliesWorkspace();
  renderAdvancedWorkspace();
  renderConfigSearch();
  $(".modes-workspace").dataset.mode = ui.mode;
  for (const button of document.querySelectorAll("[data-mode-choice]")) button.setAttribute("aria-pressed", String(button.dataset.modeChoice === ui.mode));
  for (const details of configFields.querySelectorAll(".mode-details")) details.open = ui.expanded.includes(details.dataset.modeGroup);
  $("#template-search").value = ui.search; $("#template-category").value = ui.category;
  // 旧版分类名已经不存在时回到“全部”，避免目录为空。
  if (!$("#template-category").value) $("#template-category").value = "全部";
  filterTemplates(); $("#template-list").scrollTop = ui.listScroll;
  if (ui.selection && state.selectedTemplate) fieldCards.get(state.selectedTemplate).querySelector("textarea").setSelectionRange(...ui.selection);
  openView(state.view);
  updateDirtyState();
}

// 悬浮岛模块；配置模块内再分玩法、消息与关键词、高级三类。
const MODULE_PANELS = { overview: "overview-panel", config: "settings-panel", catalog: "catalog-panel", data: "data-panel", maintenance: "maintenance-panel" };

const CONFIG_VIEWS = ["settings", "replies", "advanced"];

const VIEW_TITLES = { overview: "概览", config: "配置", catalog: "角色", data: "数据", maintenance: "备份与迁移" };

export function openView(view) {
  if (view === "config") view = state.configView || "settings";
  const module = CONFIG_VIEWS.includes(view) ? "config" : view;
  if (!MODULE_PANELS[module]) return;
  const previousView = state.view;
  if (view !== previousView) state.scrollPositions[previousView] = window.scrollY;
  state.view = view;
  if (module === "config") state.configView = view;
  document.body.dataset.view = view;
  for (const button of document.querySelectorAll(".island [data-tab]")) {
    if (button.dataset.tab === module) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  placeIslandPill();
  for (const [name, id] of Object.entries(MODULE_PANELS)) $(`#${id}`).hidden = name !== module;
  for (const button of document.querySelectorAll(".segmented [data-config-view]")) button.setAttribute("aria-pressed", String(button.dataset.configView === view));
  configTabs.hidden = view !== "advanced";
  for (const section of configFields.querySelectorAll("[data-config-view]")) section.hidden = section.dataset.configView !== view;
  for (const section of $("#side-context").querySelectorAll("[data-side-view]")) section.hidden = section.dataset.sideView !== view;
  if (view === "advanced") selectConfigGroup(state.configGroup);
  document.title = `今日姻缘 · ${VIEW_TITLES[module]}`;
  if (view !== previousView) window.scrollTo({ top: state.scrollPositions[view] || 0, behavior: "instant" });
}

// 右侧卡片在每个配置分类下显示对应的上下文（规则预览、消息预览或说明）。
function sideSection(view) {
  const section = document.createElement("div");
  section.dataset.sideView = view;
  $("#side-context").append(section);
  return section;
}

function configView(view, className = "") {
  const section = document.createElement("div");
  section.dataset.configView = view;
  section.className = className;
  configFields.append(section);
  return section;
}

const MODE_NAMES = { wife: "今日老婆", husband: "今日老公", member: "娶群友" };

const COMMON_MODE_KEYS = ["capacity", "steal_enabled", "steal_probability", "steal_attempt_limit"];

// 统一数值时不跟随的字段：玩法开关与角色池各玩法独立。
const PER_MODE_KEYS = new Set(["enabled", "pool_ids"]);

const MODE_ROWS = {
  enabled: ["玩法启用", "控制该玩法是否可用"],
  capacity: ["持有上限", "所有来源共用，也是每周期普通抽取次数", "个"],
  steal_enabled: ["抢夺启用", "是否允许抢夺"],
  steal_probability: ["抢夺成功率", "单次有效抢夺的基础概率", "%"],
  steal_attempt_limit: ["抢夺次数", "每周期尝试上限，0 为不限", "次"],
};

function modeMatrix(keys, unifiable = true) {
  const wrap = document.createElement("div");
  wrap.className = "matrix-scroll";
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", "三玩法参数对照，可横向滚动");
  const table = document.createElement("table");
  table.className = `setting-matrix${unifiable ? " unifiable" : ""}`;
  const head = document.createElement("thead");
  const headers = document.createElement("tr");
  for (const [index, title] of ["配置项", ...Object.values(MODE_NAMES)].entries()) {
    const th = document.createElement("th"); th.scope = "col"; th.textContent = title;
    // 统一数值时只显示第一列，表头改为“三种玩法”。
    if (index === 1 && unifiable) { const all = document.createElement("span"); all.className = "unified-title"; all.textContent = "三种玩法（统一）"; th.append(all); }
    headers.append(th);
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
        const note = document.createElement("small"); note.className = "conditional-note"; note.hidden = true; cell.append(note);
      } else { cell.className = "muted not-applicable"; cell.textContent = "— 此玩法不适用"; }
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
  const switcher = document.createElement("div"); switcher.className = "mode-switcher segmented"; switcher.setAttribute("role", "group"); switcher.setAttribute("aria-label", "窄屏玩法切换");
  for (const [key, name] of [...Object.entries(MODE_NAMES), ["all", "对照"]]) {
    const button = document.createElement("button"); button.type = "button"; button.textContent = name; button.dataset.modeChoice = key; button.setAttribute("aria-pressed", String(key === "wife"));
    button.addEventListener("click", () => { view.dataset.mode = key; for (const other of switcher.children) other.setAttribute("aria-pressed", String(other === button)); }); switcher.append(button);
  }
  view.dataset.unified = String(!!state.unifiedModes);
  // 玩法开关：三种玩法各自独立。
  const toggles = document.createElement("section"); toggles.className = "card card-pad mode-toggles";
  toggles.innerHTML = '<div class="card-title"><div><h3>启用的玩法</h3><p>关闭玩法不会清空已配置的数值。</p></div></div>';
  const toggleRow = document.createElement("div"); toggleRow.className = "mode-toggle-row";
  for (const [mode, name] of Object.entries(MODE_NAMES)) {
    const card = fieldCards.get(`modes.${mode}.enabled`); if (!card) continue;
    card.querySelector(".lbl label").textContent = name; toggleRow.append(card);
  }
  toggles.append(toggleRow);
  const common = document.createElement("section"); common.className = "card card-pad common-modes";
  const head = document.createElement("div"); head.className = "card-title";
  head.innerHTML = "<div><h3>常用参数</h3><p></p></div>";
  head.querySelector("p").textContent = state.unifiedModes ? "三种玩法使用同一组数值，修改会同时写入老婆、老公和群友。" : "三种玩法并排对照，修改先进入草稿。";
  const unify = document.createElement("label"); unify.className = "toggle-control unify-toggle";
  const unifyInput = document.createElement("input"); unifyInput.type = "checkbox"; unifyInput.setAttribute("role", "switch"); unifyInput.checked = !!state.unifiedModes;
  unifyInput.setAttribute("aria-label", "三种玩法使用相同数值");
  const unifyText = document.createElement("span"); unifyText.className = "toggle-state"; unifyText.textContent = "三种玩法使用相同数值";
  unify.append(unifyInput, unifyText); head.append(unify);
  unifyInput.addEventListener("change", async (event) => {
    event.stopPropagation();
    if (unifyInput.checked) {
      const differing = unifiablePaths().filter((path) => !sameAcrossModes(path));
      if (differing.length && !await confirmAction(`将以“今日老婆”的数值统一另外两种玩法：共 ${differing.length} 项不同，改动只进入草稿，保存前可放弃。`)) { unifyInput.checked = false; return; }
      state.unifiedModes = true;
      for (const path of differing) propagateMode(path);
    } else state.unifiedModes = false;
    view.dataset.unified = String(state.unifiedModes);
    head.querySelector("p").textContent = state.unifiedModes ? "三种玩法使用同一组数值，修改会同时写入老婆、老公和群友。" : "三种玩法并排对照，修改先进入草稿。";
    updateDirtyState();
  });
  common.append(head, modeMatrix(COMMON_MODE_KEYS)); main.prepend(toggles, common); main.prepend(switcher);
  const allKeys = [...new Set(Object.values(state.savedConfig.modes).flatMap((mode) => Object.keys(mode)))].filter((key) => !COMMON_MODE_KEYS.includes(key) && key !== "enabled");
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
    const empty = document.createElement("p"); empty.className = "group-closed muted"; empty.hidden = true;
    details.append(summary, modeMatrix(keys, !keys.some((key) => PER_MODE_KEYS.has(key))), empty);
    if (name === "角色池与候选") {
      const link = document.createElement("button"); link.type = "button"; link.className = "btn btn-ghost btn-sm"; link.textContent = "查看候选资格与统计权重";
      link.addEventListener("click", () => { openView("advanced"); selectConfigGroup("stats"); }); details.append(link);
    }
    main.append(details);
  }
  const help = sideSection("settings");
  help.innerHTML = '<h4>规则说明</h4><ul class="rule-help"><li>抽取、指定、抢夺和赠送得到的关系共用一个持有上限；抢来的和指定的关系当天受保护。</li><li>抢夺、被抢和离婚次数为 0 表示不限；指定次数为 0 表示关闭指定。</li><li>关闭某项功能后，它的次级设置会收起，数值保留。</li></ul><div class="divider"></div>';
  const preview = document.createElement("section"); preview.className = "rule-preview"; preview.id = "rule-preview";
  preview.setAttribute("aria-live", "polite");
  help.append(preview);
  view.append(main);
  renderRulePreview();
}

// 统一数值涉及的“今日老婆”字段：至少两种玩法都有，且不是各玩法独立的开关或角色池。
export function unifiablePaths() {
  return [...fieldCards.keys()].filter((path) => {
    const [root, mode, key] = path.split(".");
    return root === "modes" && mode === "wife" && !PER_MODE_KEYS.has(key) && ["husband", "member"].some((other) => fieldCards.has(`modes.${other}.${key}`));
  });
}

function sameAcrossModes(path) {
  const key = path.split(".")[2];
  const value = JSON.stringify(state.draftValues[path]);
  return ["husband", "member"].every((mode) => !fieldCards.has(`modes.${mode}.${key}`) || JSON.stringify(state.draftValues[`modes.${mode}.${key}`]) === value);
}

export function propagateMode(path) {
  const key = path.split(".")[2];
  for (const mode of ["husband", "member"]) {
    const target = `modes.${mode}.${key}`;
    if (fieldCards.has(target) && JSON.stringify(state.draftValues[target]) !== JSON.stringify(state.draftValues[path])) assignDraftField(target, state.draftValues[path]);
  }
}

// 仅在首次读取或切换范围时根据数据推断界面模式，之后保留使用者的选择。
function syncUiFlags(scopeChanged) {
  if (scopeChanged || state.unifiedModes === null) state.unifiedModes = fieldCards.size ? unifiablePaths().every(sameAcrossModes) : null;
  const mode = state.draftValues["render.mode"];
  const mixed = RENDER_KINDS.some((kind) => { const value = state.draftValues[`render.types.${kind}`]; return value !== undefined && value !== "inherit" && value !== mode; });
  if (scopeChanged || state.renderCustom === null || mixed) state.renderCustom = mixed;
}

// 用当前草稿说明一位用户一天内的额度，帮助核对数值组合是否符合预期。
export function renderRulePreview() {
  const host = document.getElementById("rule-preview");
  if (!host || !state.savedConfig) return;
  const value = (path) => state.draftValues[path];
  const heading = document.createElement("h4"); heading.textContent = "规则预览（使用草稿）";
  const list = document.createElement("ul");
  for (const [mode, name] of Object.entries(MODE_NAMES)) {
    const item = document.createElement("li");
    if (value(`modes.${mode}.enabled`) === false) { item.textContent = `${name}：已关闭`; list.append(item); continue; }
    const capacity = value(`modes.${mode}.capacity`);
    const parts = [`最多同时持有 ${capacity} 段`, `每周期普通抽取 ${capacity} 次，离婚可补抽`];
    const designated = value(`modes.${mode}.designated_limit`);
    if (designated !== undefined) parts.push(Number(designated) ? `可指定 ${designated} 次` : "不可指定");
    if (value(`modes.${mode}.steal_enabled`)) {
      const attempts = Number(value(`modes.${mode}.steal_attempt_limit`));
      parts.push(`${attempts ? `抢夺 ${attempts} 次` : "抢夺不限次数"}，成功率 ${value(`modes.${mode}.steal_probability`)}%`);
    } else parts.push("不可抢夺");
    item.textContent = `${name}：${parts.join("；")}。`;
    list.append(item);
  }
  host.replaceChildren(heading, list);
}

// 回复词按使用场景分组：同一场景的成功结果与错误提示相邻。
const TEMPLATE_CATEGORIES = ["抽取与指定", "抢夺", "赠送", "离婚", "列表与统计", "我的姻缘与帮助", "指令与通用提示", "通知", "标题标签"];

function templateCategory(path) {
  const key = path.split(".").at(-1);
  if (path.startsWith("messages.notifications.")) return "通知";
  if (path.startsWith("messages.titles.")) return "标题标签";
  if (/^(my_status|help|my_collection)$/.test(key)) return "我的姻缘与帮助";
  if (/steal|stolen|target_protected|no_target_relationship/.test(key)) return "抢夺";
  if (/gift|invite/.test(key)) return "赠送";
  if (/divorc/.test(key)) return "离婚";
  if (/rank|list|affection|activity|page/.test(key)) return "列表与统计";
  if (/draw|designated|capacity|admin_set|already_held|no_candidate|relationship_locked/.test(key)) return "抽取与指定";
  return "指令与通用提示";
}

function templateOrder(path) {
  const section = path.split(".")[1];
  return TEMPLATE_CATEGORIES.indexOf(templateCategory(path)) * 10 + ["results", "errors", "notifications", "titles"].indexOf(section);
}

function renderRepliesWorkspace() {
  const view = configView("replies");
  view.innerHTML = '<div class="reply-tabs segmented" role="group" aria-label="消息与关键词"><button type="button" id="reply-templates-tab" aria-pressed="true">回复模板</button><button type="button" id="reply-keywords-tab" aria-pressed="false">指令关键词</button></div><div id="reply-templates" class="replies-workspace"><section class="card template-directory"><div class="card-title"><div><h3>模板目录</h3><p id="template-total"></p></div><button type="button" class="btn btn-ghost btn-sm reply-directory-back" id="reply-directory-back">返回编辑</button></div><label class="field"><span>搜索模板</span><input id="template-search" type="search" placeholder="名称或文案" /></label><label class="field"><span>分类</span><select id="template-category"></select></label><div id="template-list" class="template-list"></div></section><div class="reply-edit-area"><section id="reply-editor-panel" class="card"><div class="card-title"><div><h3 id="template-name"></h3><p id="template-count"></p></div><button type="button" class="btn btn-ghost btn-sm reply-directory-toggle" id="reply-directory-toggle">模板目录</button></div><div id="reply-editor"></div><h4>插入变量</h4><div id="template-variables" class="variable-buttons"></div><div id="reply-quote"></div></section></div></div><section id="reply-keywords" class="card card-pad" hidden><div class="card-title"><div><h3>指令关键词</h3><p>每行一个别名，冲突由服务端检查；与回复模板共享同一配置草稿。</p></div></div><div class="config-rows"></div></section><div id="reply-field-store" hidden></div>';
  const side = sideSection("replies");
  side.innerHTML = '<div id="reply-preview-panel"><h4>消息预览</h4><p class="preview-label">使用草稿 · 模拟预览，不会实际发送</p><div class="chat-preview"><div class="chat-user">示例用户消息</div><div id="template-preview" class="chat-bubble"></div></div><p class="side-note">演示数据；实际图片、引用和消息样式以 QQ 客户端为准。</p></div>';
  sideSection("advanced").innerHTML = '<div class="notice"><p>群级覆盖只保存改动的字段；“仅全局设置”的项需切到全局配置修改。保存后立即生效，无需重载插件。</p></div>';
  const store = $("#reply-field-store");
  for (const [path, card] of fieldCards) {
    if (/^(messages|reply_quote|reply_enabled)\./.test(path)) store.append(card);
    if (path.startsWith("commands.keywords.")) $("#reply-keywords .config-rows").append(card);
  }
  const categories = ["全部", ...TEMPLATE_CATEGORIES];
  for (const category of categories) $("#template-category").add(new Option(category, category));
  $("#template-search").addEventListener("input", filterTemplates);
  $("#template-category").addEventListener("change", filterTemplates);
  for (const kind of ["templates", "keywords"]) $(`#reply-${kind}-tab`).addEventListener("click", () => showReplySection(kind));
  $("#reply-directory-toggle").addEventListener("click", () => { $(".replies-workspace").dataset.directory = "true"; });
  $("#reply-directory-back").addEventListener("click", () => { $(".replies-workspace").dataset.directory = "false"; });
  initReplyBulk();
  filterTemplates();
  if (!fieldCards.has(state.selectedTemplate)) state.selectedTemplate = [...fieldCards.keys()].find((path) => path.startsWith("messages."));
  if (state.selectedTemplate) selectTemplate(state.selectedTemplate);
}

export function assignDraftField(path, raw, inherited = false) {
  const card = fieldCards.get(path); if (!card || state.scopeId !== "global" && configIsGlobalOnly(path)) return false;
  const input = card.querySelector("[data-config-path]");
  if (inherited) { const flag = card.querySelector("[data-override-path]"); if (!flag) return false; flag.checked = false; flag.dispatchEvent(new Event("change", { bubbles: true })); }
  else { writeInputValue(input, raw); if (isPoolConfig(path)) renderPoolPicker(card, input); input.dispatchEvent(new Event("input", { bubbles: true })); }
  return true;
}

export function updateReplySelection() {
  const summary = $("#reply-selection-count"); if (!summary) return;
  const selected = [...state.selectedReplies].filter((path) => fieldCards.has(path));
  summary.textContent = `已选 ${selected.length} 条回复（含其他分类）· 只修改草稿`;
  for (const button of document.querySelectorAll("[data-reply-bulk]")) button.disabled = !selected.length || button.dataset.replyBulk === "inherit" && state.scopeId === "global";
}

function initReplyBulk() {
  const bulk = document.createElement("details"); bulk.className = "reply-bulk";
  bulk.innerHTML = '<summary>批量调整回复</summary><div class="actions"><button type="button" class="btn btn-ghost btn-sm" data-select-replies="visible">全选当前分类</button><button type="button" class="btn btn-ghost btn-sm" data-select-replies="all">全选所有回复</button><button type="button" class="btn btn-ghost btn-sm" data-select-replies="none">清空选择</button></div><div class="actions"><button type="button" class="btn btn-ghost btn-sm" data-reply-bulk="enable">开启回复</button><button type="button" class="btn btn-ghost btn-sm" data-reply-bulk="disable">关闭回复</button><button type="button" class="btn btn-ghost btn-sm" data-reply-bulk="quote">开启引用</button><button type="button" class="btn btn-ghost btn-sm" data-reply-bulk="unquote">关闭引用</button><button type="button" class="btn btn-ghost btn-sm" data-reply-bulk="inherit">选中回复恢复继承</button></div><p id="reply-selection-count" class="hint" role="status"></p>';
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
  const visible = paths.filter((path) => (category === "全部" || templateCategory(path) === category) && `${path} ${configLabel(path)} ${state.draftValues[path]}`.toLowerCase().includes(search))
    .sort((left, right) => templateOrder(left) - templateOrder(right));
  $("#template-total").textContent = `显示 ${visible.length} / 共 ${paths.length} 条`;
  for (const path of visible) {
    const button = document.createElement("button"); button.type = "button"; button.dataset.templatePath = path;
    button.className = "template-item";
    button.classList.toggle("modified", dirtyPaths().includes(path));
    button.setAttribute("aria-pressed", String(path === state.selectedTemplate));
    const name = document.createElement("strong"); name.textContent = configLabel(path);
    const kind = { results: "结果", errors: "错误", notifications: "通知", titles: "标签" }[path.split(".")[1]] || "";
    const group = document.createElement("small"); group.textContent = `${templateCategory(path)} · ${kind}`;
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
  renderTemplatePreview();
}

export function renderTemplatePreview() {
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
      } else host.append(document.createTextNode(part.text ?? ({ name: "示例角色", names: "示例角色", subject_name: "示例角色", actor_name: "小明", owner_name: "小红", sender_name: "小明", recipient_name: "小红", reset_at: "明天 00:00", target_name: "示例群友", group_name: "示例群", 群名: "示例群", days: "7", accept_keyword: "接受赠送", invite_id: "示例邀请" }[part.variable] || `示例${part.variable}`)));
    }
  } catch { host.textContent = "请校验并修改模板后查看预览。"; }
}

// 高级配置合并为 5 个分组；“运行与存储”的子项默认折叠，一般无需修改。
const ADVANCED_GROUPS = [
  ["basic", "基础与准入", ["enabled", "access", "reset"]],
  ["commands", "指令", ["commands"]],
  ["stats", "统计与权重", ["statistics", "weights"]],
  ["display", "展示与回复形式", ["render", "display"]],
  ["system", "运行与存储", ["members", "history", "resources", "batch_import"]],
];

const COLLAPSED_GROUPS = new Set(["system"]);

const TRIGGER_PATHS = ["commands.allow_bare", "commands.allow_leading_bot_mention", "commands.allow_host_prefix", "commands.extra_prefixes"];

export const RENDER_KINDS = ["draw", "status", "help", "list", "rank", "affection", "collection"];

function advancedGroup(path) {
  const key = configGroup(path);
  return ADVANCED_GROUPS.find(([, , keys]) => keys.includes(key))?.[0] || "basic";
}

function renderAdvancedWorkspace() {
  const view = configView("advanced");
  const subsections = new Map();
  for (const [id, title, keys] of ADVANCED_GROUPS) {
    const section = document.createElement("section"); section.className = "config-group card card-pad"; section.dataset.group = id;
    section.id = `config-group-${id}`;
    const head = document.createElement("div"); head.className = "card-title";
    const titleBox = document.createElement("div");
    const heading = document.createElement("h3"); heading.textContent = title; titleBox.append(heading);
    if (COLLAPSED_GROUPS.has(id)) { const note = document.createElement("p"); note.textContent = "资源上限、缓存与保留期限，一般保持默认即可；展开对应分类修改。"; titleBox.append(note); }
    head.append(titleBox); section.append(head); view.append(section);
    const button = document.createElement("button"); button.type = "button"; button.dataset.configGroup = id;
    button.id = `config-tab-${id}`; button.setAttribute("role", "tab"); button.setAttribute("aria-controls", section.id); button.textContent = title;
    section.setAttribute("role", "tabpanel"); section.setAttribute("aria-labelledby", button.id); configTabs.append(button);
    for (const key of keys) {
      let container;
      if (COLLAPSED_GROUPS.has(id)) {
        container = document.createElement("details"); container.className = "config-sub";
        const summary = document.createElement("summary"); summary.textContent = CONFIG_GROUPS[key] || key; container.append(summary);
      } else {
        container = document.createElement("div"); container.className = "config-sub";
        // 回复形式只有一项合并设置，名称已在行内，不再加小标题。
        if (keys.length > 1 && key !== "render") { const label = document.createElement("h4"); label.textContent = CONFIG_GROUPS[key] || key; container.append(label); }
      }
      const rows = document.createElement("div"); rows.className = "config-rows"; container.append(rows);
      section.append(container); subsections.set(key, rows);
    }
  }
  for (const [path, card] of fieldCards) {
    if (path === "schema_version" || path.startsWith("modes.") || /^(messages|reply_quote|reply_enabled)\./.test(path) || path.startsWith("commands.keywords.")) continue;
    if (TRIGGER_PATHS.includes(path) || path.startsWith("render.")) continue;
    subsections.get(configGroup(path))?.append(card);
  }
  // 触发方式：四个相关开关与前缀合成一项。
  const trigger = compoundField("触发方式", "群消息以哪些形式触发指令；全部关闭时只能通过额外前缀触发。", TRIGGER_PATHS);
  subsections.get("commands").prepend(trigger);
  subsections.get("render").append(renderFormField());
  for (const [, rows] of subsections) if (!rows.children.length) rows.parentElement.remove();
  if (!ADVANCED_GROUPS.some(([id]) => id === state.configGroup)) state.configGroup = ADVANCED_GROUPS[0][0];
  selectConfigGroup(state.configGroup);
}

const COMPOUND_LABELS = {
  "commands.allow_bare": "直接发送关键词（如“今日老婆”）",
  "commands.allow_leading_bot_mention": "消息开头 @机器人",
  "commands.allow_host_prefix": "AstrBot 唤醒前缀（如“/”）",
  "commands.extra_prefixes": "额外前缀，每行一个",
};

// 把几项相关配置放进同一行：左侧一个总名称，右侧逐项列出；每项仍保留自己的覆盖与修改标记。
function compoundField(title, hint, paths) {
  const row = document.createElement("div"); row.className = "config-field config-compound";
  const lbl = document.createElement("div"); lbl.className = "lbl";
  const name = document.createElement("label"); name.textContent = title; lbl.append(name);
  const ctl = document.createElement("div"); ctl.className = "ctl";
  for (const path of paths) {
    const card = fieldCards.get(path); if (!card) continue;
    card.classList.add("compound-item");
    card.querySelector(".lbl label").textContent = COMPOUND_LABELS[path] || configLabel(path);
    ctl.append(card);
  }
  const note = document.createElement("small"); note.className = "config-note"; note.textContent = hint; ctl.append(note);
  row.append(lbl, ctl);
  return row;
}

export function renderChoice() {
  if (state.renderCustom) return "custom";
  return state.draftValues["render.mode"] === "text" ? "text" : "card";
}

function renderConfigSearch() {
  const options = $("#config-search-options"); options.replaceChildren();
  for (const path of fieldCards.keys()) {
    const mode = path.startsWith("modes.") ? MODE_NAMES[path.split(".")[1]] : "";
    const option = document.createElement("option"); option.value = mode ? `${mode} · ${configLabel(path)}` : configLabel(path); options.append(option);
  }
}

function revealField(path) {
  // 被前置开关收起的项：先定位到控制它的开关，并说明原因。
  const failed = failedCondition(path);
  if (failed) {
    showNotice(`“${configLabel(path)}”已收起：${failed[2] || `需先调整“${configLabel(failed[0])}”`}。`);
    return revealField(failed[0]);
  }
  if (path.startsWith("modes.")) {
    const mode = path.split(".")[1];
    // 统一数值时只显示第一列；定位到其他玩法的字段需先切回分列对照。
    if (state.unifiedModes && mode !== "wife" && !PER_MODE_KEYS.has(path.split(".")[2])) { state.unifiedModes = false; $(".modes-workspace").dataset.unified = "false"; const unify = $(".unify-toggle input"); if (unify) unify.checked = false; }
    $(".modes-workspace").dataset.mode = mode;
    for (const button of document.querySelectorAll("[data-mode-choice]")) button.setAttribute("aria-pressed", String(button.dataset.modeChoice === mode));
  }
  const view = path.startsWith("modes.") ? "settings" : /^(messages|reply_quote|reply_enabled)\./.test(path) || path.startsWith("commands.keywords.") ? "replies" : "advanced";
  openView(view);
  if (view === "replies") {
    if (path.startsWith("commands.keywords.")) showReplySection("keywords");
    else { showReplySection("templates"); selectTemplate(path.replace(/^reply_(?:quote|enabled)\./, "messages.")); }
  } else if (view === "advanced") {
    selectConfigGroup(advancedGroup(path));
    if (path.startsWith("render.types.") || path === "render.mode") { state.renderCustom = true; renderFormSync(); }
  }
  const card = fieldCards.get(path);
  const details = card.closest("details"); if (details) details.open = true;
  card.scrollIntoView({ block: "center", behavior: "instant" });
  card.querySelector("[data-config-path]").focus({ preventScroll: true });
}

export function selectConfigGroup(group) {
  state.configGroup = group;
  for (const tab of configTabs.querySelectorAll("[data-config-group]")) {
    const selected = tab.dataset.configGroup === group;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  for (const section of configFields.querySelectorAll(".config-group")) section.hidden = section.dataset.group !== group;
}

// 编辑器内部使用展开后的文案路径，提交前合并为服务端 schema 2 结构。
export function apiConfigPayload(payload) {
  return { ...payload, draft: fromUiConfig(payload.draft), ...(payload.override ? { override: fromUiConfig(payload.override) } : {}) };
}

function serializeConfig() {
  return serializeDraft(state.savedConfig, state.globalConfig, state.draftValues, state.overridePaths, state.scopeId !== "global", FLOAT_CONFIG_PATHS);
}

export function isConfigDirty() { return dirtyPaths().length > 0; }

export function showConfigError(message) {
  for (const error of configFields.querySelectorAll(".config-error")) error.hidden = true;
  const path = [...fieldCards.keys()].sort((a, b) => b.length - a.length).find((candidate) => message.includes(candidate));
  if (!path) return;
  const card = fieldCards.get(path);
  const error = card.querySelector(".config-error");
  error.hidden = true;
  revealField(path);
  const input = card.querySelector("[data-config-path]");
  input.setAttribute("aria-invalid", "true");
  error.textContent = message;
  error.hidden = false;
  nudge(input);
}

export async function describeConfigConflict() {
  const latest = await apiGet("config", state.scopeId === "global" ? {} : { scope_id: state.scopeId });
  const current = toUiConfig(state.scopeId === "global" ? latest.data.global : latest.data.effective);
  const feedback = configFeedback("配置版本冲突。草稿已保留，请核对差异；放弃配置更改可重新读取服务端值。");
  const differences = configLeaves(current).filter(([path, value]) => JSON.stringify(value) !== JSON.stringify(configAt(state.savedConfig, path)))
    .map(([path, value]) => ({ 字段: path, 服务端最新值: value, 当前草稿: state.draftValues[path] }));
  if (differences.length) {
    const details = document.createElement("details"); const summary = document.createElement("summary"); summary.textContent = "查看配置差异"; details.append(summary, dataTable(differences)); feedback.querySelector(".msg").append(details);
  }
  return "配置版本冲突，当前草稿已保留。";
}

export function validateDraft() {
  const serialized = serializeConfig();
  for (const [path, fields] of Object.entries(state.templateFields)) templateParts(configAt(serialized.draft, path), fields, path);
  return serialized;
}

export async function previewDraft() {
  if (state.busy || !state.savedConfig) return;
  setBusy(true);
  try {
    const { draft, override } = validateDraft();
    const result = await apiPost("config/preview", apiConfigPayload({ scope_id: state.scopeId, draft, ...(state.scopeId === "global" ? {} : { override }) }));
    configFeedback(state.scopeId === "global" ? `校验通过，影响 ${result.data.affected_scopes.length} 个已记录群。草稿尚未保存。` : "校验通过，仅作用于所选群；草稿尚未保存。", "success");
  } catch (error) { configFeedback(error.message || "配置校验失败。"); showConfigError(error.message || ""); }
  finally { setBusy(false); updateDirtyState(); }
}

// 保存只经由草稿胶囊：按钮显示“保存中”，成功后变绿“已保存”约 0.6 秒再收起。
export async function saveDraft() {
  if (state.busy || !state.savedConfig) return false;
  const saveButton = $("#save");
  const saved = [...saveButton.childNodes];
  const spinner = document.createElement("span"); spinner.className = "spin";
  saveButton.replaceChildren(spinner, document.createTextNode("保存中"));
  let ok = false;
  setBusy(true);
  try {
    ok = await submitDraft();
    if (ok) {
      $("#save-bar").classList.add("done");
      saveButton.replaceChildren(document.createRange().createContextualFragment(icon("check")), document.createTextNode("已保存"));
      await new Promise((resolve) => window.setTimeout(resolve, 600));
    }
    return ok;
  } finally {
    saveButton.replaceChildren(...saved);
    $("#save-bar").classList.remove("done");
    setBusy(false);
    if (ok) await finishSave();
    updateDirtyState();
  }
}

export async function resolveDraft() {
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

export async function changeScope(nextScope) {
  scopeSelect.value = state.scopeId;
  if (state.busy || !await resolveDraft()) return false;
  setBusy(true);
  try { await loadConfig(nextScope); return true; }
  catch (error) { scopeSelect.value = state.scopeId; showNotice(error.message || "切换范围失败，已保留原草稿。", "error"); return false; }
  finally { setBusy(false); updateDirtyState(); }
}

export function focusConfigSearch() {
  openView("config");
  $("#config-search").focus();
}

export function searchConfig() {
  const search = $("#config-search").value.trim(); if (!search) return;
  const label = (key) => (key.startsWith("modes.") ? `${MODE_NAMES[key.split(".")[1]]} · ` : "") + configLabel(key);
  const path = [...fieldCards.keys()].find((key) => search === label(key)) || [...fieldCards.keys()].find((key) => label(key).includes(search));
  if (path) revealField(path); else showNotice("没有找到匹配配置项。", "error");
}
