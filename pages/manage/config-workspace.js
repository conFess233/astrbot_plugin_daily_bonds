// 配置编辑仍使用原有草稿与保存流程；这里只调整展示和批量操作。
export function initConfigWorkspace({ fields, state, changed, apiGet, label, globalOnly }) {
  const $ = (selector) => document.querySelector(selector);
  const inputFor = (path) => [...fields.querySelectorAll('[data-config-path]')].find((input) => input.dataset.configPath === path);
  const overrideFor = (path) => [...fields.querySelectorAll('[data-override-path]')].find((input) => input.dataset.overridePath === path);
  const common = (path) => path === 'enabled' || path === 'resources.font_id' || /^modes\.[^.]+\.(enabled|capacity|pool_ids|designated_capacity|designated_unique|steal_enabled|gift_enabled)$/.test(path) || path.startsWith('reset.') || ['commands.cooldown_seconds', 'display.page_size', 'statistics.activity_window_days'].includes(path);
  let group = 'common', fonts = [], previewSequence = 0;
  function override(path, enabled = true) {
    const toggle = overrideFor(path);
    if (toggle && toggle.checked !== enabled) { toggle.checked = enabled; toggle.dispatchEvent(new Event('change')); }
  }
  function assign(path, value) {
    const input = inputFor(path);
    if (!input || globalOnly(path) && state.scopeId !== 'global') return;
    override(path);
    if (input.type === 'checkbox') input.checked = !!value;
    else input.value = value;
    if (input.type === 'hidden') {
      for (const check of input.closest('.config-field').querySelectorAll('.pool-picker input')) check.checked = input.value.split('\n').includes(check.value);
    }
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }
  const cards = () => [...fields.querySelectorAll('[data-select-config]')].map((check) => check.closest('.config-field'));
  const selected = () => cards().filter((card) => !card.hidden && !card.closest('section').hidden && card.querySelector('[data-select-config]').checked);
  function select(next) {
    group = next;
    const query = $('#config-search').value.trim().toLocaleLowerCase();
    const advanced = $('#config-advanced').checked;
    for (const section of fields.querySelectorAll('.config-group')) {
      for (const card of section.querySelectorAll(':scope > .config-grid > .config-field')) {
        const path = card.dataset.fieldPath;
        const matches = !query || `${path} ${card.textContent}`.toLocaleLowerCase().includes(query);
        const templateEditor = card.querySelector('.template-editor');
        if (query && matches && templateEditor) templateEditor.open = true;
        card.hidden = !matches || (!query && (next === 'common' ? !common(path) : section.dataset.group !== next)) || (!query && next !== 'common' && !advanced && !common(path) && section.dataset.group === 'maintenance');
      }
      section.hidden = ![...section.querySelectorAll(':scope > .config-grid > .config-field')].some((card) => !card.hidden);
    }
    $('#config-selected-count').textContent = `已选择 ${selected().length} 项 · 批量操作只修改草稿`;
  }
  function enhance() {
    const allInputs = [...fields.querySelectorAll('[data-config-path]')];
    for (const input of allInputs) {
      const path = input.dataset.configPath;
      if (path.endsWith('.gift_mode') && input.closest('.config-inline-gift')) continue;
      const card = input.closest('.config-field'); card.dataset.fieldPath = path;
      card.closest('.config-group').dataset.group = card.closest('.config-group').id.replace('config-group-', '');
      if (path.startsWith('reply_quote.')) {
        const message = inputFor(path.replace('reply_quote.', 'messages.'));
        if (message) { card.className = 'config-inline-quote'; message.closest('.config-field').append(card); }
        continue;
      }
      if (path === 'schema_version') { card.hidden = true; card.remove(); continue; }
      const selection = document.createElement('label'); selection.className = 'config-selection';
      const check = document.createElement('input'); check.type = 'checkbox'; check.dataset.selectConfig = path; check.setAttribute('aria-label', `选择${label(path)}`);
      check.disabled = globalOnly(path) && state.scopeId !== 'global';
      selection.append(check, document.createTextNode('选择')); card.prepend(selection);
      check.addEventListener('change', () => select(group));
      if (path.startsWith('messages.')) {
        const variables = state.templateFields[path]?.allowed || [];
        const toolbar = document.createElement('div'); toolbar.className = 'template-variables';
        for (const variable of variables) {
          const button = document.createElement('button'); button.type = 'button'; button.className = 'variable-chip'; button.textContent = `{${variable}}`;
          button.addEventListener('click', () => { override(path); input.focus(); input.setRangeText(button.textContent, input.selectionStart, input.selectionEnd, 'end'); input.dispatchEvent(new Event('input', { bubbles: true })); });
          toolbar.append(button);
        }
        card.append(toolbar);
      }
      if (path.endsWith('.gift_enabled')) {
        const modePath = path.replace('.gift_enabled', '.gift_mode');
        const mode = inputFor(modePath); const modeCard = mode.closest('.config-field');
        modeCard.hidden = state.scopeId === 'global'; modeCard.className = 'config-inline-gift';
        modeCard.querySelector('label.field').hidden = true; card.append(modeCard);
        if (state.scopeId !== 'global') {
          const modeOverride = modeCard.querySelector('.config-override');
          const enabledOverride = card.querySelector('.config-override');
          if (modeOverride?.lastChild?.nodeType === Node.TEXT_NODE) modeOverride.lastChild.textContent = '赠送方式覆盖（关闭则继承全局）';
          if (enabledOverride?.lastChild?.nodeType === Node.TEXT_NODE) enabledOverride.lastChild.textContent = '赠送开关覆盖（关闭则继承全局）';
        }
        const select = document.createElement('select'); select.setAttribute('aria-label', '赠送方式');
        for (const [value, text] of [['off', '关闭赠送'], ['direct', '直接赠送'], ['confirm', '接收者确认']]) select.add(new Option(text, value));
        const sync = () => { select.value = input.checked ? mode.value : 'off'; };
        sync(); input.hidden = true; card.querySelector('label.field').append(select);
        select.addEventListener('change', () => { override(path); override(modePath); input.checked = select.value !== 'off'; if (select.value !== 'off') mode.value = select.value; changed(); });
        input.addEventListener('change', sync); mode.addEventListener('change', sync);
        card.querySelector('[data-override-path]')?.addEventListener('change', sync);
        modeCard.querySelector('[data-override-path]')?.addEventListener('change', sync);
      }
      if (path.endsWith('.gift_mode')) {
        card.querySelector('.config-selection')?.remove();
      }
      if (path.endsWith('.pool_ids')) {
        const toolbar = document.createElement('div'); toolbar.className = 'actions';
        for (const [text, action] of [['全选', 'all'], ['清空', 'none'], ['反选', 'invert']]) {
          const button = document.createElement('button'); button.type = 'button'; button.className = 'button secondary'; button.textContent = text;
          button.addEventListener('click', () => { override(path); const ids = [...card.querySelectorAll('.pool-picker input')].filter((check) => action === 'all' || action === 'invert' && !check.checked).map((check) => check.value); assign(path, ids.join('\n')); }); toolbar.append(button);
        }
        card.append(toolbar);
      }
      if (path === 'resources.font_id') {
        const select = document.createElement('select'); select.dataset.configPath = path; select.disabled = input.disabled;
        for (const font of fonts) select.add(new Option(font.name, font.id));
        if (!fonts.some((font) => font.id === input.value)) select.add(new Option('字体暂不可用（渲染时回退内置）', input.value));
        select.value = input.value; input.replaceWith(select);
        const image = document.createElement('img'); image.className = 'font-preview'; image.alt = '所选字体的中文渲染预览'; image.hidden = true; card.append(image);
        const button = document.createElement('button'); button.type = 'button'; button.className = 'button secondary'; button.textContent = '预览字体';
        button.addEventListener('click', async () => {
          const sequence = ++previewSequence; button.disabled = true;
          try { const result = await apiGet('fonts/preview', { font_id: select.value }); if (sequence === previewSequence) { image.src = result.data.src; image.hidden = false; button.textContent = result.data.fallback ? '已回退内置字体，再次预览' : '再次预览字体'; } }
          catch (error) { button.textContent = error.message || '预览失败，点击重试'; }
          finally { button.disabled = false; }
        }); card.append(button);
      }
    }
    for (const input of fields.querySelectorAll('[data-config-path^="messages."]')) {
      const card = input.closest('.config-field');
      const details = document.createElement('details'); details.className = 'template-editor';
      const summary = document.createElement('summary');
      const title = document.createElement('strong'); title.textContent = label(input.dataset.configPath);
      const preview = document.createElement('span'); preview.className = 'muted'; preview.textContent = input.value.split('\n')[0].slice(0, 60);
      summary.append(title, preview); details.append(summary);
      for (const child of [...card.children]) if (!child.classList.contains('config-selection')) details.append(child);
      card.append(details);
      input.addEventListener('input', () => { preview.textContent = input.value.split('\n')[0].slice(0, 60); });
    }
    select(group);
  }
  $('#config-search').addEventListener('input', () => select(group));
  $('#config-advanced').addEventListener('change', () => select(group));
  $('#config-select-all').addEventListener('click', () => { for (const card of cards()) if (!card.hidden && !card.closest('section').hidden) { const check = card.querySelector('[data-select-config]'); if (!check.disabled) check.checked = true; } select(group); });
  $('#config-select-none').addEventListener('click', () => { for (const card of cards()) card.querySelector('[data-select-config]').checked = false; select(group); });
  for (const [id, value] of [['config-enable', true], ['config-disable', false]]) $("#" + id).addEventListener('click', () => {
    for (const card of selected()) for (const input of card.querySelectorAll('[data-config-path]')) if (input.type === 'checkbox') assign(input.dataset.configPath, value);
    changed();
  });
  $('#config-inherit').addEventListener('click', () => { for (const card of selected()) for (const toggle of card.querySelectorAll('[data-override-path]')) override(toggle.dataset.overridePath, false); changed(); });
  $('#config-copy-modes').addEventListener('click', () => {
    const modes = [...document.querySelectorAll('[data-copy-mode]:checked')].map((input) => input.dataset.copyMode);
    const copies = new Map();
    for (const card of selected()) for (const input of card.querySelectorAll('[data-config-path]')) {
      const match = /^modes\.[^.]+\.(.+)$/.exec(input.dataset.configPath); if (!match) continue;
      if (match[1] === 'pool_ids') continue;
      const value = input.type === 'checkbox' ? input.checked : input.value;
      if (copies.has(match[1]) && copies.get(match[1]) !== value) { $('#config-selected-count').textContent = '选中的同名规则值不同，请只选择一个来源玩法。'; return; }
      copies.set(match[1], value);
    }
    for (const mode of modes) for (const [key, value] of copies) assign(`modes.${mode}.${key}`, value);
    changed();
  });
  return { enhance, select, loadFonts: async () => { fonts = (await apiGet('fonts')).data; } };
}
