// 四类配置视图共用原有字段、草稿与保存流程。
export const CONFIG_PAGES = { switches: '主要开关', parameters: '参数配置', keywords: '关键词', messages: '回复词' };
const REPLY_PREFIXES = ['messages.', 'reply_quote.', 'reply_enabled.'];
export function configPage(path, value) {
  if (REPLY_PREFIXES.some((prefix) => path.startsWith(prefix))) return 'messages';
  if (typeof value === 'boolean' || /^modes\.(wife|husband)\.pool_ids$/.test(path)) return 'switches';
  if (path.startsWith('commands.keywords.') || path === 'commands.extra_prefixes' || /^batch_import\..+_keywords$/.test(path)) return 'keywords';
  return 'parameters';
}
