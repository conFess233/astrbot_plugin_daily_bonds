// 概览里的数据看板：抽取趋势柱状图、关键指标和两张排行。图表只有一个系列，统一用强调色。
import { $, apiGet, showNotice } from "./app.js";

let days = 14;
let scopeId = "";
let sequence = 0;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function rate(success, attempts) {
  return attempts ? `${Math.round((success / attempts) * 100)}%` : "—";
}

function tile(label, value, note) {
  const card = element("article", "stat");
  const head = element("div", "label");
  head.append(element("span", "", label));
  if (note) head.append(element("span", "", note));
  card.append(head, element("p", "value", String(value)));
  return card;
}

function renderTiles(totals) {
  $("#dashboard-tiles").replaceChildren(
    tile("成功抽取", totals.draws),
    tile("参与人数", totals.active_users),
    tile("抢夺成功率", rate(totals.steal_success, totals.steal_attempts), `${totals.steal_success}/${totals.steal_attempts}`),
    tile("强娶成功率", rate(totals.force_success, totals.force_attempts), `${totals.force_success}/${totals.force_attempts}`),
  );
}

// 取干净的纵轴上限：1、2、5 乘以 10 的幂。
function niceMax(value) {
  if (value <= 4) return 4;
  const power = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 5, 10].find((item) => item * power >= value);
  return step * power;
}

const shortDate = (date) => `${Number(date.slice(5, 7))}/${Number(date.slice(8, 10))}`;

function renderTrend(trend) {
  const host = $("#dashboard-trend");
  host.replaceChildren();
  const peak = Math.max(0, ...trend.map((item) => item.draws));
  if (!peak) { host.append(element("p", "muted chart-empty", "这段时间还没有成功抽取的记录。")); return; }
  const top = niceMax(peak);
  const plot = element("div", "trend-plot");
  for (const ratio of [1, 0.5, 0]) {
    const line = element("div", "trend-grid");
    line.style.bottom = `${ratio * 100}%`;
    line.append(element("span", "trend-tick", String(Math.round(top * ratio))));
    plot.append(line);
  }
  const bars = element("div", "trend-bars");
  const tooltip = element("div", "chart-tooltip");
  tooltip.hidden = true;
  const show = (slot, item) => {
    const value = element("strong", "", `${item.draws} 次`);
    tooltip.replaceChildren(value, element("span", "", shortDate(item.date)));
    tooltip.hidden = false;
    const box = slot.getBoundingClientRect();
    const parent = host.getBoundingClientRect();
    tooltip.style.left = `${Math.min(Math.max(box.left - parent.left + box.width / 2, 40), parent.width - 40)}px`;
  };
  for (const item of trend) {
    const slot = element("button", "trend-slot");
    slot.type = "button";
    slot.setAttribute("aria-label", `${item.date}：${item.draws} 次`);
    const bar = element("span", "trend-bar");
    bar.style.height = item.draws ? `max(3px, ${(item.draws / top) * 100}%)` : "0";
    slot.append(bar);
    slot.addEventListener("pointerenter", () => show(slot, item));
    slot.addEventListener("focus", () => show(slot, item));
    slot.addEventListener("pointerleave", () => { tooltip.hidden = true; });
    slot.addEventListener("blur", () => { tooltip.hidden = true; });
    bars.append(slot);
  }
  plot.append(bars);
  const axis = element("div", "trend-axis");
  const middle = trend[Math.floor((trend.length - 1) / 2)];
  for (const item of [trend[0], middle, trend[trend.length - 1]]) axis.append(element("span", "", shortDate(item.date)));
  host.append(plot, axis, tooltip);
}

function renderTable(trend) {
  const table = element("table", "dashboard-data");
  const head = element("tr");
  head.append(element("th", "", "日期"), element("th", "", "成功抽取"));
  const thead = element("thead"); thead.append(head);
  const body = element("tbody");
  for (const item of trend) {
    const row = element("tr");
    row.append(element("td", "", item.date), element("td", "", String(item.draws)));
    body.append(row);
  }
  table.append(thead, body);
  $("#dashboard-table").replaceChildren(table);
}

function renderBars(host, rows, empty) {
  host.replaceChildren();
  if (!rows.length) { host.append(element("p", "muted chart-empty", empty)); return; }
  const peak = Math.max(...rows.map((row) => row.value));
  for (const row of rows) {
    const line = element("div", "rank-bar");
    const name = element("span", "rank-bar-name", row.name);
    name.title = row.name;
    const track = element("span", "rank-bar-track");
    const fill = element("span", "rank-bar-fill");
    fill.style.width = `${(row.value / peak) * 100}%`;
    track.append(fill);
    line.append(name, track, element("span", "rank-bar-value", String(row.value)));
    host.append(line);
  }
}

export async function loadDashboard(scope = scopeId) {
  scopeId = scope;
  const current = ++sequence;
  try {
    const { data } = await apiGet("dashboard", { days, ...(scopeId ? { scope_id: scopeId } : {}) });
    if (current !== sequence) return;
    renderTiles(data.totals);
    renderTrend(data.trend);
    renderTable(data.trend);
    renderBars($("#dashboard-characters"), data.characters, "还没有角色结缘记录。");
    $("#dashboard-groups-wrap").hidden = !!scopeId;
    renderBars($("#dashboard-groups"), data.groups, "这段时间还没有操作记录。");
    const extras = [`赠送 ${data.totals.gifts} 次`, `离婚 ${data.totals.divorces} 次`];
    $("#dashboard-note").textContent = `近 ${data.days} 天 · ${extras.join(" · ")} · 操作记录最长保留到历史保留天数。`;
  } catch (error) {
    if (current === sequence) showNotice(`看板读取失败：${error.message || "请重试"}。`, "error");
  }
}

export function initDashboard() {
  for (const button of document.querySelectorAll("#dashboard-days [data-days]")) {
    button.addEventListener("click", () => {
      days = Number(button.dataset.days);
      for (const other of document.querySelectorAll("#dashboard-days [data-days]")) other.setAttribute("aria-pressed", String(other === button));
      loadDashboard();
    });
  }
}
