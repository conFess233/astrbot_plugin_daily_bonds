// 批量导入角色图：上传后按目标角色分组核对，一次导入全部可导入的图片。
// 后端负责按文件名与文件夹名匹配；这里只把结果按角色归组，并提供改角色、新建、排除与导入。
import { confirmAction } from "./confirm-dialog.js";
import { applyIcons, icon } from "./icons.js";
import { bindDropZone, runLimited } from "./upload-images.js";

const THUMBS_PER_GROUP = 18;

export function initBatchImages({ apiGet, apiPost, apiUpload, showNotice, onImported, onDefaultsSaved }) {
  const $ = (id) => document.getElementById(id);
  const dialog = $("batch-images-dialog");
  let options = null;
  let job = null;
  let busy = false;
  let filter = "todo";
  let defaultsRequest = null;
  let commitRequest = null;
  const expanded = new Set();
  const selected = new Set();
  const thumbnails = new Map();
  let openPicker = null;
  let previewQueue = Promise.resolve();

  const node = (tag, text = "", className = "") => {
    const element = document.createElement(tag);
    if (text) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const button = (text, className, onClick, iconName) => {
    const element = node("button", "", className);
    element.type = "button";
    if (iconName) element.innerHTML = icon(iconName);
    element.append(document.createTextNode(text));
    element.addEventListener("click", onClick);
    return element;
  };

  // 行状态：与后端导入条件一致。
  const pending = (row) => row.status === "pending" && !row.upload_error;
  const ready = (row) => pending(row) && !row.excluded && (!row.needs_manual || row.manual_approved) && !row.errors?.length;
  const attention = (row) => row.upload_error && row.status !== "skipped" || pending(row) && !ready(row);
  const poolName = (id) => options?.pools.find((pool) => pool.id === id)?.name || id;

  async function task(work) {
    if (busy) return;
    busy = true;
    $("batch-images-controls").disabled = true;
    $("batch-import").disabled = true;
    try { await work(); }
    catch (failure) { showNotice(failure.message || "操作失败，已上传的图片仍保留在批次中。", "error"); }
    finally {
      busy = false;
      $("batch-images-controls").disabled = false;
      render();
    }
  }

  function settings() {
    const words = (id) => $(id).value.split(/[,，\n]/).map((value) => value.trim()).filter(Boolean);
    return {
      male_keywords: words("batch-male-keywords"), female_keywords: words("batch-female-keywords"),
      male_pool_id: $("batch-male-pool").value, female_pool_id: $("batch-female-pool").value,
    };
  }
  function setSettings(value) {
    $("batch-male-keywords").value = value.male_keywords.join(", ");
    $("batch-female-keywords").value = value.female_keywords.join(", ");
    for (const gender of ["male", "female"]) {
      const select = $(`batch-${gender}-pool`);
      select.replaceChildren(new Option("不自动加入", ""));
      for (const pool of options.pools.filter((pool) => pool.mode === (gender === "male" ? "husband" : "wife"))) {
        select.add(new Option(pool.name, pool.id));
      }
      select.value = value[`${gender}_pool_id`];
    }
  }

  async function patch(rows, fields) {
    if (!rows.length) return;
    const items = rows.map((row) => ({ row_id: row.row_id, ...(typeof fields === "function" ? fields(row) : fields) }));
    job = (await apiPost("batch-images/update", { job_id: job.job_id, items })).data;
    for (const row of rows) selected.delete(row.row_id);
  }

  // ---- 分组 ----
  function groupKey(row) {
    if (row.upload_error) return row.status === "skipped" ? "skipped" : "failed";
    if (row.status === "skipped") return "skipped";
    if (row.status === "imported") return `imported:${row.target_id}`;
    return `target:${row.target_id}`;
  }
  function groups() {
    const map = new Map();
    for (const row of job?.items || []) {
      const key = groupKey(row);
      if (!map.has(key)) map.set(key, { key, rows: [] });
      map.get(key).rows.push(row);
    }
    const list = [...map.values()];
    for (const group of list) {
      group.attention = group.rows.some(attention);
      group.done = group.key === "skipped" || group.key.startsWith("imported:");
    }
    const visible = list.filter((group) => filter === "done" ? group.done : filter === "attention" ? group.attention : !group.done);
    // 需要处理的排在前面，其余按名称。
    return visible.sort((a, b) => Number(b.attention) - Number(a.attention) || (a.rows[0].name || "").localeCompare(b.rows[0].name || "", "zh-CN"));
  }

  // ---- 渲染 ----
  function summary() {
    const items = job?.items || [];
    const readyRows = items.filter(ready);
    const targets = new Set(readyRows.map((row) => row.target_id));
    const created = new Set(readyRows.filter((row) => !row.existing).map((row) => row.target_id));
    const counts = {
      ready: readyRows.length,
      attention: items.filter(attention).length,
      imported: items.filter((row) => row.status === "imported").length,
      skipped: items.filter((row) => row.status === "skipped").length,
    };
    return { readyRows, targets, created, counts, items };
  }

  function render() {
    const { readyRows, targets, created, counts, items } = summary();
    const hasJob = Boolean(job);
    $("batch-review").hidden = !items.length;
    $("batch-new").hidden = !hasJob;
    $("batch-resume-wrap").hidden = hasJob || !options?.jobs?.length;
    const chips = $("batch-summary");
    chips.replaceChildren();
    const chip = (text, tone) => chips.append(node("span", text, `chip ${tone}`));
    if (items.length) {
      chip(`可导入 ${counts.ready} 张`, "ok");
      if (counts.attention) chip(`需要处理 ${counts.attention} 张`, "warn");
      if (counts.imported) chip(`已导入 ${counts.imported} 张`, "plain");
      if (counts.skipped) chip(`已排除 ${counts.skipped} 张`, "plain");
    }
    for (const control of $("batch-filter").querySelectorAll("button")) {
      control.setAttribute("aria-pressed", String(control.dataset.filter === filter));
    }
    const host = $("batch-groups");
    host.replaceChildren();
    const list = groups();
    for (const group of list) host.append(groupCard(group));
    if (items.length && !list.length) {
      host.append(node("p", filter === "attention" ? "没有需要处理的图片。" : filter === "done" ? "还没有导入或排除的图片。" : "全部图片都已处理。", "muted batch-empty"));
    }
    applyIcons(host);
    const importButton = $("batch-import");
    importButton.disabled = busy || !readyRows.length;
    importButton.textContent = readyRows.length ? `导入 ${readyRows.length} 张` : "导入";
    $("batch-footer-text").textContent = !items.length
      ? "选择图片后会自动匹配角色。"
      : readyRows.length
        ? `追加到 ${targets.size - created.size} 个已有角色${created.size ? `，新建 ${created.size} 个角色` : ""}${counts.attention ? `；${counts.attention} 张需要处理的图片不会导入` : ""}。`
        : counts.attention ? "先处理标黄的分组：选择对应角色、确认新建或排除。" : "这一批已全部处理。";
    if (job) $("batch-status").textContent = `批次保留至 ${new Date(job.expires_at * 1000).toLocaleString()}。`;
  }

  function groupCard(group) {
    const first = group.rows[0];
    const card = node("article", "", "batch-group");
    card.dataset.tone = group.done ? "done" : group.attention ? "attention" : "ready";
    const head = node("div", "", "batch-group-head");
    const title = node("div", "", "batch-group-title");
    const label = (text, tone) => title.append(node("span", text, `chip ${tone}`));
    if (group.key === "failed") {
      title.append(node("strong", "上传失败"));
    } else if (group.key === "skipped") {
      title.append(node("strong", "已排除"));
    } else {
      title.append(node("strong", first.name || "未命名角色"));
      if (group.key.startsWith("imported:")) label("已导入", "ok");
      else if (first.existing) label("已有角色", "plain");
      else label(first.manual_approved ? "新角色 · 已确认" : "新角色", first.manual_approved ? "ok" : "warn");
    }
    title.append(node("span", `${group.rows.length} 张`, "muted"));
    const chosen = group.rows.filter((row) => selected.has(row.row_id));
    if (chosen.length) title.append(node("span", `已选 ${chosen.length} 张`, "chip plain"));
    head.append(title);

    const actions = node("div", "", "batch-group-actions");
    const scope = () => (chosen.length ? chosen : group.rows);
    if (group.key === "failed") {
      actions.append(button("移除失败项", "btn btn-ghost btn-sm", () => task(() => patch(group.rows, { status: "skipped" }))));
    } else if (group.key === "skipped") {
      const restorable = group.rows.filter((row) => !row.upload_error);
      if (restorable.length) actions.append(button(chosen.length ? "恢复所选" : "全部恢复", "btn btn-ghost btn-sm", () => task(() => patch(chosen.length ? chosen : restorable, { status: "pending" }))));
    } else if (!group.key.startsWith("imported:")) {
      actions.append(button(chosen.length ? "所选改为…" : "更改角色", "btn btn-ghost btn-sm", () => { openPicker = openPicker === group.key ? null : group.key; render(); }));
      if (!first.existing && !first.manual_approved) {
        actions.append(button("确认新建", "btn btn-tonal btn-sm", () => confirmNew(group)));
      }
      actions.append(button(chosen.length ? "排除所选" : "排除", "btn btn-ghost btn-sm", () => task(() => patch(scope(), { status: "skipped" }))));
    }
    head.append(actions);
    card.append(head);

    // 说明：卡池、匹配方式、警告与候选。
    const notes = node("div", "", "batch-group-notes");
    if (group.key.startsWith("target:")) {
      const pools = first.existing ? first.pool_ids : first.pool_ids || [];
      if (!first.existing) notes.append(node("span", pools.length ? `将加入：${pools.map(poolName).join("、")}` : "尚未选择角色池", pools.length ? "muted" : "warn-text"));
      const warnings = [...new Set(group.rows.flatMap((row) => [...(row.warnings || []), ...(row.errors || [])]))]
        .filter((text) => !text.startsWith("未识别性别"));
      for (const text of warnings) notes.append(node("span", text, "warn-text"));
      const candidates = new Map(group.rows.flatMap((row) => (row.candidates || []).map((item) => [item.id, item])));
      if (!first.existing && candidates.size) {
        const picks = node("span", "可能是：", "batch-candidates");
        for (const candidate of candidates.values()) {
          picks.append(button(candidate.name, "btn btn-ghost btn-sm", () => task(() => patch(scope(), { existing: true, target_id: candidate.id, pool_ids: [] }))));
        }
        notes.append(picks);
      }
    } else if (group.key === "failed") {
      notes.append(node("span", "这些文件没有进入批次。修正后重新选择即可。", "muted"));
    }
    if (notes.childElementCount) card.append(notes);
    if (openPicker === group.key) card.append(picker(group, scope()));

    const thumbs = node("div", "", "batch-thumbs");
    const shown = expanded.has(group.key) ? group.rows : group.rows.slice(0, THUMBS_PER_GROUP);
    for (const row of shown) thumbs.append(thumb(row, group));
    if (shown.length < group.rows.length) {
      thumbs.append(button(`+${group.rows.length - shown.length}`, "thumb more-thumbs", () => { expanded.add(group.key); render(); }));
    }
    card.append(thumbs);
    return card;
  }

  function thumb(row, group) {
    const item = node("figure", "", "batch-thumb");
    const selectable = !group.key.startsWith("imported:") && group.key !== "failed";
    const face = node(selectable ? "button" : "div", "", "thumb");
    if (selectable) {
      face.type = "button";
      face.setAttribute("aria-pressed", String(selected.has(row.row_id)));
      face.setAttribute("aria-label", `${selected.has(row.row_id) ? "取消选择" : "选择"} ${row.filename}`);
      face.addEventListener("click", () => {
        selected.has(row.row_id) ? selected.delete(row.row_id) : selected.add(row.row_id);
        render();
      });
    }
    if (row.hash) {
      const image = node("img");
      image.alt = "";
      face.append(image);
      const key = `${job.job_id}/${row.row_id}`;
      if (thumbnails.has(key)) image.src = thumbnails.get(key);
      else {
        face.classList.add("loading");
        previewQueue = previewQueue.then(async () => {
          if (!face.isConnected) return;
          try {
            if (!thumbnails.has(key)) thumbnails.set(key, (await apiGet(`batch-images/${job.job_id}/preview/${row.row_id}`)).data.src);
            image.src = thumbnails.get(key);
          } catch { face.append(node("span", "无法预览", "thumb-fallback")); }
          finally { face.classList.remove("loading"); }
        });
      }
    } else face.append(node("span", "无预览", "thumb-fallback"));
    const caption = node("figcaption", row.relative_path || row.filename);
    caption.title = row.relative_path || row.filename;
    item.append(face, caption);
    if (row.upload_error) item.append(node("p", (row.errors || []).join("；") || "上传失败", "batch-thumb-error"));
    else if (row.duplicate_sources?.length) item.append(node("p", row.duplicate_skip ? "图库里已有，已跳过" : `也用于：${row.duplicate_sources.join("、")}`, "batch-thumb-note"));
    return item;
  }

  // 改角色：搜索已有角色，或用当前名称新建。
  function picker(group, rows) {
    const first = group.rows[0];
    const panel = node("div", "", "batch-picker");
    const search = node("input");
    search.type = "search";
    search.placeholder = "搜索已有角色：名称、别名或 ID";
    search.setAttribute("aria-label", "搜索已有角色");
    const results = node("div", "", "batch-picker-results");
    let sequence = 0;
    let timer;
    const run = async () => {
      const current = ++sequence;
      results.replaceChildren(node("span", "正在搜索…", "muted"));
      try {
        const value = (await apiGet("characters", { q: search.value.trim(), limit: 24 })).data;
        if (current !== sequence) return;
        results.replaceChildren();
        if (!value.items.length) results.append(node("span", "没有找到，可在下方新建。", "muted"));
        for (const character of value.items) {
          const choice = button(`${character.name}`, "btn btn-ghost btn-sm", () => task(async () => {
            await patch(rows, { existing: true, target_id: character.id, pool_ids: [] });
            openPicker = null;
          }));
          choice.title = character.id;
          results.append(choice);
        }
      } catch (failure) { if (current === sequence) results.replaceChildren(node("span", failure.message || "搜索失败", "warn-text")); }
    };
    search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 250); });
    search.value = first.existing ? "" : first.name || "";
    const create = node("div", "", "batch-picker-new");
    const name = node("input");
    name.value = first.existing ? "" : first.name || "";
    name.maxLength = 120;
    name.placeholder = "新角色名称";
    name.setAttribute("aria-label", "新角色名称");
    const gender = node("select");
    gender.setAttribute("aria-label", "新角色性别");
    for (const [value, text] of [["female", "女"], ["male", "男"], ["unspecified", "未指定"]]) gender.add(new Option(text, value));
    gender.value = first.existing ? "female" : first.gender || "female";
    const pool = node("select");
    pool.setAttribute("aria-label", "加入角色池");
    const fillPools = () => {
      const mode = { female: "wife", male: "husband" }[gender.value];
      pool.replaceChildren(...options.pools.filter((item) => !mode || item.mode === mode).map((item) => new Option(item.name, item.id)));
      const preferred = first.pool_ids?.find((id) => [...pool.options].some((option) => option.value === id))
        || settings()[`${gender.value === "male" ? "male" : "female"}_pool_id`];
      if (preferred) pool.value = preferred;
    };
    gender.addEventListener("change", fillPools);
    fillPools();
    const make = button("新建并使用", "btn btn-tonal btn-sm", () => task(async () => {
      if (!name.value.trim()) throw new Error("请填写新角色名称。");
      if (!pool.value) throw new Error("请选择角色池。");
      const id = !first.existing && rows.length === group.rows.length ? first.target_id : `char_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
      await patch(rows, { existing: false, target_id: id, name: name.value.trim(), gender: gender.value, pool_ids: [pool.value], enabled: true, manual_approved: true });
      openPicker = null;
    }));
    create.append(node("span", "或新建：", "muted"), name, gender, pool, make);
    panel.append(search, results, create);
    queueMicrotask(() => { search.focus(); run(); });
    return panel;
  }

  function confirmNew(group) {
    const first = group.rows[0];
    if (!first.pool_ids?.length || !first.name?.trim()) {
      openPicker = group.key;
      render();
      showNotice("请为新角色填写名称并选择角色池。", "error");
      return;
    }
    task(() => patch(group.rows, { manual_approved: true }));
  }

  // ---- 上传 ----
  async function upload(entries) {
    await task(async () => {
      if (!options) await loadOptions();
      const limit = options.limits.import_max_entries;
      if (entries.length + (job?.items.length || 0) > limit) throw new Error(`一批最多 ${limit} 张图片，请分批选择。`);
      if (!job) job = (await apiPost("batch-images/create", { settings: settings() })).data;
      const progress = $("batch-upload-progress");
      progress.hidden = false;
      progress.max = entries.length;
      progress.value = 0;
      let done = 0;
      try {
        const results = await runLimited(entries, 3, async ({ file, path }) => {
          let value;
          try { value = await apiUpload(`batch-images/${job.job_id}/upload`, file); }
          catch (failure) {
            value = await apiPost("batch-images/failure", { job_id: job.job_id, filename: file.name, relative_path: path, error: failure.message || "上传失败" });
          }
          done += 1;
          progress.value = done;
          $("batch-status").textContent = `正在上传 ${done}/${entries.length}`;
          return { item: value.data.item, path };
        });
        // 文件夹路径在上传后补交，后端据此按文件夹名重新匹配。
        const paths = results.filter((result) => result.ok && result.value.item && result.value.path && !result.value.item.upload_error)
          .map((result) => ({ row_id: result.value.item.row_id, relative_path: result.value.path }));
        if (paths.length) await apiPost("batch-images/update", { job_id: job.job_id, items: paths });
        job = (await apiGet("batch-images", { job_id: job.job_id })).data;
        filter = "todo";
        const failed = results.filter((result) => !result.ok || result.value.item?.upload_error).length;
        showNotice(`已上传 ${entries.length - failed} 张${failed ? `，${failed} 张失败` : ""}，请核对匹配结果。`, failed ? "error" : "success");
      } finally { progress.hidden = true; }
    });
  }

  async function commit() {
    await task(async () => {
      job = (await apiGet("batch-images", { job_id: job.job_id })).data;
      const { readyRows, targets, created } = summary();
      if (!readyRows.length) throw new Error("没有可导入的图片。");
      const existing = targets.size - created.size;
      const message = `导入 ${readyRows.length} 张图片：${existing ? `追加到 ${existing} 个已有角色` : ""}${existing && created.size ? "，" : ""}${created.size ? `新建 ${created.size} 个角色` : ""}。`;
      if (!await confirmAction(`${message}\n需要处理的图片不会导入，可以稍后继续。`)) return;
      const signature = JSON.stringify([job.job_id, readyRows.map((row) => row.row_id)]);
      if (commitRequest?.signature !== signature) commitRequest = { signature, id: crypto.randomUUID() };
      let result;
      try {
        result = (await apiPost("batch-images/commit", { job_id: job.job_id, row_ids: readyRows.map((row) => row.row_id), request_id: commitRequest.id, confirm: true })).data;
      } catch (failure) {
        try { job = (await apiGet("batch-images", { job_id: job.job_id })).data; } catch { /* 保留当前视图，等待重试 */ }
        throw failure;
      }
      job = result.job;
      commitRequest = null;
      const images = result.imported.reduce((total, item) => total + item.images, 0);
      const failures = result.failed.map((item) => `${item.id}：${item.error}`);
      showNotice(
        `已导入 ${result.imported.length} 个角色、新增 ${images} 张图片${result.skipped_duplicates ? `，${result.skipped_duplicates} 张重复图片已跳过` : ""}${failures.length ? `；${failures.length} 个角色失败：${failures.join("；")}` : ""}`,
        failures.length ? "error" : "success",
      );
      filter = job.items.some(attention) ? "attention" : "todo";
      await onImported();
    });
  }

  async function loadOptions() {
    options = (await apiGet("batch-images")).data;
    setSettings(job?.settings || options.settings);
    const jobs = $("batch-resume-job");
    jobs.replaceChildren(...options.jobs.map((item) => new Option(`${new Date(item.created_at * 1000).toLocaleString()} · ${item.items?.length ?? item.count ?? 0} 张`, item.job_id)));
  }

  async function open() {
    dialog.showModal();
    if (job && !job.items.some((row) => pending(row) || attention(row))) job = null;
    await task(loadOptions);
  }

  // ---- 事件 ----
  $("batch-images-open").addEventListener("click", open);
  $("batch-images-close").addEventListener("click", () => { if (!busy) dialog.close(); });
  dialog.addEventListener("cancel", (event) => { if (busy || openPicker) { event.preventDefault(); if (openPicker) { openPicker = null; render(); } } });
  bindDropZone($("batch-drop-zone"), {
    input: $("batch-images-files"),
    folderInput: $("batch-images-folders"),
    onFiles: upload,
    isBusy: () => busy,
    pasteScope: $("batch-drop-zone"),
  });
  $("batch-pick-folder").addEventListener("click", () => $("batch-images-folders").click());
  $("batch-filter").addEventListener("click", (event) => {
    const target = event.target.closest("[data-filter]");
    if (!target) return;
    filter = target.dataset.filter;
    render();
  });
  $("batch-import").addEventListener("click", commit);
  $("batch-new").addEventListener("click", () => { job = null; selected.clear(); expanded.clear(); thumbnails.clear(); openPicker = null; $("batch-status").textContent = ""; render(); });
  $("batch-resume").addEventListener("click", () => task(async () => {
    const id = $("batch-resume-job").value;
    if (!id) throw new Error("请先选择批次。");
    job = (await apiGet("batch-images", { job_id: id })).data;
    setSettings(job.settings);
    filter = "todo";
  }));
  for (const id of ["batch-male-keywords", "batch-female-keywords", "batch-male-pool", "batch-female-pool"]) $(id).addEventListener("input", () => { defaultsRequest = null; });
  $("batch-settings-apply").addEventListener("click", () => task(async () => {
    if (!job) { $("batch-status").textContent = "设置会用于下一批上传。"; return; }
    job = (await apiPost("batch-images/update", { job_id: job.job_id, settings: settings() })).data;
    showNotice("已按新设置重新匹配；手动改过的图片保持不变。", "success");
  }));
  $("batch-defaults-save").addEventListener("click", () => task(async () => {
    defaultsRequest ??= crypto.randomUUID();
    const value = (await apiPost("batch-images/defaults", { settings: settings(), expected_revision: options.revision, request_id: defaultsRequest })).data;
    options.settings = value.settings;
    options.revision = value.revision;
    defaultsRequest = null;
    await onDefaultsSaved();
    showNotice("已保存为默认匹配设置。", "success");
  }));
  render();
}
