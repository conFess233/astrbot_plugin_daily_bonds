// WebUI 2.0 交互反馈：通知、按钮加载、数字递增、悬浮岛指示块、全屏面板与灯箱。
import { icon } from "./icons.js";

const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// 模态对话框会让外部节点失去交互，通知容器跟随最上层对话框，否则挂在 body。
let toastHost;
function toastContainer() {
  if (!toastHost) {
    toastHost = document.createElement("div");
    toastHost.className = "toasts";
    toastHost.setAttribute("popover", "manual");
    toastHost.setAttribute("aria-live", "polite");
  }
  const modal = [...document.querySelectorAll("dialog[open]")].at(-1);
  const parent = modal || document.body;
  if (toastHost.parentElement !== parent) {
    toastHost.hidePopover?.();
    parent.append(toastHost);
  }
  if (toastHost.children.length) {
    toastHost.hidePopover?.();
    toastHost.showPopover?.();
  }
  return toastHost;
}
document.addEventListener("close", (event) => {
  if (toastHost && event.target.contains(toastHost)) toastContainer();
}, true);

/**
 * 深色通知条：普通约 2.6 秒消失；带动作（如撤销）的 5 秒并显示计时线；错误 8 秒，可手动关闭。
 * 返回通知元素，调用方可向 `.msg` 追加详情。
 */
export function toast(message, kind = "ok", options = {}) {
  const item = document.createElement("div");
  item.className = "toast";
  item.setAttribute("role", kind === "bad" ? "alert" : "status");
  const mark = document.createElement("span");
  mark.className = `ic ${kind}`;
  mark.innerHTML = icon(kind === "bad" ? "info" : "check");
  const body = document.createElement("span");
  body.className = "msg";
  body.textContent = message;
  item.append(mark, body);
  let timer;
  const close = () => {
    clearTimeout(timer);
    if (!item.isConnected || item.classList.contains("out")) return;
    item.classList.add("out");
    window.setTimeout(() => {
      item.remove();
      if (toastHost && !toastHost.children.length) toastHost.hidePopover?.();
    }, reduced() ? 0 : 200);
  };
  if (options.action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = options.action;
    button.addEventListener("click", () => { close(); options.onAction?.(); });
    const line = document.createElement("span");
    line.className = "timer";
    item.append(button, line);
  } else if (kind === "bad") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "close";
    button.setAttribute("aria-label", "关闭通知");
    button.textContent = "×";
    button.addEventListener("click", close);
    item.append(button);
  }
  const duration = options.action ? 5000 : kind === "bad" ? 8000 : 2600;
  const arm = () => { clearTimeout(timer); timer = window.setTimeout(close, duration); };
  // 悬停或聚焦时暂停计时，便于阅读较长的错误与详情。
  item.addEventListener("pointerenter", () => clearTimeout(timer));
  item.addEventListener("pointerleave", arm);
  item.addEventListener("focusin", () => clearTimeout(timer));
  const host = toastContainer();
  host.append(item);
  host.hidePopover?.();
  host.showPopover?.();
  arm();
  return item;
}

/** 按钮内显示转圈与进行时文案，结束后恢复；执行期间禁用以防重复提交。 */
export async function busyButton(button, label, task) {
  if (!button) return task();
  const saved = [...button.childNodes];
  const wasDisabled = button.disabled;
  const spinner = document.createElement("span");
  spinner.className = "spin";
  button.replaceChildren(spinner, document.createTextNode(label));
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await task();
  } finally {
    button.replaceChildren(...saved);
    button.disabled = wasDisabled;
    button.removeAttribute("aria-busy");
  }
}

