// 图片上传的公共部分：本地压缩与格式转换、拖放/粘贴/点击选择、并发上传队列。
// 只处理上传副本；原始文件和用于角色匹配的文件名保持不变。
const MIB = 1024 * 1024;
const preferences = { target: 6, quality: 80 };
const ACCEPTED = /\.(png|jpe?g|webp)$/i;
const ACCEPTED_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
// 浏览器多半能解码但服务端不收的格式：上传前转成 WebP。
const CONVERTIBLE = /\.(gif|bmp|avif|tiff?|ico|heic|heif)$/i;

export function initUploadCompression() {
  for (const input of document.querySelectorAll('[data-upload-setting]')) {
    input.value = preferences[input.dataset.uploadSetting];
    input.addEventListener('input', () => {
      preferences[input.dataset.uploadSetting] = input.valueAsNumber;
      for (const other of document.querySelectorAll(`[data-upload-setting="${input.dataset.uploadSetting}"]`)) {
        if (other !== input) other.value = input.value;
      }
    });
  }
}

/** 是否像一张图片：按扩展名或 MIME 判断，用于从文件夹里筛掉说明文件等。 */
export function isImageFile(file) {
  return ACCEPTED.test(file.name) || CONVERTIBLE.test(file.name) || file.type?.startsWith('image/');
}

function accepted(file) {
  return ACCEPTED.test(file.name) && (!file.type || ACCEPTED_TYPES.includes(file.type));
}

export async function prepareUploadImage(file, options = preferences) {
  const target = Number(options.target), quality = Number(options.quality);
  if (!Number.isFinite(target) || Math.floor(target * MIB) < 1 || target > 12) throw new Error('目标大小须大于 0 且不超过 12 MiB，至少为 1 字节。');
  if (!Number.isFinite(quality) || quality < 0 || quality > 100) throw new Error('图片质量须为 0～100%。');
  const targetBytes = Math.floor(target * MIB);
  const convert = !accepted(file);
  if (!convert && file.size <= targetBytes) return file;
  let bitmap;
  const canvas = document.createElement('canvas');
  try {
    try {
      bitmap = await createImageBitmap(file);
    } catch {
      throw new Error(convert ? '浏览器无法读取这种格式（如 HEIC），请先转换为 PNG 或 JPEG。' : '图片无法解码，文件可能已损坏。');
    }
    let width = bitmap.width, height = bitmap.height;
    // 转换后的文件名改用 .webp，保留原名主体，角色匹配不受影响。
    const name = convert ? file.name.replace(/\.[^.]+$/, '') + '.webp' : file.name;
    while (true) {
      canvas.width = width; canvas.height = height;
      const context = canvas.getContext('2d');
      if (!context) throw new Error('浏览器无法处理此图片。');
      context.drawImage(bitmap, 0, 0, width, height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/webp', quality / 100));
      if (!blob || blob.type !== 'image/webp') throw new Error('浏览器不支持 WebP 压缩或图片尺寸过大。');
      if (blob.size <= targetBytes) return new File([blob], name, { type: blob.type, lastModified: file.lastModified });
      if (width === 1 && height === 1) throw new Error('无法压缩到目标大小，请提高目标值。');
      const scale = Math.min(0.9, Math.sqrt(targetBytes / blob.size) * 0.95);
      width = Math.max(1, Math.floor(width * scale));
      height = Math.max(1, Math.floor(height * scale));
    }
  } catch (failure) {
    throw new Error(failure.message || '图片无法解码或压缩，请更换文件。');
  } finally {
    bitmap?.close(); canvas.width = canvas.height = 1;
  }
}

/** 依次处理任务，最多同时运行 limit 个；返回每项的 {ok, value | error}。 */
export async function runLimited(items, limit, worker) {
  const results = new Array(items.length);
  let next = 0;
  async function lane() {
    while (next < items.length) {
      const index = next++;
      try { results[index] = { ok: true, value: await worker(items[index], index) }; }
      catch (error) { results[index] = { ok: false, error }; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, lane));
  return results;
}

async function readEntry(entry, parent = '') {
  const path = parent + entry.name;
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    return isImageFile(file) ? [{ file, path }] : [];
  }
  const reader = entry.createReader();
  const result = [];
  while (true) {
    const children = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!children.length) break;
    for (const child of children) result.push(...await readEntry(child, path + '/'));
  }
  return result;
}

/**
 * 让一个区域接受拖入、点击选择与粘贴。onFiles 收到 [{file, path}]，path 为相对文件夹路径（可能为空）。
 * pasteScope 为空时不处理粘贴；否则只在该元素可见且焦点不在文本输入框时接管剪贴板图片。
 */
export function bindDropZone(zone, { input, folderInput, onFiles, isBusy = () => false, pasteScope = null }) {
  const deliver = (entries) => { if (entries.length) onFiles(entries); };
  zone.addEventListener('keydown', (event) => {
    if (event.target === zone && ['Enter', ' '].includes(event.key)) { event.preventDefault(); input.click(); }
  });
  for (const name of ['dragenter', 'dragover']) {
    zone.addEventListener(name, (event) => { event.preventDefault(); if (!isBusy()) zone.classList.add('dragging'); });
  }
  zone.addEventListener('dragleave', (event) => { if (!zone.contains(event.relatedTarget)) zone.classList.remove('dragging'); });
  zone.addEventListener('drop', async (event) => {
    event.preventDefault();
    zone.classList.remove('dragging');
    if (isBusy()) return;
    const entries = [...event.dataTransfer.items].map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
    if (!entries.length) { deliver([...event.dataTransfer.files].filter(isImageFile).map((file) => ({ file, path: '' }))); return; }
    const files = [];
    for (const entry of entries) files.push(...await readEntry(entry));
    // 单独拖入的文件没有父文件夹，路径即文件名，统一置空。
    deliver(files.map(({ file, path }) => ({ file, path: path.includes('/') ? path : '' })));
  });
  const fromInput = (target) => () => {
    deliver([...target.files].filter(isImageFile).map((file) => ({ file, path: file.webkitRelativePath || '' })));
    target.value = '';
  };
  input.addEventListener('change', fromInput(input));
  folderInput?.addEventListener('change', fromInput(folderInput));
  if (pasteScope) {
    document.addEventListener('paste', (event) => {
      if (!pasteScope.offsetParent || isBusy()) return;
      const dialog = document.querySelector('dialog[open]');
      if (dialog && !dialog.contains(pasteScope)) return;
      const active = document.activeElement;
      if (active?.matches?.('input:not([type=file]):not([type=checkbox]), textarea, [contenteditable]')) return;
      const files = [...(event.clipboardData?.files || [])].filter(isImageFile);
      if (!files.length) return;
      event.preventDefault();
      deliver(files.map((file, index) => ({ file: file.name && file.name !== 'image.png' ? file : new File([file], `粘贴图片-${Date.now()}-${index + 1}.png`, { type: file.type }), path: '' })));
    });
  }
}
