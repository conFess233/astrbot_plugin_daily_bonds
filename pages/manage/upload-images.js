// 只压缩上传副本；原始文件和用于角色匹配的文件名保持不变。
const MIB = 1024 * 1024;
const preferences = { target: 6, quality: 80 };

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

export async function prepareUploadImage(file, options = preferences) {
  const target = Number(options.target), quality = Number(options.quality);
  if (!Number.isFinite(target) || Math.floor(target * MIB) < 1 || target > 12) throw new Error('目标大小须大于 0 且不超过 12 MiB，至少为 1 字节。');
  if (!Number.isFinite(quality) || quality < 0 || quality > 100) throw new Error('图片质量须为 0～100%。');
  const targetBytes = Math.floor(target * MIB);
  if (file.size <= targetBytes) return file;
  if (!/\.(png|jpe?g|webp)$/i.test(file.name) && !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('仅支持 PNG / JPEG / WebP 图片压缩。');
  let bitmap;
  const canvas = document.createElement('canvas');
  try {
    bitmap = await createImageBitmap(file);
    let width = bitmap.width, height = bitmap.height;
    while (true) {
      canvas.width = width; canvas.height = height;
      const context = canvas.getContext('2d');
      if (!context) throw new Error('浏览器无法处理此图片。');
      context.drawImage(bitmap, 0, 0, width, height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/webp', quality / 100));
      if (!blob || blob.type !== 'image/webp') throw new Error('浏览器不支持 WebP 压缩或图片尺寸过大。');
      if (blob.size <= targetBytes) return new File([blob], file.name, { type: blob.type, lastModified: file.lastModified });
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
