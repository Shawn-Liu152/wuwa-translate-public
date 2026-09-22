/* Pure display helpers shared by the workbench controller. */
(function exposeWorkbenchFormatters(root) {
  function escapeHtml(value = '') {
    return String(value).replace(/[&<>'"]/g, character => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    }[character]));
  }

  function formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value || '');
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(date);
  }

  function getVideoId(url = '') {
    try {
      const parsed = new URL(url);
      if (parsed.hostname === 'youtu.be') {
        return parsed.pathname.split('/').filter(Boolean)[0] || '';
      }
      if (parsed.hostname.endsWith('youtube.com')) {
        return parsed.searchParams.get('v')
          || parsed.pathname.split('/').filter(Boolean).pop()
          || '';
      }
    } catch (_) { /* 非 URL 任务 */ }
    return '';
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Number(seconds) || 0);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor(total % 3600 / 60);
    const secs = Math.floor(total % 60);
    return [hours, minutes, secs].filter((_, index) => index > 0 || hours > 0)
      .map(value => String(value).padStart(2, '0')).join(':') || '00:00';
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let amount = value / 1024;
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[index]}`;
  }

  window.PhoebeFormatters = Object.freeze({
    escapeHtml,
    formatDate,
    getVideoId,
    formatDuration,
    formatBytes,
  });
}(window));