/** 数字在约 0.5 秒内 ease-out 递增到目标值。 */
export function countUp(element, target) {
  if (!Number.isFinite(target) || reduced()) { element.textContent = String(target); return; }
  const start = performance.now();
  const step = (now) => {
    const progress = Math.min(1, (now - start) / 520);
    element.textContent = String(Math.round(target * (1 - (1 - progress) ** 3)));
    if (progress < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/** 配置修改后所在行短暂闪一下淡色底。 */
export function flash(element) {
  if (!element) return;
  element.classList.remove("flash");
  void element.offsetWidth;
  element.classList.add("flash");
  window.setTimeout(() => element.classList.remove("flash"), 320);
}

/** 输入错误：边框变危险色并轻晃一次。 */
export function nudge(element) {
  if (!element) return;
  element.classList.remove("err");
  void element.offsetWidth;
  element.classList.add("err");
  element.addEventListener("input", () => element.classList.remove("err"), { once: true });
}

/** 列表行依次淡入。 */
export function staggerRows(rows) {
  [...rows].slice(0, 30).forEach((row, index) => {
    row.style.setProperty("--k", index);
    row.classList.add("row-in");
  });
}

/** 悬浮岛滑动指示块：返回在选中项或布局变化时调用的定位函数。 */
export function islandPill(nav) {
  const pill = document.createElement("span");
  pill.className = "pill";
  pill.setAttribute("aria-hidden", "true");
  nav.prepend(pill);
  const place = (instant = false) => {
    const current = nav.querySelector('[aria-current="page"]');
    if (!current || !current.offsetWidth) return;
    if (instant) pill.style.transition = "none";
    pill.style.width = `${current.offsetWidth}px`;
    pill.style.transform = `translateX(${current.offsetLeft}px)`;
    if (instant) requestAnimationFrame(() => { pill.style.transition = ""; });
  };
  window.addEventListener("resize", () => place(true));
  nav.addEventListener("scroll", () => place(true));
  new ResizeObserver(() => place(true)).observe(nav);
  return place;
}

// ── 全屏面板 ─────────────────────────────────
let sheetReturn = null;
let sheet;
function sheetElements() {
  if (sheet) return sheet;
  const scrim = document.createElement("div");
  scrim.className = "scrim";
  const panel = document.createElement("section");
  panel.className = "sheet";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", "sheet-title");
  panel.innerHTML = `<header><h3 id="sheet-title"></h3><button type="button" class="icon-btn" aria-label="关闭全屏查看">${icon("x")}</button></header><div class="body"></div>`;
  document.body.append(scrim, panel);
  const close = () => closeSheet();
  scrim.addEventListener("click", close);
  panel.querySelector("header button").addEventListener("click", close);
  sheet = { scrim, panel, title: panel.querySelector("h3"), body: panel.querySelector(".body") };
  return sheet;
}

/** 打开全屏查看：`fill(body)` 负责写入内容；Esc、关闭按钮或遮罩退出，焦点回到触发按钮。 */
export function openSheet(title, fill) {
  const parts = sheetElements();
  sheetReturn = document.activeElement;
  parts.title.textContent = title;
  parts.body.replaceChildren();
  fill(parts.body);
  parts.scrim.classList.add("on");
  parts.panel.classList.add("on");
  parts.panel.querySelector("header button").focus();
}

export function closeSheet() {
  if (!sheet?.panel.classList.contains("on")) return false;
  sheet.scrim.classList.remove("on");
  sheet.panel.classList.remove("on");
  sheetReturn?.focus?.();
  sheetReturn = null;
  return true;
}

export function sheetOpen() { return !!sheet?.panel.classList.contains("on"); }

// ── 灯箱：从缩略图位置放大，左右键翻页，Esc 关闭 ──
let box;
let boxItems = [];
let boxIndex = 0;
function boxElements() {
  if (box) return box;
  const root = document.createElement("div");
  root.className = "lightbox";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-label", "图片查看");
  root.setAttribute("aria-hidden", "true");
  root.innerHTML = `<div class="bg"></div><img alt="" /><div class="lb-bar"><button type="button" data-lb="prev" aria-label="上一张">${icon("left")}</button><span></span><button type="button" data-lb="next" aria-label="下一张">${icon("right")}</button><button type="button" data-lb="close" aria-label="关闭">${icon("x")}</button></div>`;
  document.body.append(root);
  root.querySelector(".bg").addEventListener("click", closeLightbox);
  root.querySelector(".lb-bar").addEventListener("click", (event) => {
    const action = event.target.closest("[data-lb]")?.dataset.lb;
    if (action === "close") closeLightbox();
    else if (action) stepLightbox(action === "next" ? 1 : -1);
  });
  box = { root, image: root.querySelector("img"), counter: root.querySelector(".lb-bar span") };
  return box;
}

function placeAt(rect) {
  Object.assign(box.image.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` });
}

function placeCentered() {
  const width = Math.min(innerWidth * 0.8, 1200);
  const height = Math.min(innerHeight * 0.78, 1000);
  placeAt({ left: (innerWidth - width) / 2, top: (innerHeight - height) / 2 - 20, width, height });
}

/** `items` 为缩略图按钮数组，每个按钮内含 img；`index` 为点击项。 */
export function openLightbox(items, index) {
  const parts = boxElements();
  boxItems = items;
  boxIndex = index;
  const from = items[index];
  parts.image.style.transition = "none";
  placeAt(from.getBoundingClientRect());
  parts.image.src = from.querySelector("img")?.src || "";
  parts.image.alt = from.querySelector("img")?.alt || "";
  parts.image.style.visibility = "visible";
  parts.root.classList.add("on");
  parts.root.setAttribute("aria-hidden", "false");
  parts.counter.textContent = `${index + 1} / ${items.length}`;
  requestAnimationFrame(() => requestAnimationFrame(() => { parts.image.style.transition = ""; placeCentered(); }));
  parts.root.querySelector('[data-lb="close"]').focus();
}

function stepLightbox(delta) {
  if (!boxItems.length) return;
  boxIndex = (boxIndex + delta + boxItems.length) % boxItems.length;
  const source = boxItems[boxIndex].querySelector("img");
  box.image.src = source?.src || "";
  box.image.alt = source?.alt || "";
  box.counter.textContent = `${boxIndex + 1} / ${boxItems.length}`;
  if (!reduced()) box.image.animate([{ opacity: 0, transform: `translateX(${delta * 16}px)` }, { opacity: 1, transform: "none" }], { duration: 200, easing: "cubic-bezier(.2,.8,.2,1)" });
}

export function closeLightbox() {
  if (!box?.root.classList.contains("on")) return false;
  const origin = boxItems[boxIndex];
  if (origin?.isConnected) placeAt(origin.getBoundingClientRect());
  box.root.classList.remove("on");
  box.root.setAttribute("aria-hidden", "true");
  window.setTimeout(() => { if (!box.root.classList.contains("on")) box.image.style.visibility = "hidden"; }, reduced() ? 0 : 330);
  if (origin?.isConnected) origin.focus();
  return true;
}

document.addEventListener("keydown", (event) => {
  if (box?.root.classList.contains("on")) {
    if (event.key === "Escape") { event.preventDefault(); closeLightbox(); }
    else if (event.key === "ArrowRight") stepLightbox(1);
    else if (event.key === "ArrowLeft") stepLightbox(-1);
    return;
  }
  if (event.key === "Escape" && sheetOpen()) { event.preventDefault(); closeSheet(); }
});
