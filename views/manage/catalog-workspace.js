// 角色模块：仓库同步、角色与角色池列表、角色编辑卡片、图片网格与灯箱。
import { $, apiGet, apiPost, catalogLayout, imagePreviewCache, loadCatalog, loadCharacter, newCharacter, persistCharacterImages, setCharacterFilter, showNotice, state } from "./app.js";
import { confirmAction } from "./confirm-dialog.js";
import { applyIcons, icon } from "./icons.js";
import { openSheet } from "./motion.js";

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

export function updateCatalogSyncSelection() {
  const count = $("#catalog-sync-items").querySelectorAll("input:checked").length;
  $("#catalog-sync-commit").disabled = state.catalogSyncBusy || count === 0;
  $("#catalog-sync-commit").textContent = count ? `同步所选 ${count} 项` : "同步所选项目";
}

export async function checkCatalogSync(force) {
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

const GENDER_LABELS = { female: "女", male: "男", unspecified: "未指定" };
const MODE_LABELS = { wife: "老婆池", husband: "老公池" };
const COVER_SIZE = 320;

// 封面进入视口后再加载，最多同时请求 4 张，避免一页上百张同时发请求。
const coverQueue = [];
let coversLoading = 0;
function pumpCovers() {
  while (coversLoading < 4 && coverQueue.length) {
    const { hash, image, frame } = coverQueue.shift();
    if (!image.isConnected) continue;
    coversLoading += 1;
    const key = `${hash}@${COVER_SIZE}`;
    if (!imagePreviewCache.has(key)) {
      imagePreviewCache.set(key, apiGet(`media/preview/${encodeURIComponent(hash)}`, { size: COVER_SIZE }).then((response) => response.data.src));
    }
    imagePreviewCache.get(key)
      .then((src) => { image.src = src; frame.classList.remove("loading"); })
      .catch(() => { imagePreviewCache.delete(key); frame.classList.remove("loading"); frame.classList.add("broken"); })
      .finally(() => { coversLoading -= 1; pumpCovers(); });
  }
}
const coverObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    coverObserver.unobserve(entry.target);
    coverQueue.push(entry.target.coverRequest);
  }
  pumpCovers();
}, { rootMargin: "200px" }) : null;

function coverFrame(item) {
  const frame = document.createElement("span");
  frame.className = "character-cover";
  if (!item.cover_hash) {
    frame.classList.add("empty");
    const initial = document.createElement("span");
    initial.className = "cover-initial";
    initial.textContent = (item.name || "?").trim().slice(0, 1);
    frame.append(initial);
    return frame;
  }
  frame.classList.add("loading");
  const image = document.createElement("img");
  image.alt = "";
  image.decoding = "async";
  frame.append(image);
  frame.coverRequest = { hash: item.cover_hash, image, frame };
  if (coverObserver) coverObserver.observe(frame);
  else { coverQueue.push(frame.coverRequest); pumpCovers(); }
  return frame;
}

function tag(text, tone = "plain") {
  const element = document.createElement("span");
  element.className = `chip ${tone}`;
  element.textContent = text;
  return element;
}

function characterCard(item, list) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "character-card";
  if (!item.enabled) button.classList.add("disabled");
  button.setAttribute("aria-pressed", String($("#character-id").value === item.id && $("#character-id").readOnly));
  const info = document.createElement("span");
  info.className = "character-info";
  const name = document.createElement("strong");
  name.textContent = item.name;
  const id = document.createElement("small");
  id.className = "mono";
  id.textContent = item.id;
  const tags = document.createElement("span");
  tags.className = "character-tags";
  tags.append(tag(GENDER_LABELS[item.gender] || item.gender));
  tags.append(item.image_count ? tag(`${item.image_count} 张图`) : tag("缺少图片", "warn"));
  if (!item.enabled) tags.append(tag("已停用", "bad"));
  const pools = document.createElement("small");
  pools.className = "character-pools";
  const names = item.pool_ids.map((poolId) => state.pools.find((pool) => pool.id === poolId)?.name || poolId);
  pools.textContent = names.length ? names.join("、") : "未加入角色池";
  pools.title = pools.textContent;
  info.append(name, id, tags, pools);
  button.append(coverFrame(item), info);
  if (item.aliases?.length) button.title = `别名：${item.aliases.join("、")}`;
  button.addEventListener("click", () => {
    for (const other of list.querySelectorAll(".character-card")) other.setAttribute("aria-pressed", String(other === button));
    loadCharacter(item.id).catch((error) => showNotice(error.message, "error"));
  });
  return button;
}

