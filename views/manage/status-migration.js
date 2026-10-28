// 用户今日状态、回复形式预览与版本迁移。只通过 textContent 写入服务端文本。

const MODE_LABELS = { wife: "老婆", husband: "老公", member: "群友" };
const SOURCE_LABELS = { draw: "抽取", designated: "指定", gift: "赠送", steal: "抢夺" };
const REPORT_SOURCES = { database: "升级迁移", host_config: "旧版原生配置导入", manual_import: "手动导入" };

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function formatTime(seconds) {
  return new Date(seconds * 1000).toLocaleString();
}

export function initStatusAndMigration({ apiGet, apiPost, apiUpload, confirmAction, showNotice }) {
  const $ = (selector) => document.querySelector(selector);
  const scopeId = () => $("#data-scope")?.value || "";

  async function guarded(button, action) {
    if (button.disabled) return;
    button.disabled = true;
    try { await action(); }
    catch (error) { showNotice(error.message || "操作失败。", "error"); }
    finally { button.disabled = false; }
  }

  // 用户今日状态
  $("#status-load")?.addEventListener("click", (event) => guarded(event.currentTarget, async () => {
    const output = $("#status-result");
    const userId = $("#data-user").value.trim();
    if (!scopeId()) { output.textContent = "请先在上方选择群。"; return; }
    if (!/^\d+$/.test(userId)) { output.textContent = "请填写数字 QQ 号。"; $("#data-user").focus(); return; }
    output.textContent = "正在查询…";
    const { data } = await apiGet("status/user", { scope_id: scopeId(), user_id: userId });
    const heading = element("p", `${data.name}（${userId}）· ${data.reset_at} 重置`);
    const blocks = data.modes.map((item) => {
      const block = element("section", undefined, "status-mode");
      block.append(element("h4", `${MODE_LABELS[item.mode]} ${item.enabled ? `${item.held}/${item.capacity}` : "· 玩法已关闭"}`));
      if (!item.enabled) return block;
      const list = element("ul");
      for (const relation of item.relationships) {
        const facts = [SOURCE_LABELS[relation.source] || relation.source];
        if (relation.protected) facts.push("受保护");
        if (relation.streak >= 2) facts.push(`连续 ${relation.streak} 天`);
        list.append(element("li", `${relation.name}（${facts.join(" · ")}）`));
      }
      if (!item.relationships.length) list.append(element("li", "还没有关系"));
      const quota = [`普通抽取剩 ${item.normal_remaining} 次`, `补抽资格 ${item.credits}`];
      if (item.designated_limit) quota.push(`指定剩 ${item.designated_remaining} 次`);
      if (item.steal_enabled) quota.push(item.steal_remaining === null ? "抢夺不限次数" : `抢夺剩 ${item.steal_remaining} 次`);
      if (item.steal_ready_in) quota.push(`抢夺冷却 ${item.steal_ready_in} 秒`);
      block.append(list, element("p", quota.join(" · "), "facts"));
      return block;
    });
    const invites = element("p", `待处理赠送：发出 ${data.invites_sent} · 收到 ${data.invites_received}`, "facts");
    output.replaceChildren(heading, ...blocks, invites);
  }));

  // 回复形式预览
  $("#render-preview")?.addEventListener("click", (event) => guarded(event.currentTarget, async () => {
    const card = $("#render-preview-card-output");
    const text = $("#render-preview-text");
    card.textContent = "正在渲染…";
    const { data } = await apiGet("render/preview", scopeId() ? { scope_id: scopeId() } : {});
    text.textContent = data.text || "（空）";
    if (data.image) {
      const image = element("img");
      image.src = data.image;
      image.alt = "姻缘帮助卡片预览";
      card.replaceChildren(image);
    } else card.textContent = data.image_error || "卡片不可用。";
  }));

  // 版本迁移
  let plan = null;
  const commitButton = $("#migration-commit");

  async function loadMigration() {
    const host = $("#migration-reports");
    if (!host) return;
    try {
      const { data } = await apiGet("migration");
      const select = $("#migration-backup");
      select.replaceChildren(new Option(data.backups.length ? "（不使用备份）" : "（无可用备份）", ""));
      for (const backup of data.backups) {
        select.add(new Option(`${backup.name} · ${(backup.bytes / 1048576).toFixed(1)} MiB`, backup.name));
      }
      if (!data.reports.length) { host.textContent = "还没有迁移记录。"; return; }
      const list = element("ul");
      for (const report of data.reports) {
        const notes = report.report?.settings
          ? Object.entries(report.report.settings).flatMap(([scope, items]) => items.map((item) => `${scope}：${item}`))
          : report.report?.notes || [];
        const item = element("li", `${formatTime(report.created_at)} · ${REPORT_SOURCES[report.source] || report.source} · v${report.from_version} → v${report.to_version}`);
        if (report.report?.backup) item.append(element("div", `导入前备份：${report.report.backup}`, "muted"));
        if (notes.length) {
          const details = element("details");
          details.append(element("summary", `${notes.length} 条配置变化`));
          const inner = element("ul");
          for (const note of notes) inner.append(element("li", note));
          details.append(inner);
          item.append(details);
        }
        list.append(item);
      }
      host.replaceChildren(list);
    } catch (error) { host.textContent = error.message || "迁移记录读取失败。"; }
  }

  function showPlan(data) {
    const host = $("#migration-plan");
    const lines = [
      `数据库版本 v${data.version}，迁移后为 v${data.target_version}。`,
      `包含 ${data.scopes} 个群、${data.active_relationships} 段当前关系、${data.characters} 个角色。`,
    ];
    const notes = Object.entries(data.settings_notes || {}).flatMap(([scope, items]) => items.map((item) => `${scope}：${item}`));
    const list = element("ul");
    for (const line of [...lines, ...notes]) list.append(element("li", line));
    host.replaceChildren(list);
  }

  $("#migration-preview")?.addEventListener("click", (event) => guarded(event.currentTarget, async () => {
    plan = null;
    commitButton.disabled = true;
    const backup = $("#migration-backup").value;
    const file = $("#migration-file").files?.[0];
    if (!backup && !file) { $("#migration-plan").textContent = "请选择升级前备份或上传数据库文件。"; return; }
    if (backup && file) { $("#migration-plan").textContent = "备份与上传文件只能二选一。"; return; }
    $("#migration-plan").textContent = "正在预检…";
    const { data } = backup
      ? await apiPost("migration/preview-backup", { backup })
      : await apiUpload("migration/preview", file);
    plan = data;
    showPlan(data);
    commitButton.disabled = false;
  }));

  commitButton?.addEventListener("click", () => guarded(commitButton, async () => {
    if (!plan) return;
    const confirmed = await confirmAction(
      `将用所选数据库替换当前插件数据（${plan.scopes} 个群）。\n会先自动备份当前数据库；迁移失败时自动还原。继续吗？`,
    );
    if (!confirmed) return;
    const { data } = await apiPost("migration/commit", { preview_id: plan.preview_id });
    plan = null;
    $("#migration-plan").textContent = `迁移完成。导入前的数据已备份为 ${data.backup}。请刷新页面读取新数据。`;
    showNotice("迁移完成，请刷新页面。");
    await loadMigration();
  }).finally(() => { commitButton.disabled = !plan; }));

  // 打开“备份与迁移”模块时读取迁移记录。
  document.querySelector('.island [data-tab="maintenance"]')?.addEventListener("click", () => { loadMigration(); });
}
