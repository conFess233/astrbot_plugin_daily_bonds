import { initBatchImages } from "./batch-images.js";
import { confirmAction } from "./confirm-dialog.js";
import { bindDropZone, initUploadCompression, prepareUploadImage, runLimited } from "./upload-images.js";
import { initStatusAndMigration } from "./status-migration.js";
import { applyIcons, icon } from "./icons.js";
import { toast, busyButton, countUp, flash, nudge, staggerRows, islandPill, openLightbox } from "./motion.js";

import { configLeaves, draftValues, toUiConfig } from "./config-model.js";
import { checkCatalogSync, editPool, newPool, openCharacterSheet, removeCatalogItem, renderCharacterImages, renderCharacters, renderPools, setCatalogWide, updateCatalogSyncSelection } from "./catalog-workspace.js";
import { RENDER_KINDS, apiConfigPayload, assignDraftField, beforeUnload, changeScope, configFeedback, configIsGlobalOnly, describeConfigConflict, dirtyPaths, focusConfigSearch, isConfigDirty, loadScopes, openView, previewDraft, propagateMode, refreshPoolPickers, renderChoice, renderConfigFields, renderDraftDiff, renderRulePreview, renderScopeList, renderTemplatePreview, resolveDraft, saveDraft, searchConfig, selectConfigGroup, setScopeLabel, showConfigError, unifiablePaths, updateConditionalFields, updateConfigSummary, updateReplySelection, validateDraft } from "./config-workspace.js";
import { QUERY_COLUMNS, clearPeriodResetPlan, correctionExamples, dataTable, previewPeriodReset, renderImportPreview, renderMaintenanceJob, renderRestoreScopeMappings } from "./data-workspace.js";

initUploadCompression();
applyIcons();

export const bridge = window.AstrBotPluginView;
export const $ = (selector) => document.querySelector(selector);
let activeRequests = 0;
let progressTimer;

async function withProgress(request) {
  activeRequests += 1;
  if (activeRequests === 1) {
    progressTimer = window.setTimeout(() => { $("#loading-progress").classList.add("on"); }, 150);
  }
  try {
    return await request();
  } finally {
    activeRequests -= 1;
    if (activeRequests === 0) {
      window.clearTimeout(progressTimer);
      $("#loading-progress").classList.remove("on");
    }
  }
}

