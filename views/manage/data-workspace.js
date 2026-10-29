// 数据与备份模块：按群查询、周期重置、角色包导入预检与备份恢复映射。
import { $, apiPost, bridge, showNotice, state } from "./app.js";

export function dataTable(records, columns) {
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

export const correctionExamples = {
  relationship_end: '{"relationship_id":"关系 ID","compensation":0}',
  credit_grant: '{"mode":"member","user_id":"QQ号","quantity":1}',
  intimacy_set: '{"source_id":"QQ号","target_id":"QQ号","score":0}',
  activity_set: '{"user_id":"QQ号","observed_second":1780000000,"message_count":0}',
  counter_reset: '{"mode":"member","user_id":"QQ号","fields":["normal_draws"]}',
};

export function clearPeriodResetPlan() {
  state.periodResetPlan = null;
  $("#period-reset-confirm").disabled = true;
}

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

export async function previewPeriodReset(action) {
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

export function renderImportPreview(data) {
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

export function renderRestoreScopeMappings(plan) {
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

export function renderMaintenanceJob(data, title) {
  const host = $("#maintenance-report");
  host.replaceChildren();
  const summary = document.createElement("p");
  summary.textContent = `${title} · 作业 ${data.job_id} · ${data.bytes ?? ""} bytes · SHA-256 ${data.sha256 ?? ""}`;
  host.append(summary);
  if (data.download) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-ghost";
    button.textContent = "下载 ZIP";
    button.addEventListener("click", () => bridge.download(data.download, {}, `${title}-${data.job_id}.zip`)
      .catch((error) => showNotice(error.message || "文件下载失败。", "error")));
    host.append(button);
  }
  const details = document.createElement("pre");
  details.textContent = JSON.stringify(data, null, 2);
  host.append(details);
}

export const QUERY_COLUMNS = {
  relationships: ["id", "snapshot_name", "owner_id", "mode", "slot_kind", "state", "acquired_at"],
  intimacy: ["source_id", "target_id", "score", "updated_at"],
  activity: ["user_id", "score", "messages", "active_days", "first_observed", "last_observed"],
  invites: ["snapshot_name", "sender_id", "recipient_id", "state", "expires_at"],
  history: ["id", "kind", "actor_id", "mode", "result_code", "created_at", "data"],
};

const COLUMN_LABELS = { id: "ID", kind: "操作", result_code: "结果代码", snapshot_name: "角色/群友", owner_id: "持有者", mode: "玩法", slot_kind: "槽位", state: "状态", acquired_at: "获得时间", source_id: "发起用户", target_id: "目标用户", score: "分数", updated_at: "更新时间", user_id: "用户", messages: "消息数", active_days: "活跃天数", first_observed: "首次记录", last_observed: "最近记录", sender_id: "赠送者", recipient_id: "接收者", expires_at: "到期时间", action: "操作", actor_id: "操作者", created_at: "记录时间", data: "详情" };
