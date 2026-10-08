// 原生 dialog 的批量上传、预览编辑、结果确认与未完成任务恢复。
import { confirmAction } from './confirm-dialog.js';
export function initBatchImages({ apiGet, apiPost, apiUpload, showNotice, onImported, onDefaultsSaved }) {
  const $ = (id) => document.querySelector(`#${id}`);
  const dialog = $("batch-images-dialog");
  let options, job, busy = false, page = 0, defaultsRequest, commitRequest;
  const selectedRows = new Set();
  let characterChoices = new Map();
  let dirty = new Map(), thumbnails = new Map(), previewQueue = Promise.resolve(), searchTimer, searchSequence = 0;
  const node = (tag, text = "", className = "") => {
    const value = document.createElement(tag);
    if (text) value.textContent = text;
    if (className) value.className = className;
    return value;
  };
  const pending = (row) => row.status === "pending";
  const ready = (row) => pending(row) && !row.upload_error && !row.excluded && (!row.needs_manual || row.manual_approved) && !row.errors?.length;
  const manual = (row) => pending(row) && (row.needs_manual && !row.manual_approved || row.errors?.length);
  const error = (message) => { $("batch-error").textContent = message; $("batch-error").hidden = !message; };
  async function task(work) {
    if (busy) return;
    busy = true; $("batch-images-controls").disabled = true; $("batch-images-close").disabled = true;
    $("batch-result-confirm").disabled = true; error("");
    try { await work(); } catch (failure) { error(failure.message || "操作失败，草稿仍保留。"); }
    finally {
      busy = false; $("batch-images-controls").disabled = false; $("batch-images-close").disabled = false;
      $("batch-result-confirm").disabled = false;
    }
  }
  function settings() {
    const words = (id) => $(id).value.split(/[,，\n]/).map((value) => value.trim()).filter(Boolean);
    return { male_keywords: words("batch-male-keywords"), female_keywords: words("batch-female-keywords"),
      male_pool_id: $("batch-male-pool").value, female_pool_id: $("batch-female-pool").value };
  }
  function setSettings(value) {
    $("batch-male-keywords").value = value.male_keywords.join(", ");
    $("batch-female-keywords").value = value.female_keywords.join(", ");
    for (const gender of ["male", "female"]) {
      const select = $(`batch-${gender}-pool`);
      select.replaceChildren(new Option("请选择默认卡池", ""));
      for (const pool of options.pools.filter((pool) => pool.mode === (gender === "male" ? "husband" : "wife"))) {
        select.add(new Option(`${pool.name} · ${pool.id}`, pool.id));
      }
      select.value = value[`${gender}_pool_id`];
    }
  }
  function setJob(value) { job = value; dirty.clear(); render(); }
  function changed(row, field, value) {
    const targetChanged = field === "target_id" && row.existing && row.target_id !== value;
    row[field] = value;
    const patch = dirty.get(row.row_id) || { row_id: row.row_id };
    if (targetChanged) { row.pool_ids = []; patch.pool_ids = []; }
    patch[field] = value; dirty.set(row.row_id, patch); commitRequest = null;
    if (["target_id", "existing", "name", "gender", "pool_ids", "enabled"].includes(field)) {
      row.manual_approved = false; patch.manual_approved = false;
      row.needs_manual = true;
      const approval = $("batch-image-rows").querySelector(`[data-row-id="${row.row_id}"] [data-manual-approved]`);
      if (approval) approval.checked = false;
    }
  }
  async function saveDraft() {
    if (!job || !dirty.size) return;
    const patches = [...dirty.values()].map((patch) => {
      const row = job.items.find((item) => item.row_id === patch.row_id);
      if (row.existing) {
        return Object.fromEntries(Object.entries(patch).filter(([key]) => ["row_id", "target_id", "existing", "pool_ids", "relative_path", "manual_approved", "status"].includes(key)));
      }
      return patch;
    });
    setJob((await apiPost("batch-images/update", { job_id: job.job_id, items: patches })).data);
  }
  function field(parent, labelText, input) {
    const label = node("label", "", "field"); label.append(node("span", labelText), input); parent.append(label); return input;
  }
  function checkbox(parent, text, checked, onChange) {
    const input = node("input"); input.type = "checkbox"; input.checked = checked;
    const label = node("label", "", "check-field"); label.append(input, document.createTextNode(text)); parent.append(label);
    input.addEventListener("change", () => onChange(input.checked)); return input;
  }
  function rowCard(row) {
    const card = node("article", "", `batch-row${row.needs_manual || row.errors?.length ? " manual" : ""}`);
    card.dataset.rowId = row.row_id;
    const image = node("img"); image.alt = row.filename; card.append(image);
    if (row.hash) {
      previewQueue = previewQueue.then(async () => {
        if (!card.isConnected) return;
        const key = `${job.job_id}/${row.row_id}`;
        try {
          if (!thumbnails.has(key)) thumbnails.set(key, (await apiGet(`batch-images/${job.job_id}/preview/${row.row_id}`)).data.src);
          if (card.isConnected) image.src = thumbnails.get(key);
        } catch { if (card.isConnected) image.alt = "图片暂不可预览"; }
      });
    } else image.alt = row.upload_error ? "上传失败" : "暂无图片预览";
    if (row.status !== "imported") checkbox(card, "选择", selectedRows.has(row.row_id), (value) => { value ? selectedRows.add(row.row_id) : selectedRows.delete(row.row_id); updateSelection(); });
    card.append(node("strong", row.name || "尚未关联角色"), node("span", row.target_id ? `角色 ID：${row.target_id}` : "尚未关联角色", "muted"), node("span", `文件：${row.filename}`, "muted"));
    if (row.upload_error) card.append(node("p", `上传失败：${(row.errors || []).join("；") || "请重新上传原文件"}`, "batch-row-note"));
    if (row.duplicate_sources?.length) card.append(node("p", `重复来源：${row.duplicate_sources.join("、")}${row.duplicate_skip ? " · 已自动跳过" : " · 可跨角色复用"}`, "batch-row-note"));
    const body = node("details", "", "batch-row-details"); body.append(node("summary", "详情与编辑")); card.append(body);
    body.append(node("h3", row.name || "尚未关联角色"), node("p", `原文件：${row.filename}`, "muted"), node("p", `${row.width || "—"} × ${row.height || "—"} · ${Math.round((row.byte_size || 0) / 1024)} KiB · ${row.mime_type || "未通过格式校验"}`, "muted"));
    const messages = [...(row.warnings || []), ...(row.errors || [])];
    if (messages.length) body.append(node("p", messages.join("；"), "batch-row-note"));
    if (row.status === "imported") { body.append(node("p", `已导入 ${row.name} · ${row.target_id}`)); return card; }
    if (row.upload_error) {
      const skip = node("button", row.status === "skipped" ? "已跳过失败文件" : "跳过失败文件", "button secondary"); skip.type = "button"; skip.disabled = row.status === "skipped";
      skip.addEventListener("click", () => { changed(row, "status", "skipped"); render(); }); body.append(skip); return card;
    }
    checkbox(body, "跳过此图片（不删除已导入内容）", row.status === "skipped", (value) => { changed(row, "status", value ? "skipped" : "pending"); render(); });
    const controls = node("fieldset", "", "control-group"); controls.disabled = row.status === "skipped" && !row.duplicate_skip; body.append(controls);
    checkbox(controls, "关联已有角色：追加图片与卡池，保留档案", row.existing, (value) => {
      changed(row, "existing", value);
      if (!value) changed(row, "target_id", `char_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`);
      render();
    });
    const grid = node("div", "", "form-grid"); controls.append(grid);
    const id = node("input"); id.value = row.target_id || ""; id.maxLength = 128;
    if (row.existing) id.setAttribute("list", "batch-character-options");
    field(grid, row.existing ? "关联角色 ID（更换目标，不改原 ID）" : "新角色 ID", id);
    id.addEventListener("input", () => changed(row, "target_id", id.value.trim()));
    id.addEventListener("change", render);
    const name = node("input"); name.value = row.name || ""; name.maxLength = 120; name.disabled = row.existing;
    field(grid, "角色名称", name); name.addEventListener("input", () => changed(row, "name", name.value));
    name.addEventListener("change", render);
    const gender = node("select"); for (const [value, text] of [["female", "女"], ["male", "男"], ["unspecified", "未指定"]]) gender.add(new Option(text, value));
    gender.value = row.gender; gender.disabled = row.existing; field(grid, "性别", gender);
    gender.addEventListener("change", () => { changed(row, "gender", gender.value); changed(row, "pool_ids", []); render(); });
    const pools = node("fieldset", "", "pool-membership"); pools.append(node("legend", "角色池")); grid.append(pools);
    const checks = node("div", "", "check-list"); pools.append(checks);
    for (const pool of options.pools) {
      if (row.gender !== "unspecified" && pool.mode !== (row.gender === "female" ? "wife" : "husband")) continue;
      const input = checkbox(checks, `${pool.name} · ${pool.id}`, (row.pool_ids || []).includes(pool.id), (checked) => {
        const selected = new Set(row.pool_ids || []); checked ? selected.add(pool.id) : selected.delete(pool.id);
        changed(row, "pool_ids", [...selected]);
        render();
      });
    }
    const enabled = checkbox(controls, "启用抽取", row.enabled !== false, (value) => { changed(row, "enabled", value); render(); }); enabled.disabled = row.existing;
    if (row.needs_manual || row.errors?.length || dirty.has(row.row_id)) {
      const approval = checkbox(controls, "我已核对目标角色、性别与卡池，确认此项", row.manual_approved, (value) => changed(row, "manual_approved", value));
      approval.dataset.manualApproved = "true";
    }
    return card;
  }
  function filteredRows() {
    const filter = $("batch-filter").value;
    return (job?.items || []).filter((row) => filter === "all" || filter === "duplicate" && row.duplicate_sources?.length || filter === "ready" && ready(row) || filter === "manual" && (manual(row) || row.status === "failed") || filter === "pending" && ["pending", "failed"].includes(row.status));
  }
  function updateSelection() { $("batch-selected-count").textContent = `已选 ${job?.items.filter((row) => selectedRows.has(row.row_id) && row.status !== "imported").length || 0} 张 · 卡池追加影响整个角色`; }
  function render() {
    const rows = filteredRows();
    page = Math.min(page, Math.max(0, Math.ceil(rows.length / 40) - 1));
    const container = $("batch-image-rows"); container.replaceChildren();
    const groups = new Map();
    function folder(path) {
      if (groups.has(path)) return groups.get(path);
      const parts = path ? path.split("/") : ["未分组"];
      const parent = path.includes("/") ? folder(path.slice(0, path.lastIndexOf("/"))) : container;
      const details = node("details", "", "batch-folder"); details.open = true;
      const summary = node("summary"); details.append(summary);
      const children = job.items.filter((row) => {
        const rowFolder = (row.relative_path || "").split("/").slice(0, -1).join("/");
        return row.status !== "imported" && (rowFolder === path || path && rowFolder.startsWith(path + "/"));
      });
      checkbox(summary, parts.at(-1), children.length > 0 && children.every((row) => selectedRows.has(row.row_id)), (value) => {
        for (const row of job.items) {
          const rowFolder = (row.relative_path || "").split("/").slice(0, -1).join("/");
          if (row.status !== "imported" && (rowFolder === path || path && rowFolder.startsWith(path + "/"))) value ? selectedRows.add(row.row_id) : selectedRows.delete(row.row_id);
        }
        render();
      });
      const remove = node("button", "排除此文件夹", "button secondary"); remove.type = "button";
      remove.addEventListener("click", (event) => {
        event.preventDefault();
        for (const row of job.items) {
          const rowFolder = (row.relative_path || "").split("/").slice(0, -1).join("/");
          if (row.status !== "imported" && (rowFolder === path || path && rowFolder.startsWith(path + "/"))) changed(row, "status", "skipped");
        }
        render();
      }); summary.append(remove);
      const grid = node("div", "", "batch-thumbnail-grid"); details.append(grid);
      parent.append(details); groups.set(path, grid); return grid;
    }
    for (const row of rows.slice(page * 40, page * 40 + 40)) folder((row.relative_path || "").split("/").slice(0, -1).join("/")).append(rowCard(row));
    $("batch-page").textContent = `${rows.length ? page * 40 + 1 : 0}–${Math.min(rows.length, page * 40 + 40)} / ${rows.length}`;
    $("batch-prev").disabled = page === 0; $("batch-next").disabled = (page + 1) * 40 >= rows.length;
    const items = job?.items || [];
    $("batch-status").textContent = job ? `已导入 ${items.filter((r) => r.status === "imported").length} · 可导入 ${items.filter(ready).length} · 待处理 ${items.filter((r) => manual(r) || r.status === "failed").length} · 重复 ${items.filter((r) => r.duplicate_sources?.length).length} · 失败 ${items.filter((r) => r.upload_error || r.errors?.length).length} · 跳过 ${items.filter((r) => r.status === "skipped").length} · 保留至 ${new Date(job.expires_at * 1000).toLocaleString()}` : "选择图片或文件夹开始新的批次，或恢复未完成批次。";
    $("batch-images-close").textContent = items.length && !items.some((r) => ["pending", "failed"].includes(r.status)) ? "完成并关闭" : "关闭并保留";
    updateSelection();
  }
  async function open() {
    dialog.showModal();
    $("batch-images-controls").hidden = false; $("batch-import-result").hidden = true;
    $("batch-images-title").textContent = "批量导入角色图"; $("batch-filter").value = "pending";
    if (job?.items.length && !job.items.some((row) => ["pending", "failed"].includes(row.status))) job = null;
    await task(async () => {
      options = (await apiGet("batch-images")).data;
      setSettings(job?.settings || options.settings);
      $("batch-bulk-pool").replaceChildren(new Option("不追加卡池", ""), ...options.pools.map((pool) => new Option(`${pool.name} · ${pool.id}`, pool.id)));
      const jobs = $("batch-resume-job"); jobs.replaceChildren(new Option("选择未完成批次", ""));
      for (const item of options.jobs) jobs.add(new Option(`${new Date(item.created_at * 1000).toLocaleString()} · ${item.items?.length ?? item.count ?? 0} 张 · ${item.job_id}`, item.job_id));
      clearTimeout(searchTimer); $("batch-character-search").value = "";
      await searchCharacters("", ++searchSequence);
      render();
    });
  }
  async function upload(files) {
    if (!files.length) return;
    await task(async () => {
      await saveDraft();
      if (files.length + (job?.items.length || 0) > options.limits.import_max_entries) throw new Error("文件数量超过批次条目上限，请分批选择。");
      if (!job) setJob((await apiPost("batch-images/create", { settings: settings() })).data);
      $("batch-upload-progress").hidden = false; $("batch-upload-progress").max = files.length;
      try {
        for (const [index, entry] of files.entries()) {
          const file = entry.file || entry; const relativePath = entry.path || file.webkitRelativePath || "";
          $("batch-status").textContent = `正在上传 ${index + 1}/${files.length}：${file.name}`;
          let reason = !/\.(png|jpe?g|webp)$/i.test(file.name) ? "仅支持 PNG / JPEG / WebP" : file.size > 12 * 1024 * 1024 ? "超过单张 12 MiB 限制" : "";
          let value;
          if (reason) value = await apiPost("batch-images/failure", { job_id: job.job_id, filename: file.name, relative_path: relativePath, error: reason });
          else {
            try { value = await apiUpload(`batch-images/${job.job_id}/upload`, file); }
            catch (failure) { value = await apiPost("batch-images/failure", { job_id: job.job_id, filename: file.name, relative_path: relativePath, error: failure.message || "上传失败，请重选此文件" }); }
          }
          if (value.data.item) {
            job.items.push(value.data.item);
            if (relativePath) dirty.set(value.data.item.row_id, {row_id: value.data.item.row_id, relative_path: relativePath});
          } else job = value.data;
          $("batch-upload-progress").value = index + 1;
        }
        await saveDraft();
        setJob((await apiGet("batch-images", {job_id: job.job_id})).data);
        page = 0; render();
      } finally { $("batch-upload-progress").hidden = true; $("batch-images-files").value = ""; $("batch-images-folders").value = ""; render(); }
    });
  }
  async function commit(manualPhase) {
    await task(async () => {
      await saveDraft();
      if (!job) throw new Error("请先选择并上传图片。");
      const rows = job.items.filter((row) => ready(row) && (!manualPhase || row.manual_approved));
      if (!rows.length) throw new Error(manualPhase ? "请修改待处理项，保存草稿并勾选审核确认。" : "没有可靠项，可进入待人工列表核对。");
      if (!await confirmAction(`将确认导入 ${rows.length} 张图片，已有角色追加图片和所选卡池，新角色按预览创建。继续吗？`)) return;
      const signature = JSON.stringify([job.job_id, rows.map((row) => row.row_id)]);
      if (commitRequest?.signature !== signature) commitRequest = { signature, id: crypto.randomUUID() };
      let result;
      try { result = (await apiPost("batch-images/commit", { job_id: job.job_id, row_ids: rows.map((row) => row.row_id), request_id: commitRequest.id, confirm: true })).data; }
      catch (failure) { try { setJob((await apiGet("batch-images", {job_id: job.job_id})).data); } catch { /* 保留本地草稿，等待用户明确重试。 */ } throw failure; }
      setJob(result.job);
      const success = $("batch-success-list"), failed = $("batch-failure-list"); success.replaceChildren(); failed.replaceChildren();
      for (const item of result.imported) success.append(node("li", `${item.name} · ${item.images} 张图片 · 卡池：${(item.pool_ids || []).join("、")}`));
      for (const item of result.failed) failed.append(node("li", `${item.id}：${item.error}`));
      if (!result.imported.length) success.append(node("li", "本次没有成功导入的角色。"));
      $("batch-import-result").hidden = false; $("batch-images-controls").hidden = true;
      $("batch-images-title").textContent = "批量导入结果";
      await onImported();
    });
  }
  $("batch-images-open").addEventListener("click", open);
  $("batch-images-close").addEventListener("click", () => task(async () => { await saveDraft(); dialog.close(); }));
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); if (!busy) $("batch-images-close").click(); });
  window.addEventListener("beforeunload", (event) => { if (dirty.size) { event.preventDefault(); event.returnValue = ""; } });
  $("batch-images-folders").addEventListener("change", (event) => upload([...event.target.files].filter((file) => /\.(png|jpe?g|webp)$/i.test(file.name))));
  $("batch-select-all").addEventListener("click", () => { for (const row of filteredRows()) if (row.status !== "imported") selectedRows.add(row.row_id); render(); });
  $("batch-select-none").addEventListener("click", () => { selectedRows.clear(); render(); });
  $("batch-bulk-apply").addEventListener("click", () => task(async () => {
    const target = $("batch-bulk-target").value.trim(), name = $("batch-bulk-name").value.trim(), pool = $("batch-bulk-pool").value;
    const character = target ? characterChoices.get(target) : null;
    if (target && !character) throw new Error("请搜索并选择一个有效的已有角色。");
    if (target && name) throw new Error("请选择已有角色或填写新角色名称，两者只选一种。");
    if (!target && !name && !pool) throw new Error("请填写关联角色或追加卡池。");
    const rows = job?.items.filter((row) => selectedRows.has(row.row_id) && row.status !== "imported" && !row.upload_error) || [];
    if (!rows.length) throw new Error("请先选择图片或文件夹。");
    const newId = name ? `char_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}` : null;
    for (const row of rows) {
      if (target || newId) { changed(row, "existing", !!target); changed(row, "target_id", target || newId); }
      if (newId) { changed(row, "name", name); changed(row, "gender", $("batch-bulk-gender").value); changed(row, "pool_ids", pool ? [pool] : []); }
      else if (target) { row.name = character.name; changed(row, "pool_ids", pool ? [pool] : []); }
      else if (pool) changed(row, "pool_ids", [...new Set([...row.pool_ids, pool])]);
      changed(row, "status", "pending");
    }
    await saveDraft(); render();
  }));
  $("batch-bulk-approve").addEventListener("click", () => task(async () => {
    for (const row of job?.items || []) if (selectedRows.has(row.row_id) && row.status === "pending" && !row.upload_error) changed(row, "manual_approved", true);
    await saveDraft(); render();
  }));
  $("batch-bulk-skip").addEventListener("click", () => { for (const row of job?.items || []) if (selectedRows.has(row.row_id) && row.status !== "imported") changed(row, "status", "skipped"); render(); });
  $("batch-images-files").addEventListener("change", (event) => upload([...event.target.files]));
  const drop = $("batch-drop-zone");
  drop.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); $("batch-images-files").click(); } });
  for (const name of ["dragenter", "dragover"]) drop.addEventListener(name, (event) => { event.preventDefault(); if (!busy) drop.classList.add("dragging"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
  async function readEntry(entry, parent = "") {
    const path = parent + entry.name;
    if (entry.isFile) {
      if (!/\.(png|jpe?g|webp)$/i.test(entry.name)) return [];
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject)); return [{file, path}];
    }
    const reader = entry.createReader(); const result = [];
    while (true) {
      const children = await new Promise((resolve, reject) => reader.readEntries(resolve, reject)); if (!children.length) break;
      for (const child of children) result.push(...await readEntry(child, path + "/"));
    }
    return result;
  }
  drop.addEventListener("drop", async (event) => {
    event.preventDefault(); drop.classList.remove("dragging"); if (busy) return;
    const entries = [...event.dataTransfer.items].map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
    const fallback = [...event.dataTransfer.files];
    try { const files = []; for (const entry of entries) files.push(...await readEntry(entry)); await upload(entries.length ? files : fallback); }
    catch (failure) { error(failure.message || "文件夹读取失败，请使用添加文件夹入口重试。"); }
  });
  $("batch-draft-save").addEventListener("click", () => task(async () => { await saveDraft(); $("batch-status").textContent += " · 草稿已保存"; }));
  $("batch-filter").addEventListener("change", () => { page = 0; render(); });
  $("batch-prev").addEventListener("click", () => { page -= 1; render(); });
  $("batch-next").addEventListener("click", () => { page += 1; render(); });
  $("batch-import-ready").addEventListener("click", () => commit(false));
  $("batch-import-manual").addEventListener("click", () => commit(true));
  $("batch-result-confirm").addEventListener("click", () => {
    $("batch-import-result").hidden = true; $("batch-images-controls").hidden = false;
    $("batch-images-title").textContent = "待人工处理的图片"; $("batch-filter").value = "manual"; page = 0; render();
    if (!job.items.some((row) => ["pending", "failed"].includes(row.status))) { showNotice("本批导入已完成。"); dialog.close(); }
  });
  $("batch-new").addEventListener("click", () => task(async () => { await saveDraft(); job = null; dirty.clear(); selectedRows.clear(); thumbnails.clear(); page = 0; render(); }));
  $("batch-resume").addEventListener("click", () => task(async () => {
    const id = $("batch-resume-job").value; if (!id) throw new Error("请先选择未完成批次。");
    await saveDraft(); setJob((await apiGet("batch-images", {job_id: id})).data); setSettings(job.settings); page = 0; render();
  }));
  for (const id of ["batch-male-keywords", "batch-female-keywords", "batch-male-pool", "batch-female-pool"]) $(id).addEventListener("input", () => { defaultsRequest = null; });
  $("batch-settings-apply").addEventListener("click", () => task(async () => {
    await saveDraft(); if (job) setJob((await apiPost("batch-images/update", { job_id: job.job_id, settings: settings() })).data);
    else $("batch-status").textContent = "检测设置将用于下一批上传。";
  }));
  $("batch-defaults-save").addEventListener("click", () => task(async () => {
    defaultsRequest ??= crypto.randomUUID();
    const value = (await apiPost("batch-images/defaults", { settings: settings(), expected_revision: options.revision, request_id: defaultsRequest })).data;
    options.settings = value.settings; options.revision = value.revision; defaultsRequest = null;
    await onDefaultsSaved(); $("batch-status").textContent = "已保存全局检测默认值。本批草稿需点击应用检测才重新匹配。";
  }));
  async function searchCharacters(query, sequence) {
    const select = $("batch-bulk-target"), status = $("batch-character-search-status");
    try {
      const value = (await apiGet("characters", { q: query, limit: 100 })).data;
      if (sequence !== searchSequence) return;
      characterChoices = new Map(value.items.map((character) => [character.id, character]));
      select.replaceChildren(new Option(value.items.length ? "请选择已有角色" : "没有匹配的角色", ""), ...value.items.map((character) => new Option(`${character.name} · ${character.id}`, character.id)));
      $("batch-character-options").replaceChildren(...value.items.map((character) => new Option(`${character.name} · ${character.id}`, character.id)));
      status.textContent = value.total > value.items.length ? `显示前 ${value.items.length} 个，共 ${value.total} 个；请输入搜索词缩小范围。` : `找到 ${value.items.length} 个角色。`;
    } catch (failure) {
      if (sequence !== searchSequence) return;
      select.replaceChildren(new Option("角色加载失败，请重新搜索", ""));
      status.textContent = failure.message || "角色搜索失败。";
    } finally { if (sequence === searchSequence) select.disabled = false; }
  }
  $("batch-character-search").addEventListener("input", (event) => {
    clearTimeout(searchTimer); const sequence = ++searchSequence, query = event.target.value.trim();
    $("batch-bulk-target").value = ""; $("batch-bulk-target").disabled = true;
    $("batch-character-search-status").textContent = "正在搜索…";
    searchTimer = setTimeout(() => searchCharacters(query, sequence), 250);
  });
}
