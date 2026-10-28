// 页内 dialog 可用于 Plugin Pages 沙箱；浏览器 confirm() 会被宿主拦截。
export function confirmAction(message) {
  const dialog = document.querySelector('#action-confirm-dialog');
  dialog.querySelector('#action-confirm-message').textContent = message;
  dialog.returnValue = 'cancel';
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
    dialog.showModal();
  });
}
