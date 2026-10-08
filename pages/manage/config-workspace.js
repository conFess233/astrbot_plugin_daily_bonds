// 四类配置视图共用原有字段、草稿与保存流程。
export const CONFIG_PAGES = { switches: '主要开关', parameters: '参数配置', keywords: '关键词', messages: '回复词' };
const SECTIONS = { wife: '老婆', husband: '老公', member: '群友', steal: '抢夺', gift: '赠送与邀请', divorce: '离婚', lists: '列表', statistics: '统计排行', feedback: '通用反馈', commands: '触发方式', access: '权限与名单', reset: '每日重置', resources: '资源维护' };
const REPLY_PREFIXES = ['messages.', 'reply_quote.', 'reply_enabled.'];
export function configPage(path, value) {
  if (REPLY_PREFIXES.some((prefix) => path.startsWith(prefix))) return 'messages';
  if (typeof value === 'boolean' || /^modes\.(wife|husband)\.pool_ids$/.test(path)) return 'switches';
  if (path.startsWith('commands.keywords.') || path === 'commands.extra_prefixes' || /^batch_import\..+_keywords$/.test(path)) return 'keywords';
  return 'parameters';
}
function functionSection(path) {
  const parts = path.split('.'), key = parts.at(-1);
  if (parts[0] === 'modes') return parts[1];
  if (REPLY_PREFIXES.some((prefix) => path.startsWith(prefix)) || path.startsWith('commands.keywords.')) {
    if (/^(draw_|designated_|capacity_full_single_)/.test(key) || /^list_(wife|husband|member)$/.test(key) || key === 'admin_set_wife' || parts[0] === 'commands' && /^(steal_|gift_|divorce_|list_)/.test(key)) {
      if (key.endsWith('wife') || key === 'list_characters') return 'wife';
      if (key.endsWith('husband')) return 'husband';
      if (key.endsWith('member') || key === 'list_members') return 'member';
    }
    if (/steal|stolen|target_protected|no_target_relationship/.test(key)) return 'steal';
    if (/gift|invite|restart_invalidated|participant_ineligible/.test(key)) return 'gift';
    if (/divorc/.test(key)) return 'divorce';
    if (/rank|affection|intimacy|activity/.test(key)) return 'statistics';
    if (/list_|page|_slot|existing/.test(key)) return 'lists';
    return 'feedback';
  }
  if (['statistics', 'weights', 'display'].includes(parts[0])) return 'statistics';
  if (parts[0] === 'access') return 'access';
  if (parts[0] === 'reset') return 'reset';
  if (parts[0] === 'commands') return 'commands';
  return parts[0] === 'enabled' ? 'feedback' : 'resources';
}
export function initConfigWorkspace({ fields, state, changed, apiGet, label, globalOnly, activate, readValue, renderPool, showNotice }) {
  const $ = (selector) => document.querySelector(selector);
  const valueAt = (object, path) => path.split('.').reduce((value, key) => value?.[key], object);
  let inputs = new Map(), cards = [], fonts = [], page = 'switches', previewSequence = 0;
  const currentSections = {}, selection = new Set();
  const inputFor = (path) => inputs.get(path);
  const flagFor = (path) => fields.querySelector(`[data-override-path="${path}"]`);
  const slotFor = (path) => inputFor(path)?.closest('[data-config-slot]');
  const readOnly = (path) => state.scopeId !== 'global' && globalOnly(path);
  function status(path) {
    const slot = slotFor(path); if (!slot) return;
    const local = !!flagFor(path)?.checked; slot.classList.remove('inherited');
    const badge = slot.querySelector(':scope > .config-origin');
    if (badge) { badge.hidden = state.scopeId === 'global'; badge.textContent = readOnly(path) ? '仅全局设置' : local ? '本群覆盖' : '继承全局'; }
    const restore = slot.querySelector(':scope > [data-restore-path]'); if (restore) { restore.disabled = !local; restore.hidden = !local; }
    const input = inputFor(path); input.disabled = readOnly(path);
    for (const check of slot.querySelectorAll('.pool-picker input')) check.disabled = input.disabled;
  }
  function markOverride(path) { if (readOnly(path)) return; const flag = flagFor(path); if (flag) flag.checked = true; status(path); }
  function assign(path, value, inherited = false) {
    const input = inputFor(path); if (!input || readOnly(path)) return false;
    if (input.type === 'checkbox') input.checked = value;
    else input.value = Array.isArray(value) ? value.join('\n') : path.endsWith('.steal_probability') ? String(value * 100) : String(value);
    if (input.type === 'hidden') renderPool(slotFor(path), input);
    const flag = flagFor(path); if (flag) flag.checked = !inherited;
    status(path); updateSelection(); return true;
  }
  function updateSelection() {
    const quoteCount = [...selection].filter((path) => inputs.has(path.replace('messages.', 'reply_quote.'))).length;
    $('#config-selected-count').textContent = `已选 ${selection.size} 条回复（含其他分区）· ${quoteCount} 条支持引用 · 只修改草稿`;
    for (const id of ['config-enable', 'config-disable']) $('#' + id).disabled = selection.size === 0;
    $('#config-inherit').disabled = !selection.size || state.scopeId === 'global';
    for (const id of ['config-quote-enable', 'config-quote-disable']) $('#' + id).disabled = quoteCount === 0;
  }
  function showSection(section) {
    currentSections[page] = section;
    for (const button of $('#config-section-tabs').querySelectorAll('[data-config-section]')) { const selected = button.dataset.configSection === section; button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1; }
    for (const card of cards) card.hidden = card.dataset.page !== page || page !== 'switches' && card.dataset.section !== section;
    for (const block of fields.querySelectorAll('[data-function-section]')) block.hidden = page !== 'switches' && block.dataset.functionSection !== section;
    const canCopy = page === 'parameters' && ['wife', 'husband', 'member'].includes(section);
    $('#config-mode-copy').hidden = !canCopy;
    $('#config-copy-source').textContent = canCopy ? `来源：${SECTIONS[section]}。仅同步本次修改的参数。` : '';
    for (const check of document.querySelectorAll('[data-copy-mode]')) { check.disabled = check.dataset.copyMode === section; if (check.disabled) check.checked = false; }
  }
  function select(next) {
    page = next;
    for (const group of fields.querySelectorAll('.config-group')) group.hidden = group.dataset.page !== page;
    $('#config-reply-bulk').hidden = page !== 'messages';
    const nav = $('#config-section-tabs'); nav.replaceChildren(); nav.hidden = page === 'switches';
    const sections = Object.keys(SECTIONS).filter((section) => cards.some((card) => card.dataset.page === page && card.dataset.section === section));
    for (const section of sections) { const button = document.createElement('button'); button.type = 'button'; button.className = 'config-tab'; button.dataset.configSection = section; button.setAttribute('role', 'tab'); button.textContent = SECTIONS[section]; button.addEventListener('click', () => showSection(section)); nav.append(button); }
    if (!sections.includes(currentSections[page])) currentSections[page] = sections[0];
    showSection(currentSections[page]); updateSelection();
  }
  function focus(path) {
    const input = inputFor(path); if (!input) return;
    const card = input.closest('.config-field'); activate(card.dataset.page); showSection(card.dataset.section);
    const editor = card.querySelector('.template-editor'); if (editor) editor.open = true;
    $('#config-search-results').hidden = true; $('#config-search').value = '';
    queueMicrotask(() => { card.scrollIntoView({ block: 'center' }); if (input.disabled) { card.tabIndex = -1; card.focus(); } else if (input.type === 'hidden') card.querySelector('.pool-picker input:not(:disabled)')?.focus(); else input.focus(); });
  }
  function search() {
    const results = $('#config-search-results'); results.replaceChildren();
    const query = $('#config-search').value.trim().toLocaleLowerCase(); results.hidden = !query; if (!query) return;
    const matches = cards.filter((card) => `${[...card.querySelectorAll('[data-config-path]')].map((input) => input.dataset.configPath).join(' ')} ${card.textContent} ${inputFor(card.dataset.fieldPath)?.value || ''}`.toLocaleLowerCase().includes(query));
    for (const card of matches.slice(0, 30)) { const button = document.createElement('button'); button.type = 'button'; button.className = 'config-search-result'; button.textContent = `${CONFIG_PAGES[card.dataset.page]} › ${SECTIONS[card.dataset.section]} › ${label(card.dataset.fieldPath)}`; button.addEventListener('click', () => focus(card.dataset.fieldPath)); results.append(button); }
    if (!matches.length) results.textContent = '没有匹配的设置。';
  }
  function enhance() {
    inputs = new Map([...fields.querySelectorAll('[data-config-path]')].map((input) => [input.dataset.configPath, input])); cards = []; selection.clear();
    for (const [path, input] of inputs) {
      const slot = input.closest('.config-field'); slot.dataset.configSlot = path; slot.dataset.fieldPath = path;
      slot.dataset.page = configPage(path, valueAt(state.savedConfig, path)); slot.dataset.section = functionSection(path);
      slot.querySelector('.config-override')?.setAttribute('hidden', '');
      const badge = document.createElement('small'); badge.className = 'config-origin'; slot.append(badge);
      if (flagFor(path)) { const restore = document.createElement('button'); restore.type = 'button'; restore.className = 'button text-button'; restore.textContent = '恢复继承'; restore.dataset.restorePath = path; restore.addEventListener('click', () => { assign(path, valueAt(state.globalConfig, path), true); changed(); }); slot.append(restore); }
      slot.addEventListener('input', (event) => { if (event.target.dataset.configPath !== path) return; markOverride(path); changed(); });
      slot.querySelector('.pool-picker')?.addEventListener('change', () => { markOverride(path); changed(); }); status(path);
    }
    for (const [path] of inputs) {
      if (!path.startsWith('reply_quote.') && !path.startsWith('reply_enabled.')) continue;
      const parent = slotFor(path.replace(/^reply_(?:quote|enabled)\./, 'messages.')); if (!parent) continue;
      const slot = slotFor(path); slot.className = 'config-inline-toggle';
      slot.querySelector('label.field > span').textContent = path.startsWith('reply_enabled.') ? '启用回复' : '引用触发消息'; parent.append(slot);
    }
    for (const [path, input] of inputs) {
      if (path.startsWith('reply_quote.') || path.startsWith('reply_enabled.')) continue;
      const card = slotFor(path); if (path === 'schema_version') { card.remove(); inputs.delete(path); continue; } cards.push(card);
      if (path.startsWith('messages.')) {
        const controls = document.createElement('div'); controls.className = 'reply-controls'; const enabledPath = path.replace('messages.', 'reply_enabled.');
        if (inputs.has(enabledPath)) {
          const selectionLabel = document.createElement('label'); selectionLabel.className = 'config-selection'; const check = document.createElement('input'); check.type = 'checkbox'; check.dataset.selectConfig = path; check.setAttribute('aria-label', `选择${label(path)}`);
          check.addEventListener('change', () => { check.checked ? selection.add(path) : selection.delete(path); updateSelection(); }); selectionLabel.append(check, document.createTextNode('选择')); controls.append(selectionLabel, slotFor(enabledPath));
          const quote = slotFor(path.replace('messages.', 'reply_quote.')); if (quote) controls.append(quote);
          const applies = document.createElement('small'); applies.className = 'muted'; applies.textContent = ['wife', 'husband', 'member'].includes(card.dataset.section) ? `适用：${SECTIONS[card.dataset.section]}` : '共用文案 · 影响适用玩法'; controls.append(applies);
        }
        const heading = document.createElement('h4'); heading.textContent = label(path); card.prepend(heading);
        const editor = document.createElement('details'); editor.className = 'template-editor'; const summary = document.createElement('summary'); summary.textContent = '编辑文案 · '; const preview = document.createElement('span'); preview.className = 'muted';
        const updatePreview = () => { preview.textContent = input.value.split('\n')[0].slice(0, 60) || (path.startsWith('messages.titles.') ? '空文字，隐藏对应标题或标签' : '空文案，不发送'); }; updatePreview(); input.addEventListener('input', updatePreview); summary.append(preview); editor.append(summary);
        for (const child of [...card.children]) if (child !== heading) editor.append(child);
        const variables = document.createElement('div'); variables.className = 'template-variables';
        const variableHint = document.createElement('p'); variableHint.className = 'muted'; variableHint.textContent = '点击变量插入文案；名称优先使用群名片，无对应对象时显示“群友”或“对象”。'; editor.append(variableHint);
        for (const variable of state.templateFields[path]?.allowed || []) {
          const token = `{${variable}}`; const meaning = state.templateFields[path].descriptions?.[variable];
          const button = document.createElement('button'); button.type = 'button'; button.className = 'variable-chip'; button.textContent = meaning ? `${token} · ${meaning}` : token;
          button.addEventListener('click', () => { input.focus(); input.setRangeText(token, input.selectionStart, input.selectionEnd, 'end'); input.dispatchEvent(new Event('input', { bubbles: true })); }); variables.append(button);
        }
        editor.append(variables); card.append(controls, editor);
      }
      if (path === 'resources.font_id') {
        const select = document.createElement('select'); select.dataset.configPath = path; select.disabled = input.disabled; select.add(new Option(fonts.length ? '自动选择本地字体' : '未读取到本地字体（使用文字回复）', 'auto')); for (const font of fonts) select.add(new Option(font.name, font.id)); if (input.value !== 'auto' && !fonts.some((font) => font.id === input.value)) select.add(new Option('所选字体不可用（自动选择本地字体）', input.value)); select.value = input.value; input.replaceWith(select); inputs.set(path, select);
        const image = document.createElement('img'); image.className = 'font-preview'; image.alt = '字体中文预览'; image.hidden = true; card.append(image); const button = document.createElement('button'); button.type = 'button'; button.className = 'button secondary'; button.textContent = '预览字体';
        button.addEventListener('click', async () => { const sequence = ++previewSequence; button.disabled = true; try { const result = await apiGet('fonts/preview', { font_id: select.value }); if (sequence === previewSequence) { image.src = result.data.src; image.hidden = false; } } catch (error) { showNotice(error.message || '预览失败。', 'error'); } finally { button.disabled = false; } }); card.append(button);
      }
    }
    for (const group of fields.querySelectorAll('.config-group')) {
      const groupPage = group.id.replace('config-group-', ''); group.dataset.page = groupPage; const container = group.querySelector('.config-grid'); container.replaceChildren(); const groupCards = cards.filter((card) => card.dataset.page === groupPage);
      if (groupPage === 'switches') {
        const wrapper = document.createElement('div'); wrapper.className = 'switch-matrix-wrap'; const table = document.createElement('table'); table.className = 'switch-matrix'; const head = table.createTHead().insertRow();
        for (const title of ['功能', '老婆', '老公', '群友']) { const th = document.createElement('th'); th.scope = 'col'; th.textContent = title; head.append(th); }
        const body = table.createTBody(); const modeCards = groupCards.filter((card) => /^modes\.[^.]+\./.test(card.dataset.fieldPath) && inputFor(card.dataset.fieldPath).type === 'checkbox');
        for (const key of [...new Set(modeCards.map((card) => card.dataset.fieldPath.split('.').at(-1)))]) { const row = body.insertRow(); const th = document.createElement('th'); th.scope = 'row'; th.textContent = label(modeCards.find((card) => card.dataset.fieldPath.endsWith('.' + key)).dataset.fieldPath).split(' · ').at(-1); row.append(th); for (const mode of ['wife', 'husband', 'member']) { const cell = row.insertCell(); const card = modeCards.find((card) => card.dataset.fieldPath === `modes.${mode}.${key}`); if (card) { card.classList.add('config-switch-cell'); cell.append(card); } else cell.textContent = '—'; } }
        wrapper.append(table); container.append(wrapper); const poolGrid = document.createElement('div'); poolGrid.className = 'config-grid'; for (const card of groupCards.filter((card) => card.dataset.fieldPath.endsWith('.pool_ids'))) poolGrid.append(card); container.append(poolGrid);
        const heading = document.createElement('h4'); heading.textContent = '通用开关'; container.append(heading); const commonGrid = document.createElement('div'); commonGrid.className = 'config-grid'; for (const card of groupCards.filter((card) => !card.dataset.fieldPath.startsWith('modes.'))) commonGrid.append(card); container.append(commonGrid);
      } else {
        for (const section of Object.keys(SECTIONS)) { const sectionCards = groupCards.filter((card) => card.dataset.section === section); if (!sectionCards.length) continue; const block = document.createElement('section'); block.dataset.functionSection = section; const heading = document.createElement('h4'); heading.textContent = SECTIONS[section]; block.append(heading); const grid = document.createElement('div'); grid.className = 'config-grid'; grid.append(...sectionCards); block.append(grid); container.append(block); }
      }
    }
    select(state.configGroup);
  }
  $('#config-search').addEventListener('input', search);
  $('#config-search').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); $('#config-search-results button')?.click(); } if (event.key === 'ArrowDown') { event.preventDefault(); $('#config-search-results button')?.focus(); } if (event.key === 'Escape') $('#config-search-results').hidden = true; });
  $('#config-section-tabs').addEventListener('keydown', (event) => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; const buttons = [...$('#config-section-tabs').querySelectorAll('button')]; const index = buttons.indexOf(document.activeElement); const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length; event.preventDefault(); buttons[next]?.click(); buttons[next]?.focus(); });
  function choose(all) { if (page !== 'messages') return; for (const card of cards) if (card.dataset.page === 'messages' && (all || card.dataset.section === currentSections.messages)) { const check = card.querySelector('[data-select-config]'); if (check) { check.checked = true; selection.add(card.dataset.fieldPath); } } updateSelection(); }
  $('#config-select-all').addEventListener('click', () => choose(false)); $('#config-select-every-reply').addEventListener('click', () => choose(true));
  $('#config-select-none').addEventListener('click', () => { selection.clear(); for (const check of fields.querySelectorAll('[data-select-config]')) check.checked = false; updateSelection(); });
  for (const [id, prefix, value] of [['config-enable', 'reply_enabled.', true], ['config-disable', 'reply_enabled.', false], ['config-quote-enable', 'reply_quote.', true], ['config-quote-disable', 'reply_quote.', false]]) $('#' + id).addEventListener('click', () => { if (page !== 'messages') return; for (const path of selection) assign(path.replace('messages.', prefix), value); changed(); updateSelection(); });
  $('#config-inherit').addEventListener('click', () => { if (page !== 'messages' || state.scopeId === 'global') return; for (const path of selection) for (const prefix of ['messages.', 'reply_enabled.', 'reply_quote.']) { const field = path.replace('messages.', prefix); if (inputs.has(field)) assign(field, valueAt(state.globalConfig, field), true); } changed(); updateSelection(); });
  $('#config-copy-modes').addEventListener('click', () => {
    const source = currentSections.parameters; if (page !== 'parameters' || !['wife', 'husband', 'member'].includes(source)) return;
    try { const values = cards.filter((card) => card.dataset.page === 'parameters' && card.dataset.section === source).map((card) => [card.dataset.fieldPath, readValue(inputFor(card.dataset.fieldPath))]).filter(([path, value]) => JSON.stringify(value) !== JSON.stringify(valueAt(state.savedConfig, path))); const targets = [...document.querySelectorAll('[data-copy-mode]:checked')].map((check) => check.dataset.copyMode); if (!values.length || !targets.length) { showNotice('请先修改本玩法参数，并勾选需要同步的玩法。', 'error'); return; } let count = 0; for (const mode of targets) for (const [path, value] of values) if (assign(path.replace(`modes.${source}.`, `modes.${mode}.`), value)) count += 1; changed(); showNotice(`已同步 ${count} 项参数到草稿。`, 'success'); } catch (error) { showNotice(error.message, 'error'); }
  });
  return { enhance, select, focus, loadFonts: async () => { fonts = (await apiGet('fonts')).data; } };
}