function poolChip(label, value, count) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "pool-chip";
  button.setAttribute("aria-pressed", String(state.characterFilter.pool === value));
  button.append(document.createTextNode(label));
  if (count !== undefined) {
    const number = document.createElement("span");
    number.className = "pool-count";
    number.textContent = count;
    button.append(number);
  }
  button.addEventListener("click", () => setCharacterFilter({ pool: value }));
  return button;
}

function renderPoolFilter() {
  const host = $("#pool-filter");
  host.replaceChildren(poolChip("全部角色", ""));
  for (const mode of ["wife", "husband"]) {
    const pools = state.pools.filter((pool) => pool.mode === mode);
    if (!pools.length) continue;
    const group = document.createElement("span");
    group.className = "pool-chip-group";
    group.append(Object.assign(document.createElement("span"), { className: "pool-chip-label", textContent: MODE_LABELS[mode] }));
    for (const pool of pools) group.append(poolChip(pool.name, pool.id, pool.character_count));
    host.append(group);
  }
  host.append(poolChip("未加入角色池", "-"));
  // 选中的角色池已被删除：回到全部。
  const current = state.characterFilter.pool;
  if (current && current !== "-" && !state.pools.some((pool) => pool.id === current)) setCharacterFilter({ pool: "" });
}

function renderPoolOverview() {
  const host = $("#pool-overview");
  const pool = state.pools.find((item) => item.id === state.characterFilter.pool);
  const stats = state.characterStats;
  host.hidden = !pool && state.characterFilter.pool !== "-";
  host.replaceChildren();
  if (host.hidden) return;
  const title = document.createElement("div");
  title.className = "pool-overview-title";
  const heading = document.createElement("strong");
  heading.textContent = pool ? pool.name : "未加入任何角色池的角色";
  title.append(heading);
  if (pool) title.append(tag(MODE_LABELS[pool.mode] || pool.mode), tag(pool.id));
  const facts = document.createElement("span");
  facts.className = "muted";
  if (stats) {
    const missing = stats.missing_images ? `，${stats.missing_images} 个缺少图片` : "，都有图片";
    const disabled = stats.disabled ? `，${stats.disabled} 个已停用` : "";
    facts.textContent = `${stats.total} 个角色${missing}${disabled}`;
  }
  host.append(title, facts);
  if (pool) {
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "btn btn-ghost btn-sm";
    edit.textContent = "编辑角色池";
    edit.addEventListener("click", () => {
      editPool(pool);
      $("#pool-name").closest("section").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    host.append(edit);
  }
}

export function renderCharacters() {
  renderPoolFilter();
  renderPoolOverview();
  const list = $("#character-list");
  list.replaceChildren();
  for (const item of state.characters) list.append(characterCard(item, list));
  if (!state.characters.length) {
    const empty = document.createElement("p");
    empty.className = "muted character-empty";
    const filtered = $("#character-search").value.trim() || Object.values(state.characterFilter).some(Boolean);
    empty.textContent = filtered ? "没有符合条件的角色。" : "暂无角色，可新建、同步仓库或导入角色包。";
    list.append(empty);
  }
  const stats = state.characterStats;
  if (stats) {
    const parts = [`${stats.total} 个角色`];
    if (stats.missing_images) parts.push(`${stats.missing_images} 个缺少图片`);
    if (stats.disabled) parts.push(`${stats.disabled} 个已停用`);
    $("#character-summary").textContent = `${parts.join(" · ")}。点击角色在右侧编辑。`;
  }
  const start = state.characterTotal ? state.characterOffset + 1 : 0;
  $("#character-page").textContent = `${start}–${state.characterOffset + state.characters.length} / ${state.characterTotal}`;
  $("#character-prev").disabled = state.characterOffset === 0;
  $("#character-next").disabled = state.characterOffset + state.characters.length >= state.characterTotal;
}

export function renderPools() {
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
    const name = document.createElement("strong"); name.textContent = pool.name;
    const meta = document.createElement("small"); meta.textContent = `${pool.mode === "husband" ? "男角色" : "女角色"} · ${pool.character_count} 个角色`;
    button.append(name, meta);
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

// 角色图网格：普通档 3 列最多 5 张，第 6 格为“+N”；加宽档 5 列显示全部；全屏自适应。
const NORMAL_THUMBS = 5;

function catalogWide() { return catalogLayout.classList.contains("wide") && window.innerWidth > 1100; }

export function setCatalogWide(wide) {
  catalogLayout.classList.toggle("wide", wide);
  const button = $("#character-expand");
  button.setAttribute("aria-pressed", String(wide));
  button.setAttribute("aria-label", wide ? "收窄面板" : "加宽面板");
  button.querySelector("svg.i")?.remove();
  button.dataset.i = wide ? "right" : "left";
  applyIcons(button.parentElement);
  renderCharacterImages();
}

function thumbButton(hash, removable) {
  const wrap = document.createElement("div");
  wrap.className = "thumb-wrap";
  const preview = document.createElement("button");
  preview.type = "button";
  preview.className = "thumb";
  preview.disabled = true;
  preview.textContent = "读取中…";
  preview.dataset.hash = hash;
  wrap.append(preview);
  if (removable) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "thumb-remove";
    remove.setAttribute("aria-label", `移除图片 ${hash.slice(0, 12)}`);
    remove.innerHTML = icon("x");
    remove.addEventListener("click", async () => {
      const position = state.characterImages.indexOf(hash);
      state.characterImages = state.characterImages.filter((value) => value !== hash);
      renderCharacterImages();
      const restore = async () => {
        if (state.characterImages.includes(hash)) return;
        state.characterImages.splice(Math.min(position, state.characterImages.length), 0, hash);
        renderCharacterImages();
        await persistCharacterImages("已恢复图片。");
      };
      // 已有角色立即保存；新角色仍在草稿里，随“保存角色”提交。
      const saved = await persistCharacterImages(null);
      showNotice(saved ? "已移除 1 张图片。" : "已从草稿移除 1 张图片，保存角色后生效。", "success", { action: "撤销", onAction: restore });
    });
    wrap.append(remove);
  }
  if (!imagePreviewCache.has(hash)) {
    imagePreviewCache.set(hash, apiGet(`media/preview/${encodeURIComponent(hash)}`).then((response) => response.data.src));
  }
  imagePreviewCache.get(hash).then((src) => {
    if (!wrap.isConnected) return;
    const image = document.createElement("img");
    image.src = src;
    image.alt = `${$("#character-name").value || "角色"} · 图 ${hash.slice(0, 8)}`;
    // 媒体失败保留占位文字，不让格子消失。
    image.addEventListener("error", () => { preview.replaceChildren(document.createTextNode("图片无法显示")); preview.disabled = true; });
    preview.replaceChildren(image);
    preview.disabled = false;
  }).catch(() => {
    imagePreviewCache.delete(hash);
    if (wrap.isConnected) preview.textContent = "暂无预览";
  });
  return wrap;
}

function fillGallery(host, hashes, { limit = Infinity, removable = false } = {}) {
  host.replaceChildren();
  if (!hashes.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "还没有角色图。";
    host.append(empty);
    return;
  }
  const shown = hashes.length > limit ? hashes.slice(0, limit) : hashes;
  for (const hash of shown) host.append(thumbButton(hash, removable));
  if (shown.length < hashes.length) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "thumb more-thumbs";
    more.textContent = `+${hashes.length - shown.length}`;
    more.setAttribute("aria-label", `查看其余 ${hashes.length - shown.length} 张`);
    more.addEventListener("click", () => (window.innerWidth <= 1100 ? openCharacterSheet() : setCatalogWide(true)));
    host.append(more);
  }
}

export function renderCharacterImages() {
  const count = state.characterImages.length;
  $("#character-images-note").textContent = state.characterSaved
    ? `${count ? `共 ${count} 张。` : ""}添加或移除图片会立即保存，其他字段仍需点击“保存角色”。`
    : "新角色点击“保存角色”后，图片随角色一起保存。";
  const wide = catalogWide();
  $("#character-expand").hidden = !(count >= 4 || wide);
  fillGallery($("#character-images"), state.characterImages, { limit: wide ? Infinity : NORMAL_THUMBS, removable: true });
}

export function openCharacterSheet() {
  const name = $("#character-name").value || "新建角色";
  openSheet(`${name}${$("#character-id").value ? ` · ${$("#character-id").value}` : ""}`, (body) => {
    const meta = document.createElement("dl");
    meta.className = "kv";
    const pools = [...$("#character-pools").querySelectorAll("input:checked")].map((check) => check.closest("label")?.textContent.trim()).filter(Boolean);
    const gender = $("#character-gender").selectedOptions[0]?.textContent || "";
    for (const [label, value] of [["名称", name], ["ID", $("#character-id").value || "（未保存）"], ["性别", gender], ["别名", $("#character-aliases").value || "无"], ["抽取", $("#character-enabled").checked ? "已启用" : "已停用"], ["角色池", pools.join("、") || "未加入"], ["图片", `${state.characterImages.length} 张`]]) {
      const term = document.createElement("dt"); term.textContent = label;
      const detail = document.createElement("dd"); detail.textContent = value;
      meta.append(term, detail);
    }
    const gallery = document.createElement("div");
    gallery.className = "gallery";
    fillGallery(gallery, state.characterImages);
    body.append(meta, gallery);
  });
}

export async function removeCatalogItem(kind, id, endpoint) {
  const preview = await apiPost(`${endpoint}/delete-preview`, { id });
  const impact = preview.data.impact;
  if (!await confirmAction(`将软删除 ${id}。\n影响：${JSON.stringify(impact)}\n继续吗？`)) return;
  const result = await apiPost(`${endpoint}/delete`, {
    entity_kind: kind, entity_id: id, preflight_id: preview.data.preflight_id,
    reason: "AstrBot 管理页面删除", request_id: crypto.randomUUID(),
  });
  showNotice(`已软删除 ${id}`, "success");
  // 被删除的选中项清空详情并退回普通档。
  if (kind === "character") { setCatalogWide(false); newCharacter(); } else newPool();
  await loadCatalog();
}

export function newPool() {
  $("#pool-id").readOnly = false;
  $("#pool-id").value = "";
  $("#pool-name").value = "";
  $("#pool-mode").value = "wife";
  state.poolRevision = 0;
  $("#pool-revision").textContent = "新建的角色池默认不启用；需在配置管理中选择。";
  $("#delete-pool").disabled = true;
}

export function editPool(pool) {
  $("#pool-id").value = pool.id;
  $("#pool-id").readOnly = true;
  $("#pool-name").value = pool.name;
  $("#pool-mode").value = pool.mode;
  state.poolRevision = pool.revision;
  $("#pool-revision").textContent = `版本 ${pool.revision} · ${pool.character_count} 个角色`;
  $("#delete-pool").disabled = false;
}