export const state = {
  scopes: [],
  scopeId: "global",
  savedConfig: null,
  globalConfig: null,
  savedOverride: {},
  templateFields: {},
  draftValues: {},
  overridePaths: new Set(),
  view: "overview",
  configView: "settings",
  scrollPositions: {},
  selectedTemplate: "",
  selectedReplies: new Set(),
  pendingSubmission: null,
  saveNeedsReload: false,
  configGroup: "basic",
  renderCustom: null,
  unifiedModes: null,
  revision: 0,
  globalRevision: 0,
  busy: false,
  characters: [],
  characterOffset: 0,
  // 角色列表筛选：pool 为空表示全部、"-" 表示未加入角色池。
  characterFilter: { pool: "", gender: "", status: "" },
  characterStats: null,
  characterTotal: 0,
  catalogBusy: false,
  catalogSyncBusy: false,
  pools: [],
  characterRevision: 0,
  characterImages: [],
  characterSaved: null,
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

export async function apiGet(endpoint, params) {
  return withProgress(async () => normalizeApiResponse(await bridge.apiGet(endpoint, params)));
}

export async function apiPost(endpoint, body) {
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

export const configFields = $("#config-fields");
export const configTabs = $("#config-tabs");
export const scopeSelect = $("#scope-select");
export const imagePreviewCache = new Map();
let configLoadSequence = 0;
let catalogLoadSequence = 0;
let characterLoadSequence = 0;
let searchTimer;
let overviewLoadSequence = 0;
let queryLoadSequence = 0;
let queryOffset = 0;
let currentQuery = null;
export const fieldCards = new Map();
let beforeUnloadActive = false;

// 通知统一走悬浮按钮上方的深色通知条；error 类型保留更久并可手动关闭。
export function showNotice(message, kind = "success", options = {}) {
  return toast(message, kind === "error" ? "bad" : "ok", options);
}

export function setBusy(value) {
  state.busy = value;
  const locked = value || !!state.pendingSubmission || state.saveNeedsReload || !state.savedConfig;
  $("#config-controls").disabled = locked;
  for (const button of [$("#refresh"), $("#discard"), $("#preview"), $("#save"), scopeSelect]) button.disabled = value;
  scopeSelect.disabled = locked;
  $("#preview").disabled = locked;
  $("#discard").disabled = locked || !isConfigDirty();
  $("#save").disabled = value || !state.savedConfig;
}


export function updateDirtyState() {
  renderRulePreview();
  const paths = dirtyPaths();
  const dirty = paths.length > 0;
  const visible = dirty || !!state.pendingSubmission || state.saveNeedsReload;
  // 有草稿时悬浮按钮变形为“放弃 / N 项未保存 / 保存”胶囊，是唯一保存入口。
  setDraftPill(visible);
  if (!visible) $("#save-error").hidden = true;
  $("#dirty-indicator").textContent = state.pendingSubmission ? "提交结果待确认" : state.saveNeedsReload ? "已保存，待重新读取" : `${paths.length} 项未保存`;
  $("#save").lastChild.textContent = state.pendingSubmission ? "重试提交" : state.saveNeedsReload ? "重新读取" : "保存";
  $("#discard").hidden = !!state.saveNeedsReload;
  renderDraftDiff(paths);
  for (const [path, card] of fieldCards) {
    card.classList.toggle("modified", paths.includes(path));
    const badge = card.querySelector(".field-source");
    if (badge) badge.textContent = state.scopeId === "global" ? "" : configIsGlobalOnly(path) ? "仅全局设置" : state.overridePaths.has(path) ? "本群覆盖" : "继承全局，编辑即覆盖";
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


$("#scope-search").addEventListener("input", renderScopeList);


export const CONFIG_OPTIONS = {
  "access.groups.mode": [["unrestricted", "不限制"], ["blacklist", "黑名单"], ["whitelist", "白名单"]],
  "access.users.mode": [["unrestricted", "不限制"], ["blacklist", "黑名单"], ["whitelist", "白名单"]],
  "render.mode": [["card", "卡片（t2i 渲染）"], ["text", "纯文本"]],
  ...Object.fromEntries(["draw", "status", "help", "list", "rank", "affection", "collection"].map((kind) =>
    [`render.types.${kind}`, [["inherit", "跟随默认形式"], ["card", "卡片"], ["text", "纯文本"]]])),
};
export const FLOAT_CONFIG_PATHS = new Set([
  "weights.base", "weights.per_intimacy_point", "weights.maximum",
  "weights.activity_floor",
  ...["wife", "husband", "member"].flatMap((mode) => [`modes.${mode}.steal_intimacy_bonus`, `modes.${mode}.steal_probability_max`]),
]);
export const CONFIG_HINTS = {
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
  "display.rank_merge_rows": "1～100；列表每张卡片最多显示的行数，排行每张最多 50 行（5 列）；超出时分成多张。",
  "render.mode": "卡片由 AstrBot 的 t2i 服务渲染，会把群名片、头像和角色图发送到该服务；选择纯文本则不发送。渲染失败时自动改发文字。",
  ...Object.fromEntries(["draw", "status", "help", "list", "rank", "affection", "collection"].map((kind) =>
    [`render.types.${kind}`, "跟随默认形式，或为这一类回复单独选择卡片或纯文本。"])),
  ...Object.fromEntries(["wife", "husband"].flatMap((mode) => [
    [`modes.${mode}.designated_limit`, "0～100；0 关闭指定。每周期重置，失败不扣次数；指定得到的关系占用持有上限，当天受保护。"],
    [`modes.${mode}.designated_unique`, "开启时，已有有效关系的角色不能再次指定；关闭允许同一人或多人重复指定。只影响后续指定，已有关系保留。"],
  ])),
  ...Object.fromEntries(["wife", "husband", "member"].flatMap((mode) => [
    [`modes.${mode}.capacity`, "1～100；所有来源（抽取、指定、抢夺、赠送）共用此上限，同时是每周期普通抽取次数。"],
    [`modes.${mode}.steal_intimacy_bonus`, "0～1；成功率 = 基础成功率 + 抢夺者对持有者的亲密度 × 此值。0 关闭加成。"],
    [`modes.${mode}.steal_probability_max`, "0～1；只限制加成后的成功率，基础成功率更高时以基础成功率为准。"],
  ])),
};


export const placeIslandPill = islandPill($(".island"));
window.addEventListener("load", () => placeIslandPill(true));


// 回复形式：全部卡片 / 全部文字 / 按类型；只有“按类型”才展开默认形式与 7 类单独设置。
export function renderFormField() {
  const row = document.createElement("div"); row.className = "config-field config-compound render-form";
  const lbl = document.createElement("div"); lbl.className = "lbl";
  const name = document.createElement("label"); name.textContent = "回复形式"; lbl.append(name);
  const ctl = document.createElement("div"); ctl.className = "ctl";
  const choices = document.createElement("div"); choices.className = "segmented"; choices.setAttribute("role", "group"); choices.setAttribute("aria-label", "回复形式");
  const detail = document.createElement("div"); detail.className = "render-detail";
  const sync = () => {
    const choice = renderChoice();
    for (const button of choices.children) button.setAttribute("aria-pressed", String(button.dataset.render === choice));
    detail.hidden = choice !== "custom";
  };
  for (const [value, text] of [["card", "全部卡片"], ["text", "全部文字"], ["custom", "按类型"]]) {
    const button = document.createElement("button"); button.type = "button"; button.dataset.render = value; button.textContent = text;
    button.addEventListener("click", () => {
      if (value === "custom") state.renderCustom = true;
      else {
        state.renderCustom = false;
        assignDraftField("render.mode", value);
        for (const kind of RENDER_KINDS) if (state.draftValues[`render.types.${kind}`] !== "inherit") assignDraftField(`render.types.${kind}`, "inherit");
      }
      sync(); flash(row);
    });
    choices.append(button);
  }
  const modeCard = fieldCards.get("render.mode");
  if (modeCard) { modeCard.classList.add("compound-item"); modeCard.querySelector(".lbl label").textContent = "默认形式"; detail.append(modeCard); }
  for (const kind of RENDER_KINDS) {
    const card = fieldCards.get(`render.types.${kind}`); if (!card) continue;
    card.classList.add("compound-item"); detail.append(card);
  }
  const note = document.createElement("small"); note.className = "config-note";
  note.textContent = CONFIG_HINTS["render.mode"];
  ctl.append(choices, detail, note);
  row.append(lbl, ctl);
  row.addEventListener("change", sync);
  renderFormSync = sync;
  sync();
  return row;
}
export let renderFormSync = () => {};


async function loadOverview() {
  const sequence = ++overviewLoadSequence;
  const scopeId = $("#overview-scope").value;
  const grid = $("#overview-cards");
  if (grid.dataset.scope !== scopeId) { grid.replaceChildren(); $("#overview-tasks").replaceChildren(); $("#period-summary").textContent = ""; $("#overview-updated").textContent = ""; }
  grid.dataset.scope = scopeId;
  // 首次读取用骨架占位，数据返回后替换。
  if (!grid.children.length) {
    for (let index = 0; index < 4; index += 1) {
      const card = document.createElement("article"); card.className = "stat"; card.setAttribute("aria-hidden", "true");
      card.innerHTML = '<span class="sk" style="width:40%"></span><span class="sk"></span>'; grid.append(card);
    }
  }
  $("#overview-refresh").disabled = true;
  $("#overview-updated").textContent = "正在读取统计…";
  try {
    const result = await apiGet("overview", scopeId ? { scope_id: scopeId } : {});
    if (sequence !== overviewLoadSequence) return;
    const data = result.data;
    const scope = state.scopes.find((item) => String(item.id) === scopeId);
    const range = scopeId ? `群 ${scope?.group_id || scopeId}` : "全部群";
    // 统计卡：标签 + 数值；范围写在标签右侧，故障数大于 0 时用危险色。
    const metrics = [
      ...(!scopeId ? [["已记录群", data.scopes, "个"]] : []),
      ["当前有效关系", data.active_relationships, "段"],
      ["待处理赠送", data.pending_invites, "条"],
      ["已录入角色", data.catalog?.characters, "个"],
      ...(scopeId ? [["引用角色池", data.enabled_pool_count, "个"]] : []),
      ["通知故障", data.failed_notifications, "条"],
    ];
    grid.replaceChildren();
    for (const [label, value, unit] of metrics) {
      const card = document.createElement("article"); card.className = "stat";
      card.classList.toggle("bad", label === "通知故障" && value > 0);
      const title = document.createElement("div"); title.className = "label";
      const name = document.createElement("span"); name.textContent = label;
      const where = document.createElement("span"); where.textContent = label === "已录入角色" ? "全局" : range;
      title.append(name, where);
      const number = document.createElement("div"); number.className = "value";
      const digits = document.createElement("span"); digits.textContent = value ?? "—"; number.append(digits);
      if (value !== undefined && value !== null) { const small = document.createElement("small"); small.textContent = unit; number.append(small); countUp(digits, Number(value)); }
      card.append(title, number); grid.append(card);
    }
    const period = data.current_period;
    const time = (value) => new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short", timeZone: period.timezone }).format(new Date(value * 1000));
    $("#period-summary").textContent = !scopeId ? `当前周期数量 ${data.current_periods ?? "未获取"} · 选择一个群查看周期起止时间与重置安排。` : period ? `当前周期 ${period.sequence} · ${time(period.starts_at)} – ${time(period.ends_at)} · ${period.timezone} ${period.reset_time}` : "该群还没有已创建的当前周期。";
    const tasks = $("#overview-tasks"); tasks.replaceChildren();
    for (const [label, count] of [[`${range}待处理赠送邀请`, data.pending_invites], [`${range}通知故障`, data.failed_notifications], ["全局维护任务故障", data.failed_jobs]]) {
      const line = document.createElement("p"); line.textContent = `${label}：${count ?? "未获取"}`; tasks.append(line);
    }
    if (data.pending_invites > 0) {
      const button = document.createElement("button"); button.type = "button"; button.className = "btn btn-ghost"; button.textContent = "查询赠送邀请";
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
    if (grid.querySelector(".sk")) grid.replaceChildren();
    showNotice(`统计读取失败：${error.message || "请重试"}。${grid.children.length ? "保留上次成功数据。" : "当前没有可显示的数据。"}`, "error");
    $("#overview-updated").textContent = grid.children.length ? `上次数据 ${grid.dataset.refreshed || "时间未知"} · 待刷新` : "尚无统计数据";
  } finally { if (sequence === overviewLoadSequence) $("#overview-refresh").disabled = false; }
}

export async function loadConfig(scopeId = state.scopeId) {
  const sequence = ++configLoadSequence;
  const result = await apiGet("config", scopeId === "global" ? {} : { scope_id: scopeId });
  if (sequence !== configLoadSequence) return;
  const shown = toUiConfig(scopeId === "global" ? result.data.global : result.data.effective);
  const values = draftValues(shown);
  state.scopeId = scopeId;
  scopeSelect.value = scopeId;
  state.globalRevision = result.revision.global;
  state.revision = scopeId === "global" ? result.revision.global : result.revision.scope;
  state.savedConfig = structuredClone(shown);
  state.globalConfig = toUiConfig(result.data.global);
  state.savedOverride = toUiConfig(result.data.override || {});
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

// 标题下的连接状态：最近一次读取是否成功，以及已记录的群数。
function setConnection(ok) {
  $("#connection-dot").className = `dot${ok ? "" : " bad"}`;
  $("#connection-text").textContent = ok ? `已连接 · ${state.scopes.length} 个群` : "读取失败，可点击右上角刷新重试";
}

async function refreshAll() {
  if (state.busy || state.pendingSubmission) return;
  setBusy(true);
  try {
    try { await loadScopes(); setConnection(true); } catch (error) { setConnection(false); showNotice(error.message || "群列表读取失败。", "error"); }
    await loadOverview();
    try { state.pools = (await apiGet("pools")).data; renderPools(); } catch (error) { showNotice(error.message || "角色池读取失败。", "error"); }
    await loadConfig();
  } catch (error) { configFeedback(error.message || "读取配置失败，可重试；已有草稿仍保留。"); }
  finally { setBusy(false); updateDirtyState(); }
}


let afterSave = null;
export async function finishSave() {
  const task = afterSave; afterSave = null;
  if (task) await task();
}

export async function submitDraft() {
  try {
    if (state.saveNeedsReload) { await loadConfig(); return true; }
    if (!state.pendingSubmission) {
      const { draft, override } = validateDraft();
      await apiPost("config/preview", apiConfigPayload({ scope_id: state.scopeId, draft, ...(state.scopeId === "global" ? {} : { override }) }));
      state.pendingSubmission = { scope_id: state.scopeId, draft, ...(state.scopeId === "global" ? {} : { override }), expected_revision: state.revision, request_id: crypto.randomUUID() };
    }
    const payload = state.pendingSubmission;
    let result;
    try { result = await apiPost("config/save", apiConfigPayload(payload)); }
    catch (error) {
      const code = error.code || error.error?.code || "";
      if (/CONFLICT|INVALID|DENIED|FORBIDDEN|UNAUTHORIZED/.test(code) || error.message?.includes("版本冲突")) {
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
    // “已保存”展示完再重新读取，避免胶囊在动画前就被收起。
    afterSave = async () => {
      try { await loadConfig(); configFeedback("配置已保存，立即生效。", "success"); }
      catch { state.saveNeedsReload = true; configFeedback("配置已保存，重新读取失败。当前内容保留，请点击重新读取配置。", "success"); }
    };
    return true;
  } catch (error) { configFeedback(error.message || "配置校验失败，草稿仍保留。"); showConfigError(error.message || ""); return false; }
}


function openModule(module) {
  openView(module);
  if (module === "catalog") loadCatalog().catch((error) => showNotice(error.message, "error"));
}
$(".island").addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-tab]"); if (tab) openModule(tab.dataset.tab);
});
for (const button of document.querySelectorAll(".segmented [data-config-view]")) button.addEventListener("click", () => openView(button.dataset.configView));

// 悬浮按钮的快捷菜单：Enter/空格打开，上下键切换，Esc 关闭并把焦点还给按钮。
const fab = $("#fab");
const fabMenu = $("#fab-menu");
function fabItems() { return [...fabMenu.querySelectorAll("[role=menuitem]")]; }
// 菜单项自下而上依次出现。
fabItems().forEach((item, index, items) => item.style.setProperty("--k", items.length - 1 - index));
fabMenu.inert = true;
function fabMenuOpen() { return fabMenu.classList.contains("on"); }
function openFabMenu() {
  fabMenu.classList.add("on");
  fabMenu.inert = false;
  fab.setAttribute("aria-expanded", "true");
  fabItems()[0].focus();
}
function closeFabMenu(restoreFocus = true) {
  if (!fabMenuOpen()) return;
  fabMenu.classList.remove("on");
  fabMenu.inert = true;
  fab.setAttribute("aria-expanded", "false");
  if (restoreFocus && !fab.hidden) fab.focus();
}
fab.addEventListener("click", () => (fabMenuOpen() ? closeFabMenu() : openFabMenu()));

// 草稿胶囊从圆形按钮展开；收起时等宽度过渡结束再换回圆形按钮。
let pillTimer;
function setDraftPill(visible) {
  const pill = $("#save-bar");
  if (visible === pill.classList.contains("on") && pill.hidden === !visible) return;
  clearTimeout(pillTimer);
  if (visible) {
    closeFabMenu(false);
    fab.hidden = true;
    pill.hidden = false;
    requestAnimationFrame(() => pill.classList.add("on"));
  } else {
    pill.classList.remove("on");
    pillTimer = window.setTimeout(() => { pill.hidden = true; fab.hidden = false; }, 320);
  }
}
fabMenu.addEventListener("keydown", (event) => {
  const items = fabItems(); const current = items.indexOf(document.activeElement);
  if (event.key === "Escape") { event.preventDefault(); closeFabMenu(); return; }
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
  items[next].focus();
});
document.addEventListener("click", (event) => { if (!event.target.closest(".fab-wrap")) closeFabMenu(false); });

fabMenu.addEventListener("click", (event) => {
  const item = event.target.closest("[data-fab]"); if (!item) return;
  closeFabMenu(false);
  const action = item.dataset.fab;
  if (action === "refresh") $("#refresh").click();
  else if (action === "search") focusConfigSearch();
  else if (action === "batch") { openModule("catalog"); $("#batch-images-open").click(); }
  else if (action === "backup") { openModule("maintenance"); $("#backup-create").click(); }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.target.closest("input, textarea, select, [contenteditable]") || document.querySelector("dialog[open]")) return;
  event.preventDefault(); focusConfigSearch();
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
  if (state.unifiedModes && path.startsWith("modes.wife.") && unifiablePaths().includes(path)) propagateMode(path);
  updateDirtyState();
  if (/^(messages|reply_quote|reply_enabled)\./.test(path)) { clearTimeout(templatePreviewTimer); templatePreviewTimer = setTimeout(renderTemplatePreview, 120); }
});
configFields.addEventListener("change", (event) => {
  const path = event.target.dataset?.configPath;
  if (path && !path.startsWith("messages.")) flash(fieldCards.get(path));
  updateDirtyState();
});
scopeSelect.addEventListener("change", () => changeScope(scopeSelect.value));

$("#config-search").addEventListener("change", searchConfig);
$("#config-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); searchConfig(); } });



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


export async function loadCatalog({ refreshPools = true } = {}) {
  const sequence = ++catalogLoadSequence;
  const query = $("#character-search").value.trim();
  const offset = state.characterOffset;
  const [characters, pools] = await Promise.all([
    apiGet("characters", { q: query, limit: 100, offset, ...state.characterFilter }),
    refreshPools ? apiGet("pools") : Promise.resolve(null),
  ]);
  if (sequence !== catalogLoadSequence) return;
  if (offset && !characters.data.items.length && characters.data.total <= offset) {
    state.characterOffset = Math.max(0, Math.floor((characters.data.total - 1) / 100) * 100);
    return loadCatalog({ refreshPools });
  }
  state.characters = characters.data.items;
  state.characterTotal = characters.data.total;
  state.characterStats = characters.data.stats || null;
  if (pools) state.pools = pools.data;
  renderCharacters();
  if (pools) {
    renderPools();
    refreshPoolPickers();
  }
}

export async function loadCharacter(id, afterSave = false) {
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
  // 图片增删即时保存时使用的已保存字段，避免顺带提交表单里未保存的修改。
  state.characterSaved = {
    id: item.id, name: item.name, gender: item.gender, enabled: item.enabled, aliases: item.aliases,
    pool_ids: item.pools.map((pool) => pool.id), provenance: state.characterProvenance,
  };
  $("#delete-character").disabled = false;
  for (const check of $("#character-pools").querySelectorAll("input")) {
    check.checked = item.pools.some((pool) => pool.id === check.value);
  }
  renderCharacterImages();
}

export function newCharacter() {
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
  state.characterSaved = null;
  $("#delete-character").disabled = true;
  for (const check of $("#character-pools").querySelectorAll("input")) check.checked = false;
  renderCharacterImages();
}


export const catalogLayout = $("#catalog-layout");


// 缩略图统一通过灯箱查看，在同一网格内左右翻页。
document.addEventListener("click", (event) => {
  const thumb = event.target.closest(".gallery .thumb[data-hash]");
  if (!thumb || thumb.disabled) return;
  const items = [...thumb.closest(".gallery").querySelectorAll(".thumb[data-hash]:not(:disabled)")];
  openLightbox(items, items.indexOf(thumb));
});
$("#character-expand").addEventListener("click", () => setCatalogWide(!catalogLayout.classList.contains("wide")));
$("#character-full").addEventListener("click", openCharacterSheet);
window.addEventListener("resize", () => { if (window.innerWidth <= 1100 && catalogLayout.classList.contains("wide")) setCatalogWide(false); });

// 角色图：选择后立即显示本地预览与上传状态；已有角色上传完成即保存图片列表。
function uploadTile(file) {
  const tile = document.createElement("div");
  tile.className = "upload-tile";
  tile.title = file.name;
  const url = URL.createObjectURL(file);
  const image = document.createElement("img");
  image.alt = "";
  image.src = url;
  image.addEventListener("error", () => image.remove());
  const label = document.createElement("span");
  label.className = "upload-state";
  label.textContent = "上传中";
  tile.append(image, label);
  return {
    tile,
    fail(message) {
      tile.classList.add("failed");
      label.textContent = message;
      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.className = "thumb-remove";
      dismiss.setAttribute("aria-label", `关闭 ${file.name} 的错误提示`);
      dismiss.innerHTML = icon("x");
      dismiss.addEventListener("click", () => { tile.remove(); URL.revokeObjectURL(url); });
      tile.append(dismiss);
    },
    done() { tile.remove(); URL.revokeObjectURL(url); },
  };
}

async function uploadCharacterImages(entries) {
  await withCatalogWrite(async () => {
    const characterId = $("#character-id").value;
    const host = $("#character-uploads");
    const tiles = entries.map(({ file }) => uploadTile(file));
    host.append(...tiles.map((item) => item.tile));
    const results = await runLimited(entries, 3, async ({ file }) => (await apiUpload("media/upload", file)).data);
    let added = 0;
    results.forEach((result, index) => {
      if (!result.ok) { tiles[index].fail(result.error?.message || "上传失败"); return; }
      tiles[index].done();
      if (!state.characterImages.includes(result.value.hash)) { state.characterImages.push(result.value.hash); added += 1; }
    });
    // 上传期间切换了角色：图片已进入图库，但不归属新打开的角色。
    if ($("#character-id").value !== characterId) return;
    renderCharacterImages();
    const failed = results.filter((result) => !result.ok).length;
    if (!added) {
      if (failed) showNotice(`${failed} 张图片上传失败，原因见图片上的说明。`, "error");
      else showNotice("这些图片已在角色中，没有新增。", "success");
      return;
    }
    const saved = await persistCharacterImages(null);
    const tail = failed ? `；${failed} 张失败，原因见图片上的说明` : "";
    showNotice(`${saved ? "已添加" : "已加入草稿"} ${added} 张图片${tail}${saved ? "" : "，保存角色后生效"}。`, failed ? "error" : "success");
  });
}

/** 已有角色：按加载时的字段保存当前图片列表，不提交表单里尚未保存的其他修改。返回是否已保存。 */
export async function persistCharacterImages(message) {
  const saved = state.characterSaved;
  if (!saved || saved.id !== $("#character-id").value.trim()) return false;
  try {
    const result = await apiPost("characters/save", {
      ...saved,
      image_hashes: state.characterImages,
      expected_revision: state.characterRevision,
      request_id: crypto.randomUUID(),
    });
    state.characterRevision = result.data.revision;
    renderCharacterImages();
    if (message) showNotice(message, "success");
    loadCatalog({ refreshPools: false }).catch(() => {});
    return true;
  } catch (error) {
    showNotice(`图片未保存：${error.message || "角色保存失败"}。可点击“保存角色”重试。`, "error");
    return false;
  }
}

bindDropZone($("#character-drop"), {
  input: $("#character-image"),
  onFiles: uploadCharacterImages,
  isBusy: () => state.catalogBusy,
  pasteScope: $("#character-drop"),
});

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


$("#delete-character").addEventListener("click", () => withCatalogWrite(async () => {
  try { await removeCatalogItem("character", $("#character-id").value, "characters"); }
  catch (error) { showNotice(error.message || "删除失败。", "error"); }
}));


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
// 筛选变化时回到第一页；角色池由 renderCharacters 生成的标签切换。
export function setCharacterFilter(change) {
  Object.assign(state.characterFilter, change);
  state.characterOffset = 0;
  loadCatalog({ refreshPools: false }).catch((error) => showNotice(error.message || "读取角色列表失败。", "error"));
}
$("#gender-filter").addEventListener("click", (event) => {
  const button = event.target.closest("[data-gender]");
  if (!button) return;
  for (const other of $("#gender-filter").querySelectorAll("button")) other.setAttribute("aria-pressed", String(other === button));
  setCharacterFilter({ gender: button.dataset.gender });
});
$("#status-filter").addEventListener("change", (event) => setCharacterFilter({ status: event.target.value }));
$("#new-character-top").addEventListener("click", () => {
  newCharacter();
  $("#character-name").focus();
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
    if (rows.length) { const table = dataTable(rows, Array.isArray(data.items) ? QUERY_COLUMNS[query.kind] : undefined); host.append(table); staggerRows(table.tBodies[0].rows); }
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
      if (!userId) { nudge($("#data-user")); throw new Error("用户配额查询需要填写用户 ID。"); }
      endpoint = `users/${encodeURIComponent(userId)}/quota`; params.mode = mode;
    } else if (kind === "intimacy") { if (userId) params.source_id = userId; }
    else if (kind === "activity") { params.window_days = $("#data-window").value; if (userId) params.user_id = userId; }
    else if (kind === "invites") params.state = "pending";
    if (["invites", "history"].includes(kind) && userId) throw new Error("此查询接口不支持用户过滤，请清空用户 ID 后按群查询。");
    for (const other of document.querySelectorAll("button[data-query]")) other.setAttribute("aria-pressed", String(other === button));
    const name = button.textContent;
    await busyButton(button, "查询中", () => runQuery({ endpoint, params, kind, name }));
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


$("#period-reset-mode").addEventListener("change", clearPeriodResetPlan);


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

initStatusAndMigration({ apiGet, apiPost, apiUpload, confirmAction, showNotice });

await bridge.ready();
await refreshAll();
checkCatalogSync(false).catch((error) => {
  $("#catalog-sync-banner").textContent = "尚无仓库对比数据";
  showNotice(error.message || "仓库更新检查失败，可稍后手动重查。", "error");
});
