/* ============================================================
   字幕控制台 — 前端交互
   SSE 实时日志 / 安全配置 / cancel / delete / form memory
   ============================================================ */

let selectedJob = null;
const selectedJobIds = new Set();
const NAVIGATION_VIEWS = new Set([
  'workbench', 'review', 'glossary', 'prompt', 'settings', 'guide', 'faq',
]);
let logAutoFollow = true;
const DETAIL_TAB_NAMES = ['overview', 'pipeline', 'log', 'files', 'risk'];
let activeDetailTab = 'overview';
let risksLoadedJobId = '';
let eventSource = null;
let globalJobEventSource = null;
let selectionVersion = 0;
let jobsRequestVersion = 0;
let risksRequestVersion = 0;
let glossaryRequestVersion = 0;
let toastTimer = null;
let riskItems = [];
let allRiskItems = [];
let riskPage = 1;
let RISK_PAGE_SIZE = 30;
const activeRiskKeys = { review: '', detail: '' };
let riskDirty = false;
let riskSaving = false;
const deliveryRequests = new Set();
let allJobs = [];
let jobSummaryFilter = '';
let completionTimer = null;
const celebratedJobs = new Set();

/* 交互保持窗口：交互期间只记录刷新意图，空闲时基于最新状态做 keyed reconcile。 */
let pendingJobsListRender = false;
let pendingJobsListScrollTop = 0;
let jobsListApplyTimer = 0;
let pointerIsDown = false;
const JOBS_LIST_APPLY_PROBE_MS = 300;
/* SSE 断线兜底轮询：连续失败达到阈值才降级，间隔按任务状态自适应 */
let jobsPollTimer = 0;
let jobsPollFailures = 0;
const JOBS_POLL_ACTIVE_MS = 2000;
const JOBS_POLL_IDLE_MS = 8000;
const JOBS_POLL_EMPTY_MS = 15000;
const JOBS_POLL_FAILURE_THRESHOLD = 3;
const ACTIVE_JOB_STATUSES = [
  'queued', 'running', 'paused', 'waiting_capcut', 'ready_for_translation',
];

const $ = (selector) => document.querySelector(selector);
const { escapeHtml, formatDate, getVideoId, formatDuration, formatBytes } = window.PhoebeFormatters;
const newTaskDialog = $('#new-task-dialog');
let newTaskOpener = null;
const {
  apiRequest: api,
  confirmAction,
  debounce,
  createScheduler,
  createTransport,
} = window.SubtitleWorkbenchCore;

/* 渲染调度器：同帧内的多次状态更新合并为一次渲染。
   SSE progress/status 高频事件不再排队重复 DOM 工作。 */
const render = createScheduler();
/* 传输层：按资源键去重在飞 GET；切换任务时可取消过期请求。 */
const transport = createTransport({ request: api });
let localSessionExpired = false;

function showSessionRecovery() {
  localSessionExpired = true;
  stopJobsPolling();
  globalJobEventSource?.close();
  eventSource?.close();
  $('#health').textContent = '会话已失效';
  const dialog = $('#session-recovery');
  if (!dialog.open) dialog.showModal();
}

window.addEventListener('subtitle-session-expired', showSessionRecovery);
$('#session-recovery').addEventListener('cancel', event => event.preventDefault());
$('#session-reconnect').addEventListener('click', async event => {
  const button = event.currentTarget;
  if (button.disabled) return;
  button.disabled = true;
  try {
    await window.SubtitleWorkbenchCore.reconnectLocalSession();
    // Reload the same URL; never replay the rejected mutation.
    window.location.reload();
  } catch (error) {
    $('#session-recovery-note').textContent = error.status === 401
      ? '连接仍未恢复。请关闭工作台服务后重新启动，并使用新打开的页面。'
      : '暂时无法连接。请使用启动工作台后新打开的页面。';
  } finally {
    button.disabled = false;
  }
});

function setNewTaskMode(mode = 'url') {
  const nextMode = mode === 'upload' ? 'upload' : 'url';
  document.querySelectorAll('input[name="new_task_mode"]').forEach(input => {
    input.checked = input.value === nextMode;
  });
  document.querySelectorAll('[data-new-task-panel]').forEach(panel => {
    panel.hidden = panel.dataset.newTaskPanel !== nextMode;
  });
}

function openNewTaskDialog(mode = 'url', opener = document.activeElement) {
  setNewTaskMode(mode);
  if (!newTaskDialog.open) {
    newTaskOpener = opener instanceof HTMLElement ? opener : null;
    newTaskDialog.showModal();
  }
  const selectedMode = newTaskDialog.querySelector(`input[name="new_task_mode"][value="${mode}"]`);
  selectedMode?.focus();
}

function closeNewTaskDialog() {
  if (newTaskDialog.open) newTaskDialog.close();
}

document.querySelectorAll('input[name="new_task_mode"]').forEach(input => {
  input.addEventListener('change', event => setNewTaskMode(event.currentTarget.value));
});
document.addEventListener('click', event => {
  const opener = event.target.closest('[data-open-new-task]');
  if (opener) openNewTaskDialog('url', opener);
});
newTaskDialog.querySelector('[data-close-new-task]').addEventListener('click', closeNewTaskDialog);
newTaskDialog.addEventListener('click', event => {
  if (event.target === newTaskDialog) closeNewTaskDialog();
});
newTaskDialog.addEventListener('close', () => {
  if (newTaskOpener?.isConnected) newTaskOpener.focus();
  newTaskOpener = null;
});

const STAGE_LABELS = {
  'Queued': '任务已排队',
  'Sources ready': '源语言证据已就绪',
  'Downloading YouTube assets': '正在获取 YouTube 素材',
  'Waiting for CapCut English SRT': '等待上传剪映主字幕',
  'Translation queued': '翻译任务已排队',
  'Preparing English sources': '正在整理源语言证据',
  'Completed': '翻译与风险分析已完成',
  'Failed': '处理失败',
  'Download failed': '素材获取失败',
  'API authentication failed': 'API Key 验证失败',
  'API request failed': '模型请求失败',
  'Cancelled by user': '任务已取消',
  'Force reset (was stuck)': '任务已强制停止',
  'Download retry queued': '素材重试已排队',
  'Ready for translation (retry)': '可继续翻译',
  'Ready for formal translation': '等待正式翻译',
  'Source strategy upgraded': '已启用补充源语言证据',
  'Paused by user': '任务已暂停',
  'Paused — API key required to resume': '已暂停，等待重新输入 API Key',
  'Interrupted by service restart': '服务重启已中断任务',
};

const ARTIFACT_LABELS = {
  whisper_en: '自适应 Whisper 字幕',
  'final.zh.risk.generated.json': '算法风险快照（只读）',
  'final.zh.round2.results.json': 'Round 2 自动结果',
  'final.zh.review-state.json': '人工审校状态',
  'final.zh.pipeline.metrics.json': '任务质量与成本指标',
  'final.round1.zh.srt': 'Round 1 中文译文（保留）',
  'final.round2.zh.srt': 'Round 2 精校译文（保留）',
  'final.round1.zh.srt.manifest.json': 'Round 1 断点记录',
  'final.round2.zh.srt.manifest.json': 'Round 2 断点记录',
  video: '原视频',
  thumbnail: '视频封面',
  metadata_json: 'YouTube 视频信息',
  youtube_en: 'YouTube 源语言字幕',
  capcut_en: '剪映主字幕',
  'capcut.en.srt': '剪映主字幕',
  'whisper.en.srt': 'Whisper 辅助字幕',
  'source.media': '源媒体',
  'final.zh.en.union.final.srt': '双源英文合并字幕',
  'final.zh.risk-queue.json': '风险审校队列',
  'final.zh.srt': '中文翻译字幕',
  'final.reviewed.zh.srt': '已审校中文字幕',
  'final.zh.burned.mp4': '带字幕视频（黑框白字2）',
  'final.zh.srt.manifest.json': '断点恢复记录',
};

function isDeliveryArtifact(name) {
  const extension = name.slice(name.lastIndexOf('.')).toLowerCase();
  return [
    'video',
    'thumbnail',
    'final.zh.srt',
    'final.reviewed.zh.srt',
  ].includes(name)
    || ['.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v'].includes(extension)
    || ['.jpg', '.jpeg', '.png', '.webp'].includes(extension);
}

const FLOW_STEPS = ['获取素材', '补充源语言证据', 'AI 翻译', '审校导出'];

const STATUS_MARKS = {
  queued: '…',
  running: '↻',
  paused: 'Ⅱ',
  waiting_capcut: '!',
  ready_for_translation: '!',
  completed: '✓',
  failed: '×',
  cancelled: '×',
};

/* ---- 表单记忆 ---- */

const MEMORY_KEYS = ["game", "reference_strategy", "video_quality", "youtube_auth"];

const DEFAULTS = {
  game: "wuwa",
  reference_strategy: "adaptive",
  video_quality: "1080",
  youtube_auth: "none",
};

const USER_SETTINGS_DEFAULTS = {
  proxy: "http://127.0.0.1:7890",
  base_url: "",
  model: "",
  round2_model: "",
  batch_size: 28,
  local_whisper: false,
  api_key_configured: false,
  api_key_persistence: "none",
  credential_warning: "",
};
let userSettings = { ...USER_SETTINGS_DEFAULTS };
let subtitleBurnCapability = {
  available: false,
  error_code: 'checking',
  message: '正在检查带字幕视频能力',
  style: 'black-outline-white-2-v1',
};

function applyCapabilityLimits(health) {
  const capabilities = health?.capabilities || {};
  const hasWhisper = Boolean(capabilities.local_whisper);
  const hasFfmpeg = Boolean(capabilities.ffmpeg);
  subtitleBurnCapability = health?.subtitle_burn || {
    available: false,
    error_code: 'burn_unavailable',
    message: '当前环境不能生成带字幕视频',
    style: 'black-outline-white-2-v1',
  };

  if (!hasWhisper) {
    DEFAULTS.reference_strategy = 'youtube';
    const memory = loadFormMemory();
    if (memory.reference_strategy !== 'youtube') {
      memory.reference_strategy = 'youtube';
      localStorage.setItem('subtitle_console', JSON.stringify(memory));
    }
    document.querySelectorAll('select[name="reference_strategy"]').forEach(select => {
      [...select.options].forEach(option => {
        option.disabled = option.value !== 'youtube';
      });
      select.value = 'youtube';
      select.title = '当前运行环境未安装本地 Whisper，只能使用 YouTube 字幕';
    });
  }

  const videoToggle = document.querySelector('#url-form input[name="download_video"]');
  if (videoToggle && !hasFfmpeg) {
    videoToggle.checked = false;
    videoToggle.disabled = true;
    videoToggle.closest('label').title = '安装 FFmpeg 并重启工作台后可下载视频';
  }

  if (selectedJob) renderDetail();
}

const APP_SETTINGS_KEY = 'subtitle_app_settings';
const APP_SETTING_DEFAULTS = {
  risk_page_size: '30',
  risk_default_filter: 'unresolved',
  log_visibility: 'auto',
  pet_id: 'feibi',
  pet_enabled: true,
  pet_roam: false,
  motion_reduced: false,
};
let appSettings = { ...APP_SETTING_DEFAULTS };

const RISK_REASON_LABELS = {
  term_mismatch: '人名或术语未使用官方译名',
  suspected_name: '疑似人名未被正确识别',
  name_mismatch: '人名未使用官方译名',
  person_name_present: '包含人物名，请确认识别与译名',
  missing_translation: '疑似漏译',
  empty_translation: '译文为空',
  numeric_mismatch: '数字与原文不一致',
  number_mismatch: '数字与原文不一致',
  negation_mismatch: '否定含义可能错误',
  negation_missing: '否定含义可能遗漏',
  condition_marker_missing: '条件含义可能遗漏',
  semantic_marker_missing: '关键语义可能遗漏',
  text_conflict: '两路源语言字幕内容不一致',
  credit_line: '疑似字幕署名或来源标记',
  noise_command: '疑似非语音噪声指令',
  repeated_clause: '疑似 ASR 整句复读',
  low_info_density: '长时段语音信息密度过低',
  reading_speed: '字幕阅读速度过快',
  source_conflict: '英文来源存在冲突',
  unusually_short: '译文可能过短',
  unusually_long: '译文可能过长',
  local_audio_conflict: '局部音频复核不一致',
  english_residue: '译文中残留较多英文',
};

const STATUS_LABELS = {
  queued: '排队中', running: '处理中', paused: '已暂停', waiting_capcut: '待补充剪映字幕',
  ready_for_translation: '待开始翻译', completed: '已完成', failed: '处理失败',
  cancelled: '已取消',
};

function showToast(message, kind = 'success') {
  const toast = $('#toast');
  if (!toast) return;
  const normalizedKind = ['error', 'warning'].includes(kind) ? kind : 'success';
  const presentation = {
    success: { mark: '✓', title: '操作已完成' },
    warning: { mark: '!', title: '还需要确认' },
    error: { mark: '×', title: '操作未完成' },
  }[normalizedKind];
  clearTimeout(toastTimer);
  const mark = document.createElement('span');
  mark.className = 'toast-mark';
  mark.setAttribute('aria-hidden', 'true');
  mark.textContent = presentation.mark;
  const copy = document.createElement('span');
  copy.className = 'toast-copy';
  const title = document.createElement('b');
  title.textContent = presentation.title;
  const detail = document.createElement('span');
  detail.textContent = String(message || '');
  copy.append(title, detail);
  toast.replaceChildren(mark, copy);
  toast.className = `toast toast-${normalizedKind}`;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3200);
}

function setBusy(button, busy, label = '处理中…') {
  if (!button) return;
  if (busy) {
    button.dataset.originalLabel = button.querySelector('.button-label')?.textContent || button.textContent;
    const labelNode = button.querySelector('.button-label');
    if (labelNode) labelNode.textContent = label;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  } else {
    const labelNode = button.querySelector('.button-label');
    if (labelNode && button.dataset.originalLabel) labelNode.textContent = button.dataset.originalLabel;
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

function jobTitle(job) {
  if (job.custom_name) return job.custom_name;
  if (job.metadata?.title) return job.metadata.title;
  const videoId = getVideoId(job.url);
  if (videoId) return `YouTube · ${videoId}`;
  if (job.inputs?.video || job.artifacts?.video) return `本地媒体 · ${job.id.slice(0, 6)}`;
  return `SRT 翻译 · ${job.id.slice(0, 6)}`;
}

function stageLabel(job) {
  const stage = String(job.stage || '');
  if (STAGE_LABELS[stage]) return STAGE_LABELS[stage];
  const translating = stage.match(/^Translating batch (\d+)\/(\d+)$/);
  if (translating) return `正在翻译第 ${translating[1]} / ${translating[2]} 批`;
  const capcut = stage.match(/^CapCut uploaded \((\d+) subtitles\)$/);
  if (capcut) return `剪映字幕已上传（${capcut[1]} 条）`;
  return STATUS_LABELS[job.status] || '等待状态更新';
}

function workflowPosition(job) {
  if (job.status === 'paused') {
    return workflowPosition({ ...job, status: job.paused_from || 'ready_for_translation' });
  }
  if (job.status === 'completed') return 4;
  if (job.status === 'ready_for_translation') return 3;
  if (job.status === 'waiting_capcut') return 2;
  if (['running', 'queued'].includes(job.status)) {
    return job.inputs?.capcut_en ? 3 : 1;
  }
  if (job.status === 'failed') {
    return job.inputs?.capcut_en ? 3 : 1;
  }
  return job.inputs?.capcut_en ? 3 : 1;
}

/* ---- E-1：错误码 → 建议动作 / FAQ 锚点 ----
   原因文案取后端 job.error（pipeline/safe_errors.py 已脱敏并本地化），
   这里只补后端不提供的"下一步该做什么"，避免同一句话两处维护。
   键集合必须与 safe_errors._PUBLIC_MESSAGES 保持一致。 */
const ERROR_GUIDANCE = {
  authentication_error: {
    action: '去设置检查 API Key',
    actionView: 'settings',
    faqCode: 'authentication_error',
  },
  quota_error: {
    action: '账户/订阅额度已用尽，请到服务商后台充值或确认重置时间，再点“继续失败批次”',
    faqCode: 'quota_error',
  },
  configuration_error: {
    action: '去设置检查接口地址和模型名称',
    actionView: 'settings',
    faqCode: 'configuration_error',
  },
  provider_unavailable: {
    action: '稍后重试，或换一个模型接口',
    faqCode: 'provider_unavailable',
  },
  api_error: {
    action: '稍后重试本批；反复失败就换一个模型接口',
    faqCode: 'provider_unavailable',
  },
  invalid_provider_response: {
    action: '重试本批；连续出现就换模型或调小批次',
    faqCode: 'provider_unavailable',
  },
  internal_error: {
    action: '重试一次；仍然失败就看 FAQ 的处理办法',
    faqCode: 'provider_unavailable',
  },
  translation_batch_error: {
    action: '点“继续失败批次”，不用从头重跑',
    faqCode: 'translation_batch',
  },
  round2_invalid_response: {
    action: '点“继续失败批次”，Round 2 会重做这批',
    faqCode: 'translation_batch',
  },
  batch_processing_error: {
    action: '点“继续失败批次”，不用从头重跑',
    faqCode: 'translation_batch',
  },
  pipeline_error: {
    action: '点“继续失败批次”，不用从头重跑',
    faqCode: 'translation_batch',
  },
  download_error: {
    action: '点“重试下载”，并确认链接是否需要登录',
    faqCode: 'download_error',
  },
  youtube_auth_required: {
    action: '点“更换 Cookie 并重试”',
    faqCode: 'youtube_auth_required',
  },
  source_unreliable: {
    action: '更换素材来源，或重新上传一份字幕',
    faqCode: 'source_unreliable',
  },
  operation_blocked: {
    action: '刷新任务列表后再操作',
    faqCode: 'job_state',
  },
  metadata_update_error: {
    action: '刷新页面后重试',
    faqCode: 'job_state',
  },
  cancelled: {
    action: '任务已停止，产物都已保留',
    faqCode: 'job_state',
  },
  interrupted: {
    action: '服务重启过；确认配置后继续任务',
    faqCode: 'job_state',
  },
};

function errorGuidance(job) {
  if (!job || job.status !== 'failed') return null;
  const code = String(job.error_code || 'internal_error');
  const guidance = ERROR_GUIDANCE[code] || ERROR_GUIDANCE.internal_error;
  const upstreamStatus = Number(job.upstream_status);
  return {
    code: ERROR_GUIDANCE[code] ? code : 'internal_error',
    reason: String(job.error || '').split('\n')[0].trim(),
    action: guidance.action,
    actionView: guidance.actionView || '',
    faqCode: guidance.faqCode,
    upstreamStatus: Number.isInteger(upstreamStatus) && upstreamStatus
      ? upstreamStatus
      : null,
  };
}

function renderErrorBanner(job) {
  const guidance = errorGuidance(job);
  if (!guidance) return '';
  const status = guidance.upstreamStatus
    ? `<span class="job-error-status">上游 HTTP ${guidance.upstreamStatus}</span>`
    : '';
  const action = guidance.actionView
    ? `<button type="button" class="job-error-action" data-error-view="${escapeHtml(guidance.actionView)}">${escapeHtml(guidance.action)}</button>`
    : `<span class="job-error-action-text">建议：${escapeHtml(guidance.action)}</span>`;
  return `<div class="job-error-banner">
    <code class="job-error-code">${escapeHtml(guidance.code)}</code>${status}
    <span class="job-error-reason">${escapeHtml(guidance.reason)}</span>
    ${action}
    <button type="button" class="job-error-faq" data-faq-code="${escapeHtml(guidance.faqCode)}">查看处理办法</button>
  </div>`;
}

function taskPresentation(job) {
  const stage = String(job.stage || '');
  const recentLogs = (job.logs || []).slice(-6)
    .map(log => String(log.message || '')).join('\n');
  if (job.status === 'completed') {
    const count = Number(job.metrics?.quality?.union_subtitles || 0);
    const risks = unresolvedReviewRequiredCount(job);
    const untranslated = Number(
      job.metrics?.quality?.untranslated_source_cues || 0,
    );
    const untranslatedNote = untranslated
      ? `；成片含 ${metricNumber(untranslated)} 条未翻译英文句，建议进审校确认`
      : '';
    return {
      icon: risks ? '!' : '✓',
      title: risks ? '自动字幕已生成，等待审校' : '字幕可以交付了',
      message: `${count ? `${metricNumber(count)} 条字幕` : '全部字幕'}处理完成${risks ? `，还有 ${metricNumber(risks)} 条必须确认` : '，没有必须处理的风险'}${untranslatedNote}`,
      bubble: risks ? '还有几处需要你确认' : '翻完啦，可以交付！',
    };
  }
  if (job.status === 'failed') {
    return {
      icon: '!',
      title: '这里需要你看一下',
      message: String(job.error || '任务发生异常，请查看下方处理建议').split('\n')[0],
      bubble: '这里卡住了，我标出来啦',
    };
  }
  if (job.status === 'paused') {
    return { icon: 'Ⅱ', title: '任务已经暂停', message: '所有阶段产物都已保留，可以随时继续', bubble: '先休息一下也没关系' };
  }
  if (job.status === 'waiting_capcut') {
    return { icon: '＋', title: '旧任务等待选择', message: '可直接使用自动字幕继续，也可补充剪映字幕', bubble: '素材已经准备好啦' };
  }
  if (job.status === 'ready_for_translation') {
    return { icon: '→', title: '素材已经就绪', message: '确认当前模型即可开始翻译', bubble: '准备好了，随时开工！' };
  }
  if (job.status === 'queued') {
    return { icon: '…', title: '任务正在排队', message: '菲比会在轮到它时自动继续，不需要一直盯着页面', bubble: '排到后我会自己开工' };
  }
  const activity = `${stage}\n${recentLogs}`;
  if (/Downloading YouTube|下载视频|下载.*字幕/i.test(activity)) {
    return { icon: '↓', title: '正在获取视频素材', message: '视频、封面和 YouTube 源语言字幕会分别保存', bubble: '素材正在捞取中…' };
  }
  if (/Round 2|风险精校|风险复核/i.test(activity)) {
    return { icon: '◇', title: '正在复核高风险字幕', message: '只检查可能漏译、错词或术语异常的条目', bubble: '再仔细检查一遍' };
  }
  if (/Translating batch|AI 翻译|Round 1/i.test(activity)) {
    return { icon: '译', title: stageLabel(job), message: 'Round 1 正在按术语表逐批翻译', bubble: '正在逐句翻译！' };
  }
  if (/Whisper/i.test(activity)) {
    return { icon: '⌁', title: '正在听取视频语音', message: 'Whisper 正在生成或补充源语言证据', bubble: '嘘，我在认真听！' };
  }
  if (job.status === 'running') {
    return { icon: '↻', title: stageLabel(job), message: '任务正在自动运行，阶段产物会持续保存', bubble: '我盯着进度呢！' };
  }
  return { icon: '○', title: stageLabel(job), message: '等待下一步操作', bubble: '今天要翻什么？' };
}

function unresolvedReviewRequiredCount(job) {
  const current = Number(job?.review_pending_count ?? job?._unresolvedReviewRequired);
  if (Number.isInteger(current) && current >= 0) return current;
  return Math.max(0, Number(job?.metrics?.quality?.review_required_count || 0));
}

function completedToday(job) {
  const value = job.completed_at || job.updated_at;
  if (!value || job.status !== 'completed') return false;
  const completed = new Date(value);
  const now = new Date();
  return !Number.isNaN(completed.getTime())
    && completed.getFullYear() === now.getFullYear()
    && completed.getMonth() === now.getMonth()
    && completed.getDate() === now.getDate();
}

function jobSummaryCounts(jobs) {
  const visible = jobs.filter(job => !job.archived);
  return {
    active: visible.filter(job => ACTIVE_JOB_STATUSES.includes(job.status)).length,
    review: visible.filter(job => job.status === 'completed' && unresolvedReviewRequiredCount(job) > 0).length,
    failed: visible.filter(job => job.status === 'failed').length,
    today: visible.filter(completedToday).length,
  };
}

function renderJobSummary(jobs) {
  const root = $('#job-status-summary');
  if (!root) return;
  const counts = jobSummaryCounts(jobs);
  root.querySelectorAll('[data-job-summary-filter]').forEach(button => {
    const filter = button.dataset.jobSummaryFilter;
    const count = button.querySelector('[data-job-summary-count]');
    if (count) count.textContent = String(counts[filter] || 0);
    const active = jobSummaryFilter === filter;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function jobMatchesSummaryFilter(job, summaryFilter) {
  if (!summaryFilter) return true;
  if (job.archived) return false;
  if (summaryFilter === 'active') return ACTIVE_JOB_STATUSES.includes(job.status);
  if (summaryFilter === 'review') {
    return job.status === 'completed' && unresolvedReviewRequiredCount(job) > 0;
  }
  if (summaryFilter === 'failed') return job.status === 'failed';
  if (summaryFilter === 'today') return completedToday(job);
  return true;
}

function sourceSummary(job) {
  const labels = { capcut: '剪映', youtube: 'YouTube', whisper: 'Whisper' };
  const sources = job.source_qualities || job.metrics?.sources || {};
  const names = Object.entries(sources)
    .filter(([, source]) => source?.usable !== false)
    .map(([name]) => labels[name] || name);
  if (names.length) return names.join(' + ');
  const selected = labels[job.reference_source] || '';
  return selected || (job.url ? '自动英文源' : '上传 SRT');
}

function updatePhoebeAssistant(job) {
  const petState = {
    queued: 'waiting',
    running: 'running',
    paused: 'waiting',
    waiting_capcut: 'waiting',
    ready_for_translation: 'review',
    failed: 'failed',
    completed: 'waving',
    cancelled: 'idle',
  }[job?.status] || 'idle';
  window.dispatchEvent(new CustomEvent('phoebe-pet', {
    detail: { state: petState, transient: job?.status === 'completed' },
  }));

  const bubble = document.querySelector('.phoebe-bubble');
  if (!bubble) return;
  const presentation = job ? taskPresentation(job) : { bubble: '今天要翻什么？' };
  bubble.textContent = presentation.bubble;
  document.body.dataset.phoebeMood = job?.status || 'idle';
}

function renderProgressView(job) {
  const progress = job.progress || { completed: 0, total: 0 };
  if (!progress.total) return '';
  const progressPercent = Math.round(progress.completed / progress.total * 100);
  const etaSeconds = progress.completed > 0 && progress.total > progress.completed
    ? Math.round((job.elapsed_seconds || 0) / progress.completed * (progress.total - progress.completed))
    : 0;
  return `<div class="job-progress" aria-label="翻译进度 ${progress.completed}/${progress.total}">
    <div><span>翻译批次</span><b>${progress.completed} / ${progress.total} · ${progressPercent}%${etaSeconds ? ` · 预计剩余 ${formatDuration(etaSeconds)}` : ''}</b></div>
    <progress max="${progress.total}" value="${progress.completed}">${progressPercent}%</progress>
  </div>`;
}

function renderWorkflowView(job) {
  const activeStep = workflowPosition(job);
  const completedDryRun = job.status === 'completed' && Boolean(job.options?.dry_run);
  return `<div class="workflow" aria-label="任务流程进度">${FLOW_STEPS.map((label, index) => {
    const step = index + 1;
    const state = step < activeStep || (job.status === 'completed' && !completedDryRun) ? 'done' : (step === activeStep ? 'current' : '');
    return `<div class="workflow-step ${state}"><span>0${step}</span><b>${label}</b></div>`;
  }).join('')}</div>`;
}

function renderLiveStatus(job, progressView) {
  const presentation = taskPresentation(job);
  const source = sourceSummary(job);
  return `<section class="live-status live-status-${escapeHtml(job.status)}" aria-labelledby="live-status-title">
    <span class="live-status-icon" id="live-status-icon" aria-hidden="true">${escapeHtml(presentation.icon)}</span>
    <div class="live-status-copy">
      <p class="eyebrow">PHOEBE LIVE</p>
      <h3 id="live-status-title">${escapeHtml(presentation.title)}</h3>
      <p id="live-status-message">${escapeHtml(presentation.message)}</p>
    </div>
    <dl class="live-status-facts">
      <div><dt>源语言证据</dt><dd>${escapeHtml(source)}</dd></div>
      <div><dt>当前阶段</dt><dd id="live-status-stage">${escapeHtml(stageLabel(job))}</dd></div>
    </dl>
    <div class="live-status-progress-slot" aria-live="polite">${progressView}</div>
  </section>`;
}

function updateProgressView(payload) {
  if (!selectedJob) return;
  selectedJob.progress = payload.progress || { completed: 0, total: 0 };
  selectedJob.stage = payload.stage || selectedJob.stage;
  if (Number.isFinite(Number(payload.elapsed_seconds))) {
    selectedJob.elapsed_seconds = Number(payload.elapsed_seconds);
  }

  const presentation = taskPresentation(selectedJob);
  const stage = stageLabel(selectedJob);
  const activeStep = workflowPosition(selectedJob);
  const activeFlowLabel = FLOW_STEPS[Math.max(0, Math.min(FLOW_STEPS.length - 1, activeStep - 1))];
  const setText = (selector, value) => {
    const element = document.querySelector(selector);
    if (element) element.textContent = value;
  };
  setText('#detail [data-overview-stage]', stage);
  setText('#detail [data-overview-phoebe-icon]', presentation.icon);
  setText('#detail [data-overview-phoebe-title]', presentation.title);
  setText('#detail [data-overview-phoebe-message]', presentation.message);
  setText('#detail [data-pipeline-stage]', selectedJob.stage || stage);
  setText('#detail [data-pipeline-stage-label]', stage);
  setText('#detail [data-pipeline-flow-label]', activeFlowLabel);
  setText('#detail [data-pipeline-flow-position]', `${activeStep} / ${FLOW_STEPS.length}`);
  setText('#live-status-icon', presentation.icon);
  setText('#live-status-title', presentation.title);
  setText('#live-status-message', presentation.message);
  setText('#live-status-stage', stage);
  const progressSlot = document.querySelector('#detail .live-status-progress-slot');
  if (progressSlot) progressSlot.innerHTML = renderProgressView(selectedJob);
  const workflow = document.querySelector('#detail-panel-overview .workflow');
  if (workflow) workflow.outerHTML = renderWorkflowView(selectedJob);
  updateSelectedJobCardProgress(selectedJob);
  updatePhoebeAssistant(selectedJob);
}

function updateSelectedJobCardProgress(job) {
  const card = document.querySelector(`#jobs-list .job[data-id="${CSS.escape(job?.id || '')}"]`);
  if (!card) return;
  const stageNode = card.querySelector('.job-stage-line > span');
  if (stageNode) stageNode.textContent = stageLabel(job);
  const progress = job.progress || { completed: 0, total: 0 };
  const progressPercent = progress.total
    ? Math.round(progress.completed / progress.total * 100)
    : 0;
  const progressText = card.querySelector('.job-stage-line > b');
  if (progressText) progressText.textContent = progress.total ? `${progressPercent}%` : '';
  const bar = card.querySelector('.job-mini-progress > span');
  if (bar) bar.style.width = `${progressPercent}%`;
}

function celebrateCompletion(job) {
  if (!job?.id || celebratedJobs.has(job.id)) return;
  celebratedJobs.add(job.id);
  clearTimeout(completionTimer);
  document.body.classList.add('phoebe-celebrate');
  updatePhoebeAssistant(job);
  const count = Number(job.metrics?.quality?.union_subtitles || 0);
  showToast(count ? `菲比完成了 ${metricNumber(count)} 条字幕` : '菲比完成了这次翻译');
  completionTimer = setTimeout(() => document.body.classList.remove('phoebe-celebrate'), 1500);
}

function syncUrl({
  view,
  jobId,
  jobQuery,
  jobStatus,
  riskFilter,
  page,
  faqQuery,
  glossaryQuery,
} = {}) {
  const url = new URL(window.location.href);
  const setParam = (name, value, defaultValue = '') => {
    if (value === undefined) return;
    const normalized = String(value ?? '').trim();
    if (!normalized || normalized === defaultValue) url.searchParams.delete(name);
    else url.searchParams.set(name, normalized);
  };
  setParam('view', view, 'workbench');
  setParam('job', jobId);
  setParam('jobs_q', jobQuery);
  setParam('jobs_status', jobStatus);
  setParam('risk', riskFilter, 'unresolved');
  setParam('page', page, '1');
  setParam('faq_q', faqQuery);
  setParam('glossary_q', glossaryQuery);
  /* APP-AUTH-001：保留 location.hash。启动阶段 initialize() 会先走到
     filterFaqItems→syncUrl，若在此处丢弃 hash，workbench-core 的
     一次性 bootstrap 令牌（#bootstrap=…）会在会话交换前被销毁，
     首次启动必然 401。令牌由 takeBootstrapFromFragment 读取后自行移除。 */
  history.replaceState(null, '', `${url.pathname}${url.search}${window.location.hash}`);
}

function saveFormMemory(form) {
  const data = { ...loadFormMemory() };
  MEMORY_KEYS.forEach(key => {
    const el = form.querySelector(`[name="${key}"]:checked`) || form.querySelector(`[name="${key}"]`);
    if (el) data[key] = el.value;
  });
  localStorage.setItem("subtitle_console", JSON.stringify(data));
}

function loadFormMemory() {
  try {
    const parsed = JSON.parse(localStorage.getItem("subtitle_console") || "{}");
    return Object.fromEntries(
      MEMORY_KEYS.filter(key => Object.prototype.hasOwnProperty.call(parsed, key))
        .map(key => [key, parsed[key]]),
    );
  } catch (_) { return {}; }
}

function applyMemory() {
  const mem = loadFormMemory();
  document.querySelectorAll("form").forEach(form => {
    MEMORY_KEYS.forEach(key => {
      const elements = [...form.querySelectorAll(`[name="${key}"]`)];
      const el = elements[0];
      if (!el) return;
      const value = mem[key] !== undefined ? mem[key] : (DEFAULTS[key] || "");
      if (el.type === "radio") elements.forEach(item => { item.checked = item.value === value; });
      else if (value) el.value = value;
    });
  });
}

function loadAppSettings() {
  try {
    return {
      ...APP_SETTING_DEFAULTS,
      ...JSON.parse(localStorage.getItem(APP_SETTINGS_KEY) || '{}'),
    };
  } catch (_) {
    return { ...APP_SETTING_DEFAULTS };
  }
}

function readPetSettings() {
  try {
    return JSON.parse(localStorage.getItem('phoebe-web-pet-v2') || '{}');
  } catch (_) {
    return {};
  }
}

function setFormValue(form, name, value) {
  const elements = [...form.querySelectorAll(`[name="${name}"]`)];
  if (!elements.length) return;
  if (elements[0].type === 'radio') {
    elements.forEach(element => { element.checked = element.value === String(value); });
  } else if (elements[0].type === 'checkbox') {
    elements[0].checked = Boolean(value);
  } else {
    elements[0].value = value ?? '';
  }
}

function setCredentialStatus() {
  const configured = Boolean(userSettings.api_key_configured);
  const persistence = userSettings.api_key_persistence;
  const text = configured
    ? (persistence === "session" ? "API Key 仅本次会话有效" : "API Key 已安全保存")
    : "尚未保存 API Key";
  ["#url-api-key-status", "#settings-api-key-status"].forEach(selector => {
    const status = $(selector);
    if (status) status.textContent = userSettings.credential_warning || text;
  });
}

function applyUserSettingsToForms() {
  [$("#url-form"), $("#app-settings-form")].filter(Boolean).forEach(form => {
    ["proxy", "base_url", "model", "round2_model", "batch_size", "local_whisper"]
      .forEach(key => setFormValue(form, key, userSettings[key]));
    const keyInput = form.querySelector('[name="api_key"]');
    if (keyInput) keyInput.value = "";
  });
  setCredentialStatus();
}

async function loadUserSettings() {
  const loaded = await api("/api/user-settings");
  userSettings = { ...USER_SETTINGS_DEFAULTS, ...loaded };
  applyUserSettingsToForms();
  return userSettings;
}

function collectUserSettings(form) {
  const payload = {};
  ["proxy", "base_url", "model", "round2_model"].forEach(key => {
    const input = form.querySelector(`[name="${key}"]`);
    if (input) payload[key] = input.value.trim();
  });
  const batch = form.querySelector('[name="batch_size"]');
  if (batch) payload.batch_size = Number(batch.value || 28);
  const localWhisper = form.querySelector('[name="local_whisper"]');
  if (localWhisper) payload.local_whisper = Boolean(localWhisper.checked);
  const keyInput = form.querySelector('[name="api_key"]');
  if (keyInput?.value.trim()) payload.api_key = keyInput.value.trim();
  return payload;
}

async function persistUserSettingsForm(form) {
  const saved = await api("/api/user-settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectUserSettings(form)),
  });
  userSettings = { ...USER_SETTINGS_DEFAULTS, ...saved };
  applyUserSettingsToForms();
  return userSettings;
}

async function forgetSavedApiKey() {
  try {
    const saved = await api("/api/user-settings/api-key", { method: "DELETE" });
    userSettings = { ...USER_SETTINGS_DEFAULTS, ...saved };
    applyUserSettingsToForms();
    showToast("已忘记保存的 API Key");
  } catch (error) {
    showToast(error.message, "error");
  }
}

function applyAppSettings(settings, { syncPet = false } = {}) {
  appSettings = { ...APP_SETTING_DEFAULTS, ...settings };
  const pageSize = Number(appSettings.risk_page_size);
  RISK_PAGE_SIZE = [10, 20, 30, 50].includes(pageSize) ? pageSize : 30;
  document.body.classList.toggle('motion-lite', Boolean(appSettings.motion_reduced));

  const riskFilter = $('#risk-filter');
  if (riskFilter && [...riskFilter.options].some(option => option.value === appSettings.risk_default_filter)) {
    riskFilter.value = appSettings.risk_default_filter;
  }

  if (syncPet) {
    window.dispatchEvent(new CustomEvent('phoebe-pet-settings', {
      detail: {
        enabled: Boolean(appSettings.pet_enabled),
        petId: appSettings.pet_id,
        roam: Boolean(appSettings.pet_roam),
        motionReduced: Boolean(appSettings.motion_reduced),
      },
    }));
  }
}

function populateSettingsForm() {
  const form = $("#app-settings-form");
  if (!form) return;
  applyUserSettingsToForms();

  const saved = loadAppSettings();
  const petSettings = readPetSettings();
  setFormValue(form, "risk_page_size", saved.risk_page_size);
  setFormValue(form, "risk_default_filter", saved.risk_default_filter);
  setFormValue(form, "log_visibility", saved.log_visibility);
  setFormValue(form, "pet_id", petSettings.petId || saved.pet_id);
  setFormValue(form, "pet_enabled", petSettings.hidden === undefined ? saved.pet_enabled : !petSettings.hidden);
  setFormValue(form, "pet_roam", petSettings.roam === undefined ? saved.pet_roam : petSettings.roam);
  setFormValue(form, "motion_reduced", saved.motion_reduced);
}

async function saveAppSettings(event) {
  event?.preventDefault();
  const form = $("#app-settings-form");
  if (!form || !form.reportValidity()) return;
  try {
    await persistUserSettingsForm(form);
  } catch (error) {
    $("#settings-note").textContent = error.message;
    showToast(error.message, "error");
    return;
  }
  const next = {
    risk_page_size: form.elements.risk_page_size.value,
    risk_default_filter: form.elements.risk_default_filter.value,
    log_visibility: form.elements.log_visibility.value,
    pet_id: form.elements.pet_id.value,
    pet_enabled: form.elements.pet_enabled.checked,
    pet_roam: form.elements.pet_roam.checked,
    motion_reduced: form.elements.motion_reduced.checked,
  };
  localStorage.setItem(APP_SETTINGS_KEY, JSON.stringify(next));
  applyAppSettings(next, { syncPet: true });
  riskPage = 1;
  if ($("#review")?.classList.contains("active")) loadRisks();
  if (selectedJob) renderDetail();
  $("#settings-note").textContent = userSettings.credential_warning || "设置已保存。";
  showToast("工作台设置已保存");
}

async function resetAppSettings() {
  try {
    const saved = await api("/api/user-settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(USER_SETTINGS_DEFAULTS),
    });
    userSettings = { ...USER_SETTINGS_DEFAULTS, ...saved };
  } catch (error) {
    showToast(error.message, "error");
    return;
  }
  localStorage.setItem("subtitle_console", "{}");
  applyMemory();
  localStorage.setItem(APP_SETTINGS_KEY, JSON.stringify(APP_SETTING_DEFAULTS));
  applyAppSettings(APP_SETTING_DEFAULTS, { syncPet: true });
  populateSettingsForm();
  $("#settings-note").textContent = "已恢复首次使用设置；已保存的 API Key 保持不变。";
  showToast("已恢复默认设置");
}

function translationNeedsRepair(job) {
  const options = job.options || {};
  return job.error_code === "authentication_error"
    || !(options.model || userSettings.model)
    || !(options.base_url || userSettings.base_url)
    || !(options.api_key_configured || userSettings.api_key_configured);
}

function renderTranslationSettings(job) {
  const options = job.options || {};
  const model = options.model || userSettings.model || "未配置";
  const round2Model = options.round2_model || userSettings.round2_model || "";
  const batchSize = options.batch_size || userSettings.batch_size || 28;
  const hasVideo = Boolean(job.inputs?.video || job.artifacts?.video);
  const burnRequested = Boolean(options.burn_after_translation);
  const burnAvailable = Boolean(subtitleBurnCapability.available);
  const burnDisabled = !hasVideo || !burnAvailable;
  const burnTitle = !hasVideo
    ? '当前任务没有视频，无法生成带字幕视频'
    : burnAvailable
      ? '翻译和必审风险处理完成后自动生成'
      : (subtitleBurnCapability.message || '当前环境不能生成带字幕视频');
  const repair = translationNeedsRepair(job) ? renderTranslationConfigRepair(job) : "";
  return `<section class="translation-settings">
    <div class="translation-settings-head">
      <div><p class="eyebrow">READY TO TRANSLATE</p><h3>开始翻译</h3></div>
      <button type="button" class="edit-translation-config">修改 API 配置</button>
    </div>
    <p class="translation-model-summary"><span>当前模型</span><b>${escapeHtml(model)}</b></p>
    <label class="setting-toggle burn-after-translation" title="${escapeHtml(burnTitle)}">
      <span><b>完成后生成带字幕视频</b><small>黑框白字2 · 白字黑框</small></span>
      <input type="checkbox" name="burn_after_translation" form="translation-advanced-form" ${burnRequested ? "checked" : ""} ${burnDisabled ? "disabled" : ""}>
    </label>
    ${burnDisabled ? `<p class="form-note burn-capability-note">${escapeHtml(burnTitle)}</p>` : ''}
    <details class="translation-advanced">
      <summary>高级设置</summary>
      <form id="translation-advanced-form">
        <label>Round 2 模型（可选）<input name="round2_model" value="${escapeHtml(round2Model)}" autocomplete="off" spellcheck="false"><small>留空沿用主模型</small></label>
        <label>批次大小<input required name="batch_size" type="number" inputmode="numeric" value="${escapeHtml(batchSize)}" min="1" max="100"></label>
        <label title="${hasVideo ? "使用视频复核高风险片段" : "任务没有视频，无法启用"}"><input type="checkbox" name="local_whisper" ${options.local_whisper && hasVideo ? "checked" : ""} ${hasVideo ? "" : "disabled"}> 风险段局部 Whisper</label>
      </form>
    </details>
    <div class="translation-config-slot">${repair}</div>
  </section>`;
}

function renderTranslationConfigRepair(job) {
  const options = job.options || {};
  const model = options.model || userSettings.model || "";
  const baseUrl = options.base_url || userSettings.base_url || "";
  const keyStatus = userSettings.api_key_configured
    ? "API Key 已安全保存；留空继续使用"
    : "请输入并安全保存 API Key";
  return `<form class="translation-config-repair" id="translation-config-repair-form">
    <h4>${job.error_code === "authentication_error" ? "更新失效的 API 配置" : "补齐 API 配置"}</h4>
    <label>模型名称<input required name="model" value="${escapeHtml(model)}" autocomplete="off" spellcheck="false"></label>
    <label>API Base URL<input required name="base_url" type="url" value="${escapeHtml(baseUrl)}" autocomplete="off" spellcheck="false"></label>
    <label>API Key<input name="api_key" type="password" autocomplete="off" spellcheck="false"><small>${escapeHtml(keyStatus)}</small></label>
  </form>`;
}

function renderOptionalCapcutSource(job) {
  if (!job.url) return "";
  const label = sourceLanguageLabel(job.options?.source_language || "en");
  return `<div class="optional-capcut-source">
    <label class="optional-capcut-label"><input type="checkbox" class="optional-capcut-toggle"> 补充剪映源语言字幕（可选）</label>
    <div class="optional-capcut-controls" hidden>
      <div class="capcut-launch">
        <span>可打开剪映生成${label}字幕，再作为补充来源上传。</span>
        <button type="button" class="open-capcut-app">打开剪映</button>
      </div>
      <p class="form-note capcut-launch-note" aria-live="polite"></p>
      <form class="capcut-form" data-job="${job.id}">
        <label>剪映${label}字幕<input required type="file" name="capcut_en" accept=".srt"></label>
        <button type="submit" class="run-button"><span class="button-label">上传字幕</span><span aria-hidden="true">→</span></button>
        <p class="form-note" aria-live="polite"></p>
      </form>
    </div>
  </div>`;
}

function collectTranslationSettings({ requireFreshKey = false } = {}) {
  const payload = new FormData();
  for (const selector of ["#translation-advanced-form", "#translation-config-repair-form"]) {
    const form = $(selector);
    if (!form) continue;
    if (!form.reportValidity()) return null;
    for (const [key, value] of new FormData(form).entries()) payload.set(key, value);
  }
  if (!payload.has("local_whisper")) payload.set("local_whisper", "false");
  if (!payload.has("burn_after_translation")) payload.set("burn_after_translation", "false");
  payload.set("dry_run", "false");
  if (requireFreshKey && !String(payload.get("api_key") || "").trim()) {
    $("#translation-config-repair-form [name=api_key]")?.focus();
    return null;
  }
  return { payload };
}

function displayStatus(status) {
  const mark = STATUS_MARKS[status] || '•';
  return `<span class="status ${escapeHtml(status)}"><span class="status-mark" aria-hidden="true">${mark}</span>${escapeHtml(STATUS_LABELS[status] || status)}</span>`;
}

/* ---- 任务列表 ---- */

function renderJobCard(job) {
      const progress = job.progress || { completed: 0, total: 0 };
      const progressPercent = progress.total ? Math.round(progress.completed / progress.total * 100) : 0;
      const thumbnail = job.artifacts?.thumbnail
        ? `/api/jobs/${job.id}/artifacts/thumbnail`
        : '';
      const etaSeconds = progress.completed > 0 && progress.total > progress.completed
        ? Math.round((job.elapsed_seconds || 0) / progress.completed * (progress.total - progress.completed))
        : 0;
      const jobLanguagePair = `${sourceLanguageLabel(job.options?.source_language || 'en')} → 简体中文`;
      const batchChecked = selectedJobIds.has(job.id);
      return `
      <div class="job-wrap" data-wrap-id="${escapeHtml(job.id)}">
      <label class="job-check" title="选择任务">
        <input type="checkbox" class="job-check-input" data-check-id="${escapeHtml(job.id)}" aria-label="选择任务：${escapeHtml(jobTitle(job))}" ${batchChecked ? 'checked' : ''}>
        <span class="job-check-box" aria-hidden="true"></span>
      </label>
      <button type="button" class="job job-${escapeHtml(job.status)} ${selectedJob?.id === job.id ? 'selected' : ''}" data-id="${escapeHtml(job.id)}" data-status="${escapeHtml(job.status)}" aria-pressed="${selectedJob?.id === job.id}">
        ${thumbnail
          ? `<img class="job-thumbnail" src="${thumbnail}" alt="" width="112" height="63" loading="lazy">`
          : '<span class="job-thumbnail job-thumbnail-placeholder" aria-hidden="true">菲</span>'}
        <span class="job-card-copy"><span class="job-top"><span class="job-title">${escapeHtml(jobTitle(job))}</span>${displayStatus(job.status)}</span>
        <span class="job-meta"><span>${escapeHtml(jobLanguagePair)} · ${escapeHtml(job.metadata?.channel || stageLabel(job))}</span><time datetime="${escapeHtml(job.created_at)}">${escapeHtml(formatDate(job.created_at))}</time></span>
        <span class="job-facts">${job.metadata?.duration ? `${formatDuration(job.metadata.duration)} · ` : ''}${job.disk_bytes == null ? '大小见详情' : formatBytes(job.disk_bytes)}${job.metrics?.quality?.union_subtitles ? ` · ${metricNumber(job.metrics.quality.union_subtitles)} 条字幕` : ''}${job.status === 'completed' ? ` · 总耗时 ${formatDuration(job.elapsed_seconds)} · 完成于 ${formatDate(job.completed_at || job.updated_at)}` : ''}</span>
        <span class="job-stage-line"><span>${escapeHtml(stageLabel(job))}</span>${progress.total ? `<b>${progressPercent}%${etaSeconds ? ` · 约 ${formatDuration(etaSeconds)}` : ''}</b>` : ''}</span>
        ${progress.total ? `<span class="job-mini-progress" aria-hidden="true"><span style="width:${progressPercent}%"></span></span>` : ''}
        </span>
      </button>
      ${renderErrorBanner(job)}
      </div>`;
}

function jobMatchesCurrentFilters(job) {
  const query = String($('#job-search')?.value || '').trim().toLowerCase();
  const statusFilter = $('#job-status-filter')?.value || '';
  const haystack = [jobTitle(job), job.metadata?.channel, job.id].join(' ').toLowerCase();
  if (query && !haystack.includes(query)) return false;
  if (!jobMatchesSummaryFilter(job, jobSummaryFilter)) return false;
  if (statusFilter === 'archived') return Boolean(job.archived);
  if (job.archived) return false;
  if (statusFilter === 'active') return ACTIVE_JOB_STATUSES.includes(job.status);
  return !statusFilter || job.status === statusFilter;
}

function renderCurrentJobsListHtml() {
  const visibleJobs = allJobs.filter(jobMatchesCurrentFilters);
  return visibleJobs.length ? visibleJobs.map(renderJobCard).join('') : `<div class="empty">
    <span class="empty-mark" aria-hidden="true">＋</span>
    <b>菲比还没有收到任务</b><small>粘贴链接或上传两份 SRT，马上开始</small>
    <button type="button" data-open-new-task>新建翻译任务</button>
  </div>`;
}

function updateVisibleJobCount() {
  const visible = allJobs.filter(jobMatchesCurrentFilters).length;
  const count = $('#job-count');
  if (count) count.textContent = `${visible} / ${allJobs.length} 个任务`;
}

function copyElementAttributes(target, source) {
  [...target.attributes].forEach(attribute => {
    if (!source.hasAttribute(attribute.name)) target.removeAttribute(attribute.name);
  });
  [...source.attributes].forEach(attribute => target.setAttribute(attribute.name, attribute.value));
}

function patchJobWrap(existing, replacement) {
  copyElementAttributes(existing, replacement);
  existing.classList.toggle('batch-selected', selectedJobIds.has(existing.dataset.wrapId));

  const currentCheck = existing.querySelector(':scope > .job-check');
  const nextCheck = replacement.querySelector(':scope > .job-check');
  if (currentCheck && nextCheck) {
    copyElementAttributes(currentCheck, nextCheck);
    const currentInput = currentCheck.querySelector('.job-check-input');
    const nextInput = nextCheck.querySelector('.job-check-input');
    if (currentInput && nextInput) {
      copyElementAttributes(currentInput, nextInput);
      currentInput.checked = nextInput.checked;
    }
  }

  const currentCard = existing.querySelector(':scope > .job');
  const nextCard = replacement.querySelector(':scope > .job');
  if (!currentCard || !nextCard) {
    existing.replaceChildren(...replacement.childNodes);
    return existing;
  }
  copyElementAttributes(currentCard, nextCard);

  const currentThumbnail = currentCard.querySelector(':scope > .job-thumbnail');
  const nextThumbnail = nextCard.querySelector(':scope > .job-thumbnail');
  if (currentThumbnail && nextThumbnail && currentThumbnail.outerHTML !== nextThumbnail.outerHTML) {
    currentThumbnail.replaceWith(nextThumbnail);
  } else if (!currentThumbnail && nextThumbnail) {
    currentCard.prepend(nextThumbnail);
  } else if (currentThumbnail && !nextThumbnail) {
    currentThumbnail.remove();
  }

  const currentCopy = currentCard.querySelector(':scope > .job-card-copy');
  const nextCopy = nextCard.querySelector(':scope > .job-card-copy');
  if (currentCopy && nextCopy) {
    if (currentCopy.innerHTML !== nextCopy.innerHTML) currentCopy.innerHTML = nextCopy.innerHTML;
  } else {
    currentCard.innerHTML = nextCard.innerHTML;
  }

  const currentError = existing.querySelector(':scope > .job-error-banner');
  const nextError = replacement.querySelector(':scope > .job-error-banner');
  if (currentError && nextError && currentError.outerHTML !== nextError.outerHTML) {
    currentError.replaceWith(nextError);
  } else if (!currentError && nextError) {
    existing.append(nextError);
  } else if (currentError && !nextError) {
    currentError.remove();
  }
  return existing;
}

function replaceJobCard(job) {
  if (selectedJob?.id === job.id) {
    selectedJob = job;
    renderDetail();
  }
  const index = allJobs.findIndex(candidate => candidate.id === job.id);
  if (index < 0) {
    render.schedule('jobs-list', () => loadJobs({ silent: true }));
    return;
  }
  allJobs[index] = job;
  renderJobSummary(allJobs);
  const existing = document.querySelector(`[data-wrap-id="${CSS.escape(job.id)}"]`);
  if (!jobMatchesCurrentFilters(job)) {
    existing?.remove();
    updateVisibleJobCount();
    return;
  }
  if (!existing) {
    render.schedule('jobs-list', () => loadJobs({ silent: true }));
    return;
  }
  const template = document.createElement('template');
  template.innerHTML = renderJobCard(job).trim();
  const fragment = template.content.firstElementChild;
  if (!fragment) return;
  if (isUserInteractingWithPage()) {
    pendingJobsListRender = true;
    pendingJobsListScrollTop = document.querySelector('#jobs-list')?.scrollTop || 0;
    scheduleJobsListApply();
    updateVisibleJobCount();
    updateBatchBar();
    return;
  }
  patchJobWrap(existing, fragment);
  updateVisibleJobCount();
  updateBatchBar();
}

async function refreshJobCard(jobId, generation) {
  const known = allJobs.find(job => job.id === jobId);
  if (known && Number(known.generation || 0) > Number(generation || 0)) return;
  try {
    const suffix = selectedJob?.id === jobId ? '' : '?summary=true';
    let job = await transport.get(`/api/jobs/${jobId}${suffix}`);
    if (job.summary_only && selectedJob?.id === jobId) {
      job = await transport.get(`/api/jobs/${jobId}`);
    }
    if (Number(job.generation || 0) < Number(generation || 0)) return;
    replaceJobCard(job);
  } catch (error) {
    render.schedule('jobs-list', () => loadJobs({ silent: true }));
  }
}

function startGlobalJobStream() {
  if (localSessionExpired) return;
  if (globalJobEventSource) globalJobEventSource.close();
  const es = new EventSource('/api/job-events/stream');
  globalJobEventSource = es;
  es.addEventListener('job', event => {
    let payload;
    try { payload = JSON.parse(event.data); } catch (error) { return; }
    if (!payload?.job_id) return;
    if (payload.type === 'job_added' || payload.type === 'job_deleted') {
      render.schedule('jobs-list', () => loadJobs({ silent: true }));
      return;
    }
    refreshJobCard(payload.job_id, payload.generation);
  });
  es.onopen = () => {
    noteStreamSuccess();
    render.schedule('jobs-list', () => loadJobs({ silent: true }));
  };
  es.onerror = () => noteStreamFailure();
}

async function loadJobs(options = {}) {
  /* 兜底轮询走 silent：服务端不可用时不该每两秒弹一次 toast */
  const silent = Boolean(options && options.silent);
  const requestVersion = ++jobsRequestVersion;
  const list = $('#jobs-list');
  list?.setAttribute('aria-busy', 'true');
  if (list && !allJobs.length) {
    list.innerHTML = `<div class="loading-state" role="status">
      <span class="loading-orbit" aria-hidden="true"></span>
      <b>正在读取任务…</b><small>首次打开时会扫描 Download 目录</small>
    </div>`;
  }
  try {
    /* 传输层去重：SSE status/done 与手动刷新同时触发时只发一次 GET */
    const jobs = await transport.get('/api/jobs');
    if (requestVersion !== jobsRequestVersion) return;
    allJobs = jobs;
    let current = jobs.find(job => job.id === selectedJob?.id);
    if (current?.summary_only) {
      const currentId = current.id;
      const details = await transport.get(`/api/jobs/${currentId}`);
      if (requestVersion !== jobsRequestVersion) return;
      current = selectedJob?.id === currentId ? details : null;
    }
    if (current && JSON.stringify(current) !== JSON.stringify(selectedJob)) {
      selectedJob = current;
      renderDetail();
    }
    document.body.classList.toggle('has-jobs', jobs.length > 0);
    document.body.classList.toggle('empty-state', jobs.length === 0);
    if ($('#review')?.classList.contains('active')) renderReviewTaskContext();
    renderJobSummary(jobs);
    const visibleJobs = jobs.filter(jobMatchesCurrentFilters);
    $('#job-count').textContent = `${visibleJobs.length} / ${jobs.length} 个任务`;
    syncUrl({
      jobQuery: $('#job-search')?.value || '',
      jobStatus: $('#job-status-filter')?.value || '',
    });
    // UX-F: preserve jobs-list scroll position across refresh/redraw.
    const jobsListEl = $('#jobs-list');
    const previousScrollTop = jobsListEl?.scrollTop || 0;
    const listHtml = renderCurrentJobsListHtml();
    updateBatchBar();
    // UX-F: 滚动位置恢复与交互保持都交给 applyJobsListHtml。
    applyJobsListHtml(listHtml, previousScrollTop);
    return jobs;
  } catch (error) {
    pendingJobsListRender = false;
    $('#jobs-list').innerHTML = `<div class="empty error-state">
      <span class="empty-mark" aria-hidden="true">!</span>
      <b>任务列表读取失败</b><small>${escapeHtml(error.message)}</small>
      <button type="button" data-retry="jobs">重新读取任务</button>
    </div>`;
    $('#health').textContent = localSessionExpired ? '会话已失效' : '服务不可用';
    if (!silent) showToast('无法读取任务列表，请确认本地服务仍在运行', 'error');
    return [];
  } finally {
    if (requestVersion === jobsRequestVersion) {
      list?.removeAttribute('aria-busy');
    }
  }
}

/* ---- 交互保持窗口 ----
   用户正 hover、按住鼠标或输入时只记录刷新意图，不保存可能过期的 HTML。
   空闲后基于最新 allJobs / selectedJob 重建，并按任务 ID 复用现有卡片节点；
   这样实时拉取不会打断 hover、焦点或选中边框。 */

function isUserInteractingWithPage() {
  if (pointerIsDown) return true;
  const active = document.activeElement;
  if (active && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)) return true;
  /* 只看守正在被重绘的列表：鼠标停在页面别处不该让列表永远不刷新 */
  return Boolean(document.querySelector('#jobs-list :hover'));
}

function reconcileJobsListHtml(list, listHtml) {
  const template = document.createElement('template');
  template.innerHTML = listHtml.trim();
  const nextNodes = [...template.content.children];
  const currentNodes = [...list.children];
  const nextIsCards = nextNodes.length > 0
    && nextNodes.every(node => node.matches('.job-wrap[data-wrap-id]'));
  const currentIsCards = currentNodes.length > 0
    && currentNodes.every(node => node.matches('.job-wrap[data-wrap-id]'));
  if (!nextIsCards || !currentIsCards) {
    list.replaceChildren(...nextNodes);
    return;
  }

  const currentById = new Map(currentNodes.map(node => [node.dataset.wrapId, node]));
  const nextIds = new Set(nextNodes.map(node => node.dataset.wrapId));
  currentNodes.forEach(node => {
    if (!nextIds.has(node.dataset.wrapId)) node.remove();
  });
  nextNodes.forEach((nextNode, index) => {
    const currentNode = currentById.get(nextNode.dataset.wrapId);
    const node = currentNode || nextNode;
    if (currentNode) patchJobWrap(currentNode, nextNode);
    const atIndex = list.children[index];
    if (atIndex !== node) list.insertBefore(node, atIndex || null);
  });
}

function writeJobsListHtml(listHtml, scrollTop) {
  const list = $('#jobs-list');
  if (!list) return;
  pendingJobsListRender = false;
  reconcileJobsListHtml(list, listHtml);
  list.scrollTop = scrollTop || 0;
}

function scheduleJobsListApply() {
  if (jobsListApplyTimer) return;
  jobsListApplyTimer = setTimeout(() => {
    jobsListApplyTimer = 0;
    if (!pendingJobsListRender) return;
    if (isUserInteractingWithPage()) {
      scheduleJobsListApply();
      return;
    }
    writeJobsListHtml(renderCurrentJobsListHtml(), pendingJobsListScrollTop);
  }, JOBS_LIST_APPLY_PROBE_MS);
}

function applyJobsListHtml(listHtml, scrollTop) {
  const list = $('#jobs-list');
  if (!list) return;
  if (isUserInteractingWithPage()) {
    pendingJobsListRender = true;
    pendingJobsListScrollTop = scrollTop || 0;
    scheduleJobsListApply();
    return;
  }
  writeJobsListHtml(listHtml, scrollTop);
}

document.addEventListener('pointerdown', () => { pointerIsDown = true; }, true);
document.addEventListener('pointerup', () => { pointerIsDown = false; }, true);
document.addEventListener('pointercancel', () => { pointerIsDown = false; }, true);

/* 封面 URL 失效（图片过期 / 离线）时退回文字占位，而不是显示碎图标。
   error 事件不冒泡，所以用捕获阶段的委托监听。 */
$('#jobs-list')?.addEventListener('error', event => {
  const image = event.target;
  if (!(image instanceof HTMLImageElement)) return;
  if (!image.classList.contains('job-thumbnail')) return;
  const placeholder = document.createElement('span');
  placeholder.className = 'job-thumbnail job-thumbnail-placeholder';
  placeholder.setAttribute('aria-hidden', 'true');
  placeholder.textContent = '菲';
  image.replaceWith(placeholder);
}, true);

$('#jobs-list')?.addEventListener('click', event => {
  const retry = event.target.closest('[data-retry="jobs"]');
  if (retry) {
    loadJobs();
    return;
  }
  const check = event.target.closest('.job-check-input');
  if (check) {
    const id = check.dataset.checkId;
    if (selectedJobIds.has(id)) {
      selectedJobIds.delete(id);
      check.checked = false;
    } else {
      selectedJobIds.add(id);
      check.checked = true;
    }
    syncBatchCardState(check, selectedJobIds.has(id));
    updateBatchBar();
    event.stopPropagation();
    return;
  }
  const card = event.target.closest('.job[data-id]');
  if (card) selectJob(card.dataset.id);
});

/* ---- Batch selection ---- */

function visibleJobIds() {
  return Array.from(document.querySelectorAll('#jobs-list .job[data-id]'))
    .map(node => node.dataset.id);
}

function updateBatchBar() {
  const bar = $('#batch-bar');
  const count = $('#batch-count');
  const retryBtn = $('#batch-retry');
  const archiveBtn = $('#batch-archive');
  const deleteBtn = $('#batch-delete');
  const clearBtn = $('#batch-clear');
  const selectAll = $('#job-select-all');
  if (!bar) return;
  const selected = selectedJobIds.size;
  count.textContent = `已选择 ${selected} 项`;
  if (retryBtn) retryBtn.disabled = !batchRetryEligibility(Array.from(selectedJobIds)).eligible;
  if (archiveBtn) archiveBtn.disabled = selected === 0;
  if (deleteBtn) deleteBtn.disabled = selected === 0;
  if (clearBtn) clearBtn.disabled = selected === 0;
  if (selectAll) {
    const visible = visibleJobIds();
    const visibleSelected = visible.filter(id => selectedJobIds.has(id)).length;
    selectAll.checked = visible.length > 0 && visibleSelected === visible.length;
    selectAll.indeterminate = visibleSelected > 0 && visibleSelected < visible.length;
  }
}

function syncBatchCheckboxes() {
  document.querySelectorAll('#jobs-list .job-check-input').forEach(input => {
    const selected = selectedJobIds.has(input.dataset.checkId);
    input.checked = selected;
    syncBatchCardState(input, selected);
  });
}

function syncBatchCardState(checkInput, selected) {
  const wrap = checkInput.closest('.job-wrap');
  if (wrap) wrap.classList.toggle('batch-selected', selected);
}

function clearBatchSelection() {
  selectedJobIds.clear();
  syncBatchCheckboxes();
  updateBatchBar();
}

function selectAllVisibleJobs() {
  const visible = visibleJobIds();
  const allChecked = visible.length > 0 && visible.every(id => selectedJobIds.has(id));
  if (allChecked) {
    visible.forEach(id => selectedJobIds.delete(id));
  } else {
    visible.forEach(id => selectedJobIds.add(id));
  }
  syncBatchCheckboxes();
  updateBatchBar();
}

$('#job-select-all')?.addEventListener('change', selectAllVisibleJobs);
$('#batch-clear')?.addEventListener('click', clearBatchSelection);
$('#batch-retry')?.addEventListener('click', () => {
  if (!selectedJobIds.size) return;
  window.batchJobAction?.('retry', Array.from(selectedJobIds));
});
$('#batch-archive')?.addEventListener('click', () => {
  if (!selectedJobIds.size) return;
  window.batchJobAction?.('archive', Array.from(selectedJobIds));
});
$('#batch-delete')?.addEventListener('click', () => {
  if (!selectedJobIds.size) return;
  window.batchJobAction?.('delete', Array.from(selectedJobIds));
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
  if (typing && document.activeElement?.id !== 'job-search') return;
  if (selectedJobIds.size) {
    clearBatchSelection();
    event.preventDefault();
  }
});

/* ---- Batch actions (UX-E) ---- */

function batchSelectionStats(ids) {
  const jobs = ids
    .map(id => allJobs.find(job => job.id === id))
    .filter(Boolean);
  const count = (status) => jobs.filter(job => job.status === status).length;
  return {
    total: jobs.length,
    completed: count('completed'),
    failed: count('failed'),
    paused: count('paused'),
    running: count('running') + count('queued') + count('waiting_capcut') + count('ready_for_translation'),
    hasArtifacts: jobs.some(job => Object.keys(job.artifacts || {}).length > 0),
  };
}

function batchRetryEligibility(ids) {
  const jobs = ids.map(id => allJobs.find(job => job.id === id)).filter(Boolean);
  if (!jobs.length || jobs.some(job => job.status !== 'failed')) {
    return { eligible: false, reason: '只能批量重试失败任务', needsKey: false };
  }
  const profiles = jobs.map(job => {
    const options = job.options || {};
    const inputs = job.inputs || {};
    const download = Boolean(job.url) && !(inputs.primary_srt || inputs.capcut_en);
    return JSON.stringify([
      download ? 'download' : 'translation', job.error_code || '',
      options.model || '', options.round2_model || '', options.base_url || '',
      options.proxy || '', options.source_language || 'en',
      options.target_language || 'zh-CN', Boolean(options.dry_run),
    ]);
  });
  if (new Set(profiles).size !== 1) {
    return { eligible: false, reason: '错误类型或配置不同，请分组重试', needsKey: false };
  }
  const profile = JSON.parse(profiles[0]);
  const download = profile[0] === 'download';
  if (download && jobs.some(job => job.error_code === 'youtube_auth_required'
      || job.options?.youtube_auth === 'file')) {
    return { eligible: false, reason: '需要 Cookie 的任务请逐个更新认证', needsKey: false };
  }
  return {
    eligible: true, reason: '',
    needsKey: !download && !Boolean(profile[profile.length - 1]),
  };
}

window.batchJobAction = async function batchJobAction(action, ids) {
  if (action === 'retry') {
    const eligibility = batchRetryEligibility(ids);
    if (!eligibility.eligible) {
      showToast(eligibility.reason, 'info');
      return;
    }
    const confirmed = await confirmAction({
      title: '重试所选失败任务',
      message: `将按原配置继续 ${ids.length} 个同类失败任务，并复用安全保存的 API Key。`,
      confirmLabel: '开始重试',
    });
    if (!confirmed) return;
    try {
      const result = await api('/api/jobs/batch-retry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, restart: false }),
      });
      showToast(`已重新排队 ${result.queued.length} 个任务`, result.queued.length ? 'success' : 'info');
      clearBatchSelection();
      loadJobs();
    } catch (error) {
      showToast(error.message, 'error');
    }
    return;
  }
  if (action === 'archive') {
    try {
      const result = await api('/api/jobs/batch-archive', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, archived: true }),
      });
      showToast(result.updated.length ? `已归档 ${result.updated.length} 个任务` : '没有任务被归档', result.updated.length ? 'success' : 'info');
      clearBatchSelection();
      loadJobs();
    } catch (error) {
      showToast(error.message, 'error');
    }
    return;
  }
  if (action === 'delete') {
    const stats = batchSelectionStats(ids);
    const runningIncluded = stats.running > 0;
    const artifactNote = stats.hasArtifacts ? '（将同时删除任务产物文件）' : '';
    const confirmed = await confirmAction({
      title: '批量删除任务',
      message: `删除 ${stats.total} 个任务？\n已完成 ${stats.completed} · 失败 ${stats.failed} · 暂停 ${stats.paused} · 进行中 ${stats.running}${artifactNote}\n${runningIncluded ? '⚠ 进行中的任务会被跳过（需先停止）。' : '进行中的任务不会被删除。'}`,
      confirmLabel: '删除',
      cancelLabel: '取消',
      danger: true,
    });
    if (!confirmed) return;
    try {
      const result = await api('/api/jobs/batch-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      if (result.deleted.length) showToast(`已删除 ${result.deleted.length} 个任务`, 'success');
      if (result.blocked.length) showToast(`${result.blocked.length} 个进行中任务已跳过（需先停止）`, 'info');
      clearBatchSelection();
      loadJobs();
    } catch (error) {
      showToast(error.message, 'error');
    }
    return;
  }
  showToast(`未知批量操作：${action}`, 'error');
};

/* ---- SSE 实时日志流 ---- */

function startStream(jobId) {
  stopStream();
  const cursor = Math.max(0, ...(selectedJob?.logs || []).map((log, index) => Number(log.seq || index + 1)));
  const es = new EventSource(`/api/jobs/${jobId}/stream?after=${cursor}`);
  eventSource = es;
  /* 连上就说明 SSE 还活着：清掉失败计数并停掉兜底轮询 */
  es.onopen = () => noteStreamSuccess();

  es.onmessage = (event) => {
    try {
      const log = JSON.parse(event.data);
      appendLog(log);
    } catch (_) { /* 忽略解析错误 */ }
  };

  es.addEventListener('status', async () => {
    try {
      const previousStatus = selectedJob?.status;
      /* 与 refreshSelectedJob 共用同一资源键：并发触发时只发一次 GET */
      const refreshed = await transport.get(`/api/jobs/${jobId}`, { key: `job:${jobId}` });
      if (selectedJob?.id !== jobId) return;
      selectedJob = refreshed;
      const index = allJobs.findIndex(job => job.id === refreshed.id);
      if (index >= 0) allJobs[index] = refreshed;
      const completedNow = previousStatus !== 'completed' && refreshed.status === 'completed';
      if (completedNow) {
        allRiskItems = [];
        riskItems = [];
        risksLoadedJobId = '';
        activeRiskKeys.review = '';
        activeRiskKeys.detail = '';
      }
      if (previousStatus === refreshed.status) {
        render.schedule('progress', () => updateProgressView(refreshed));
      } else {
        render.schedule('detail', () => renderDetail());
      }
      if (completedNow) {
        celebrateCompletion(refreshed);
        if (activeDetailTab === 'risk' || $('#review')?.classList.contains('active')) {
          loadRisks({ preservePage: true });
        }
      }
      if (previousStatus !== refreshed.status) {
        render.schedule('jobs-list', () => loadJobs());
      }
    } catch (_) { /* 任务可能已删除 */ }
  });

  es.addEventListener('progress', (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (!selectedJob || selectedJob.id !== jobId) return;
      /* 同帧合并：连续 progress 事件只产生一次 DOM 写入，取最新值 */
      render.schedule('progress', () => updateProgressView(payload));
    } catch (_) { /* 忽略解析错误 */ }
  });

  es.addEventListener('done', () => {
    es.close();
    if (eventSource === es) eventSource = null;
    /* done 紧跟 status 时不再叠加重复请求：调度器同帧合并、传输层去重 GET */
    render.schedule('job-detail', () => { refreshSelectedJob(jobId); });
    render.schedule('jobs-list', () => { loadJobs(); });
  });

  es.onerror = () => {
    // EventSource reconnects automatically after transient server/network errors.
    if (selectedJob?.id !== jobId || ['completed', 'failed', 'cancelled'].includes(selectedJob.status)) {
      es.close();
      if (eventSource === es) eventSource = null;
      return;
    }
    /* 连续多次重连失败才降级：一次网络抖动不值得换机制 */
    noteStreamFailure();
  };
}

function stopStream() {
  if (eventSource) eventSource.close();
  eventSource = null;
  stopJobsPolling();
}

/* ---- SSE 断线兜底轮询 ----
   EventSource 自身会重连，但服务端重启或代理长时间断开后可能一直连不上，
   任务列表就会停在旧状态。连续失败达到阈值后改用自适应轮询兜底。 */

function jobsPollIntervalMs() {
  if (!allJobs.length) return JOBS_POLL_EMPTY_MS;
  const busy = allJobs.some(job => !job.archived && (
    ACTIVE_JOB_STATUSES.includes(job.status)
    || ['queued', 'running'].includes(job.exports?.burned_video?.status)
  ));
  return busy ? JOBS_POLL_ACTIVE_MS : JOBS_POLL_IDLE_MS;
}

function startJobsPolling() {
  if (localSessionExpired) return;
  if (jobsPollTimer || document.hidden) return;
  /* 用 setTimeout 链而不是 setInterval：间隔要随任务状态自适应变化 */
  jobsPollTimer = setTimeout(async () => {
    jobsPollTimer = 0;
    if (document.hidden) return;
    await loadJobs({ silent: true });
    startJobsPolling();
  }, jobsPollIntervalMs());
}

function stopJobsPolling() {
  if (jobsPollTimer) clearTimeout(jobsPollTimer);
  jobsPollTimer = 0;
}

function noteStreamFailure() {
  jobsPollFailures += 1;
  if (jobsPollFailures >= JOBS_POLL_FAILURE_THRESHOLD) startJobsPolling();
}

function noteStreamSuccess() {
  jobsPollFailures = 0;
  stopJobsPolling();
}

document.addEventListener('visibilitychange', () => {
  /* 标签页不可见时停止轮询，回来后立即补一次刷新再继续 */
  if (document.hidden) {
    stopJobsPolling();
    return;
  }
  if (jobsPollFailures >= JOBS_POLL_FAILURE_THRESHOLD) {
    loadJobs({ silent: true });
    startJobsPolling();
  }
});

async function refreshSelectedJob(expectedId = selectedJob?.id) {
  if (!expectedId || selectedJob?.id !== expectedId) return;
  try {
    const previousStatus = selectedJob?.status;
    const refreshed = await transport.get(`/api/jobs/${expectedId}`, { key: `job:${expectedId}` });
    if (selectedJob?.id !== expectedId) return;
    selectedJob = refreshed;
    const completedNow = previousStatus !== 'completed' && refreshed.status === 'completed';
    if (completedNow) {
      allRiskItems = [];
      riskItems = [];
      risksLoadedJobId = '';
      activeRiskKeys.review = '';
      activeRiskKeys.detail = '';
    }
    renderDetail();
    if (completedNow) {
      celebrateCompletion(refreshed);
      if (activeDetailTab === 'risk' || $('#review')?.classList.contains('active')) {
        loadRisks({ preservePage: true });
      }
    }
  } catch (_) { /* 任务可能已删除 */ }
}

function displayLogMessage(message) {
  const text = String(message || '');
  if (text.includes('\uFFFD')) {
    const progress = text.match(/(\d+(?:\.\d+)?%\s*\|.*\|\s*ETA\s+.*)$/);
    if (progress) return `下载进度 ${progress[1]}`;
  }
  return text;
}

function appendLog(log) {
  if (selectedJob) {
    selectedJob.logs = [...(selectedJob.logs || []), log].slice(-300);
  }
  const logsEl = document.querySelector('#detail .logs');
  if (!logsEl) return;
  const div = document.createElement('div');
  if (log.kind === 'error') div.className = 'error';
  div.textContent = `[${(log.at || '').slice(11, 19)}] ${displayLogMessage(log.message)}`;
  logsEl.appendChild(div);
  // UX-F: only follow bottom when the user hasn't scrolled up to read history.
  if (logAutoFollow) logsEl.scrollTop = logsEl.scrollHeight;
}

function trackLogScroll(scrollEl) {
  if (!scrollEl || scrollEl.dataset.logScrollTracked) return;
  scrollEl.dataset.logScrollTracked = '1';
  scrollEl.addEventListener('scroll', () => {
    const nearBottom = scrollEl.scrollTop + scrollEl.clientHeight >= scrollEl.scrollHeight - 40;
    logAutoFollow = nearBottom;
    if (nearBottom) scrollEl.scrollTop = scrollEl.scrollHeight;
  }, { passive: true });
}

/* ---- 选中任务 ---- */

function syncSelectedJobCard() {
  document.querySelectorAll('#jobs-list .job[data-id]').forEach(card => {
    const selected = card.dataset.id === selectedJob?.id;
    card.classList.toggle('selected', selected);
    card.setAttribute('aria-pressed', String(selected));
  });
}

function scrollDetailLogToBottom() {
  const logs = document.querySelector('#detail-panel-log .log-scroll');
  if (logs) logs.scrollTop = logs.scrollHeight;
}

function restoreDetailTabState({ focus = false } = {}) {
  const tabName = DETAIL_TAB_NAMES.includes(activeDetailTab) ? activeDetailTab : 'overview';
  document.querySelectorAll('#detail [role="tab"][data-detail-tab]').forEach(tab => {
    const selected = tab.dataset.detailTab === tabName;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    const panel = document.getElementById(tab.getAttribute('aria-controls'));
    if (panel) panel.hidden = !selected;
    if (selected && focus) tab.focus();
  });
  if (tabName === 'log') requestAnimationFrame(scrollDetailLogToBottom);
}

async function changeActiveDetailTab(tabName, { focus = false } = {}) {
  if (!DETAIL_TAB_NAMES.includes(tabName)) return false;
  if (tabName !== activeDetailTab && activeDetailTab === 'risk' && !await confirmDiscardRiskEdit()) {
    restoreDetailTabState({ focus: true });
    return false;
  }
  if (tabName !== activeDetailTab && activeDetailTab === 'risk') riskDirty = false;
  const changed = tabName !== activeDetailTab;
  activeDetailTab = tabName;
  restoreDetailTabState({ focus });
  if (changed && tabName === 'risk' && selectedJob) loadRisks({ preservePage: true });
  return true;
}

function bindDetailTabNavigation() {
  const tabs = [...document.querySelectorAll('#detail [role="tab"][data-detail-tab]')];
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => changeActiveDetailTab(tab.dataset.detailTab));
    tab.addEventListener('keydown', async event => {
      let nextIndex = null;
      if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') nextIndex = 0;
      if (event.key === 'End') nextIndex = tabs.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      await changeActiveDetailTab(tabs[nextIndex].dataset.detailTab, { focus: true });
    });
  });
}

bindDetailTabNavigation();

async function selectJob(id, { preserveRiskPage = false } = {}) {
  if (!await confirmDiscardRiskEdit()) return;
  riskDirty = false;
  const previousJobId = selectedJob?.id || '';
  const version = ++selectionVersion;
  stopStream();
  $('#detail')?.setAttribute('aria-busy', 'true');
  try {
    const job = await transport.get(`/api/jobs/${id}`, { key: `job:${id}` });
    if (version !== selectionVersion) return;
    selectedJob = job;
    if (previousJobId !== job.id) {
      activeDetailTab = 'overview';
      /* 唯一筛选状态源：切换任务时重置共享 select 与分页 */
      const sharedFilter = $('#risk-filter');
      if (sharedFilter) {
        sharedFilter.value = appSettings.risk_default_filter || 'unresolved';
      }
      riskPage = 1;
      activeRiskKeys.review = '';
      activeRiskKeys.detail = '';
      allRiskItems = [];
      riskItems = [];
      risksLoadedJobId = '';
    }
    const existingIndex = allJobs.findIndex(item => item.id === job.id);
    if (existingIndex >= 0) allJobs[existingIndex] = job;
    if ($('#review')?.classList.contains('active')) renderReviewTaskContext();
    updatePhoebeAssistant(job);
    localStorage.setItem('subtitle_last_job', job.id);
    renderDetail();
    const detailScroll = $('.task-detail-scroll');
    if (detailScroll) detailScroll.scrollTop = 0;
    syncSelectedJobCard();
    await loadRisks({ preservePage: preserveRiskPage });
    syncUrl({ jobId: id });
    if (['queued', 'running'].includes(job.status)) startStream(id);
  } catch (error) {
    if (version !== selectionVersion) return;
    selectedJob = null;
    renderDetailEmpty(error.message);
  } finally {
    if (version === selectionVersion) $('#detail')?.removeAttribute('aria-busy');
  }
}

/* ---- 详情面板渲染 ---- */

function isYoutubeAuthFailure(job) {
  if (!job?.url || job.status !== 'failed') return false;
  const evidence = `${job.error_code || ''} ${job.error || ''}`.toLocaleLowerCase('en-US');
  return evidence.includes('youtube_auth_required')
    || evidence.includes('sign in to confirm')
    || evidence.includes('not a bot')
    || evidence.includes('too many requests')
    || evidence.includes('http error 429');
}

function renderYoutubeRecovery(job) {
  if (!isYoutubeAuthFailure(job)) return '';
  const previous = String(job.options?.youtube_auth || '').toLowerCase();
  const selected = ['chrome', 'file'].includes(previous)
    ? previous
    : 'chrome';
  const option = (value, label) => (
    `<option value="${value}" ${selected === value ? 'selected' : ''}>${label}</option>`
  );
  return `<section class="youtube-recovery" hidden aria-labelledby="youtube-recovery-title">
    <div class="youtube-recovery-heading">
      <span class="youtube-recovery-mark" aria-hidden="true">!</span>
      <div>
        <p class="eyebrow">YOUTUBE LOGIN CHECK</p>
        <h3 id="youtube-recovery-title">换一份登录 Cookie，原任务接着下</h3>
        <p>不会新建任务，也不会丢失现有设置。Cookie 只在本次下载期间使用，结束后自动删除。</p>
      </div>
    </div>
    <form class="youtube-recovery-form" data-job="${escapeHtml(job.id)}">
      <label for="retry-youtube-auth">从哪里读取 Cookie</label>
      <select id="retry-youtube-auth" name="youtube_auth">
        ${option('chrome', 'Google Chrome（自动读取登录状态）')}
        ${option('file', '粘贴 cookies.txt 内容（最稳定）')}
      </select>
      <div class="retry-cookie-paste-row" ${selected === 'file' ? '' : 'hidden'}>
        <div class="retry-cookie-paste-heading">
          <label for="retry-youtube-cookies-text">粘贴 Cookie 文本</label>
          <button type="button" class="paste-youtube-cookies">读取剪贴板</button>
        </div>
        <textarea id="retry-youtube-cookies-text" name="youtube_cookies_text" rows="6"
          autocomplete="off" autocapitalize="off" spellcheck="false"
          placeholder="# Netscape HTTP Cookie File&#10;复制全部内容后在这里按 Ctrl + V"></textarea>
      </div>
      <p class="youtube-recovery-tip">Chrome Cookie 读取失败时，请完全退出 Chrome 后再试；不想关闭浏览器就把 cookies.txt 的全部内容粘贴进来。</p>
      <button type="submit" class="retry-auth-submit"><span class="button-label">保存 Cookie 并重试下载</span><span aria-hidden="true">→</span></button>
      <p class="form-note youtube-recovery-note" aria-live="polite"></p>
    </form>
  </section>`;
}

function renderBurnedVideoExport(job, deliveryBlocked) {
  if (job.status !== 'completed' || job.options?.dry_run) return '';
  const exportState = job.exports?.burned_video || {};
  const status = exportState.status || '';
  const hasVideo = Boolean(job.inputs?.video || job.artifacts?.video);
  const hasArtifact = Boolean(job.artifacts?.['final.zh.burned.mp4']);
  deliveryBlocked = deliveryBlocked || job.burn_review_ready === false;
  const canGenerate = hasVideo && !deliveryBlocked && subtitleBurnCapability.available;
  const message = exportState.message || '';
  let body = '';

  if (deliveryBlocked) {
    body = '<p class="form-note">请先完成必审项</p>';
  } else if (status === 'completed' && hasArtifact && job.burned_video_current !== false) {
    body = `<a class="final-download burned-video-download" href="/api/jobs/${job.id}/artifacts/final.zh.burned.mp4" download>下载带字幕视频（黑框白字2 MP4） <span aria-hidden="true">↓</span></a>`;
  } else if (['queued', 'running'].includes(status)) {
    const progress = Math.max(0, Math.min(100, Number(exportState.progress) || 0));
    body = `<div class="burned-video-progress"><progress max="100" value="${progress}"></progress><span>${status === 'queued' ? '等待生成' : `正在生成 ${progress}%`}</span></div>
      <button type="button" class="cancel-burned-video" data-job="${escapeHtml(job.id)}">取消生成</button>`;
  } else if (status === 'pending' && job.burn_review_ready !== true) {
    body = `<p class="form-note">${escapeHtml(message || '处理完必审风险后会自动生成带字幕视频')}</p>`;
  } else {
    const unavailable = !hasVideo
      ? '当前任务没有视频，无法生成带字幕视频'
      : !subtitleBurnCapability.available
        ? (subtitleBurnCapability.message || '当前环境不能生成带字幕视频')
        : '';
    body = `${message ? `<p class="form-note error-hint">${escapeHtml(message)}</p>` : ''}
      ${unavailable ? `<p class="form-note">${escapeHtml(unavailable)}</p>` : ''}
      <button type="button" class="generate-burned-video" data-job="${escapeHtml(job.id)}" ${canGenerate && !deliveryRequests.has(job.id) ? '' : 'disabled'}>${status === 'failed' || status === 'cancelled' ? '重新生成交付文件' : '生成交付文件'}</button>`;
  }
  return `<div class="burned-video-export">
    <div><b>黑框白字2 MP4</b><small>字幕可单独下载</small></div>
    ${body}
  </div>`;
}

function renderDeliveryFiles(job) {
  const name = job.artifacts?.['final.reviewed.zh.srt'] ? 'final.reviewed.zh.srt'
    : job.artifacts?.['final.zh.srt'] ? 'final.zh.srt' : '';
  return `${name ? `<a class="artifact" href="/api/jobs/${job.id}/artifacts/${name}" download>${name === 'final.reviewed.zh.srt' ? '已审校 SRT' : '中文字幕 SRT'}</a>` : ''}
    <button type="button" class="open-job-folder" data-job="${escapeHtml(job.id)}">打开任务目录</button>`;
}

function bindDeliveryInteractions(root) {
  root.querySelector('.generate-burned-video')?.addEventListener('click', requestBurnedVideo);
  root.querySelector('.cancel-burned-video')?.addEventListener('click', cancelBurnedVideo);
  root.querySelector('.open-job-folder')?.addEventListener('click', openJobFolder);
}

function renderReviewDelivery() {
  const root = $('#review-delivery');
  if (!root) return;
  const job = selectedJob;
  const ready = job?.status === 'completed' && !job.options?.dry_run
    && risksLoadedJobId === job.id && unresolvedReviewRequiredCount(job) === 0
    && job.burn_review_ready !== false;
  root.hidden = !ready;
  if (!ready) { root.innerHTML = ''; return; }
  const markup = `<h3>审校已完成</h3>${renderBurnedVideoExport(job, false)}
    <div class="delivery-files">${renderDeliveryFiles(job)}</div>`;
  if (root.innerHTML === markup) return;
  root.innerHTML = markup;
  bindDeliveryInteractions(root);
}

function buildDetailContext(job) {
  const completedDryRun = job.status === 'completed' && Boolean(job.options?.dry_run);
  const logs = (job.logs || []).map(log => `<div class="${log.kind === 'error' ? 'error' : ''}">[${escapeHtml((log.at || '').slice(11, 19))}] ${escapeHtml(displayLogMessage(log.message))}</div>`).join('');
  const artifactNames = Object.keys(job.artifacts || {});
  const reviewedArtifact = !completedDryRun && artifactNames.includes('final.reviewed.zh.srt')
    ? 'final.reviewed.zh.srt'
    : '';
  const automaticArtifact = !completedDryRun && artifactNames.includes('final.zh.srt')
    ? 'final.zh.srt'
    : '';
  const preferredArtifact = reviewedArtifact || automaticArtifact;
  const unresolvedReviewCount = unresolvedReviewRequiredCount(job);
  const deliveryBlocked = job.status === 'completed' && !completedDryRun && unresolvedReviewCount > 0;
  const overviewLinkedArtifact = deliveryBlocked ? automaticArtifact : preferredArtifact;
  const technicalArtifactNames = artifactNames.filter(name => !isDeliveryArtifact(name));
  const technicalArtifacts = technicalArtifactNames.map(name => {
    const label = ARTIFACT_LABELS[name] || name;
    return `<a class="artifact" href="/api/jobs/${job.id}/artifacts/${encodeURIComponent(name)}" download title="${escapeHtml(name)}">${escapeHtml(label)}</a>`;
  }).join('');
  const deliveryArtifactNames = artifactNames.filter(name => isDeliveryArtifact(name)
    && name !== overviewLinkedArtifact
    && name !== 'final.zh.burned.mp4'
    && (!completedDryRun || !['final.zh.srt', 'final.reviewed.zh.srt'].includes(name)));
  const deliveryArtifacts = deliveryArtifactNames.map(name => {
    const label = ARTIFACT_LABELS[name] || name;
    return `<a class="artifact" href="/api/jobs/${job.id}/artifacts/${encodeURIComponent(name)}" download title="${escapeHtml(name)}">${escapeHtml(label)}</a>`;
  }).join('');
  const primaryDownload = deliveryBlocked
    ? `<button type="button" class="final-download review-gate open-review">处理 ${metricNumber(unresolvedReviewCount)} 条必审风险 <span aria-hidden="true">→</span></button>
      ${automaticArtifact ? `<a class="artifact automatic-download" href="/api/jobs/${job.id}/artifacts/${encodeURIComponent(automaticArtifact)}" download>下载自动版字幕（尚未完成审校）</a>` : ''}`
    : preferredArtifact
    ? `<a class="final-download" href="/api/jobs/${job.id}/artifacts/${encodeURIComponent(preferredArtifact)}" download>${reviewedArtifact ? '下载已审校中文字幕' : '下载可交付中文字幕'} <span aria-hidden="true">↓</span></a>`
    : '<span class="artifact-empty">最终字幕完成后会出现在这里</span>';
  const burnedVideoExport = renderBurnedVideoExport(job, deliveryBlocked);
  const progressView = renderProgressView(job);
  const liveStatus = renderLiveStatus(job, progressView);
  const presentation = taskPresentation(job);
  const activeStep = workflowPosition(job);
  const workflow = renderWorkflowView(job);

  let action = "";
  if (job.status === "waiting_capcut") {
    action = `${renderTranslationSettings(job)}
      ${renderOptionalCapcutSource(job)}
      <button type="button" class="run-button continue-automatic-source" data-job="${job.id}"><span class="button-label">直接使用自动字幕继续</span><span aria-hidden="true">→</span></button>
      <p class="form-note" id="translate-note" aria-live="polite"></p>`;
  } else if (job.status === "ready_for_translation") {
    action = `${renderTranslationSettings(job)}
      ${renderOptionalCapcutSource(job)}
      <button type="button" class="run-button start-translation" data-job="${job.id}"><span class="button-label">开始翻译</span><span aria-hidden="true">→</span></button>
      <p class="form-note" id="translate-note" aria-live="polite"></p>`;
  }

  let jobActions = '';
  if (['running', 'queued'].includes(job.status)) {
    jobActions = `<div class="job-actions"><button type="button" class="pause-job" data-job="${job.id}">暂停任务</button><button type="button" class="cancel-job" data-job="${job.id}">停止并保留文件</button><button type="button" class="danger delete-job" data-job="${job.id}">停止并删除任务</button><button type="button" class="danger force-reset-job" data-job="${job.id}">任务卡住时强制停止</button></div>`;
  } else if (['waiting_capcut', 'ready_for_translation'].includes(job.status)) {
    jobActions = `<div class="job-actions"><button type="button" class="pause-job" data-job="${job.id}">暂停任务</button><button type="button" class="danger delete-job" data-job="${job.id}">删除任务</button></div>`;
  } else if (job.status === 'paused') {
    jobActions = `<div class="job-actions"><button type="button" class="resume-job" data-job="${job.id}">继续任务</button><button type="button" class="danger delete-job" data-job="${job.id}">删除任务</button></div>`;
  } else if (job.status === 'failed') {
    const downloadRetry = job.url && !(job.inputs?.primary_srt || job.inputs?.capcut_en);
    const needsYoutubeAuth = downloadRetry && isYoutubeAuthFailure(job);
    const retryButtons = needsYoutubeAuth
      ? `<button type="button" class="open-youtube-recovery" aria-expanded="false">更换 Cookie 并重试</button>
         <button type="button" class="retry-job" data-job="${job.id}" data-restart="false">直接重试</button>`
      : downloadRetry
      ? `<button type="button" class="retry-job" data-job="${job.id}" data-restart="false">重试下载</button>`
      : `<button type="button" class="retry-job" data-job="${job.id}" data-restart="false">继续失败批次</button>
         <button type="button" class="restart-job" data-job="${job.id}" data-restart="true">从头重跑</button>`;
    const retryConfig = downloadRetry ? '' : renderTranslationSettings(job);
    jobActions = `${renderErrorBanner(job)}${retryConfig}${renderYoutubeRecovery(job)}${renderOptionalCapcutSource(job)}<div class="job-actions">${retryButtons} <button type="button" class="danger delete-job" data-job="${job.id}">删除任务</button></div>`;
  } else if (completedDryRun) {
    jobActions = `${renderTranslationSettings(job)}
      <div class="job-actions">
        <button type="button" class="primary-action restart-job" data-job="${job.id}" data-restart="true">开始正式翻译</button>
        <button type="button" class="danger delete-job" data-job="${job.id}">删除任务</button>
      </div>
      <p class="form-note" id="translate-note" aria-live="polite">补齐配置后会复用现有视频和源语言字幕，不需要重新下载。</p>`;
  } else if (job.status === 'completed') {
    jobActions = `${renderOptionalCapcutSource(job)}<div class="job-actions"><button type="button" class="${deliveryBlocked ? 'primary-action ' : ''}open-review">${deliveryBlocked ? `处理 ${metricNumber(unresolvedReviewCount)} 条必审风险` : '查看风险审校'}</button><button type="button" class="archive-job">${job.archived ? '取消归档' : '归档任务'}</button><button type="button" class="danger delete-job" data-job="${job.id}">删除任务</button></div>`;
  } else if (job.status === 'cancelled') {
    jobActions = `<div class="job-actions"><button type="button" class="danger delete-job" data-job="${job.id}">删除任务</button></div>`;
  }

  const configLine = `<dl class="job-config">
    <div><dt>当前模型</dt><dd>${escapeHtml(job.options?.model || userSettings.model || '尚未配置')}</dd></div>
    <div><dt>${job.url ? '视频清晰度' : '任务代次'}</dt><dd>${job.url ? escapeHtml(job.options?.video_quality === 'best' ? '最高可用' : `${job.options?.video_quality || '1080'}p`) : escapeHtml(job.generation ?? 0)}</dd></div>
  </dl>`;
  const metricsView = renderMetrics(job);
  const deliveryView = job.status === 'completed' && !completedDryRun ? `<section class="delivery-check" aria-labelledby="delivery-check-title">
    <div><p class="eyebrow">DELIVERY CHECK</p><h3 id="delivery-check-title">交付检查</h3></div>
    <ul>
      <li class="${automaticArtifact ? 'pass' : 'wait'}"><span aria-hidden="true">${automaticArtifact ? '✓' : '○'}</span>${automaticArtifact ? '自动字幕已生成' : '等待自动字幕'}</li>
      <li class="pass"><span aria-hidden="true">✓</span>Round 1 / Round 2 阶段产物已保留</li>
      <li id="delivery-risk-status" class="${deliveryBlocked ? 'wait' : 'pass'}"><span aria-hidden="true">${deliveryBlocked ? '!' : '✓'}</span>${deliveryBlocked ? `还有 ${metricNumber(unresolvedReviewCount)} 条必须确认，暂不能作为交付成品` : '必须处理的风险已经清零，可以交付'}</li>
    </ul>
  </section>` : '';
  const jobSourceLanguage = job.options?.source_language || 'en';
  const jobSourceLabel = sourceLanguageLabel(jobSourceLanguage);
  let statusHint = `<p class="hint">中文只由当前配置的模型生成；剪映与 Whisper 只提供${jobSourceLabel}证据。</p>`;
  if (isYoutubeAuthFailure(job)) {
    statusHint = `<p class="hint error-hint"><b>YouTube 要求确认登录：</b>当前网络出口已被限流，或者访客请求被判定为机器人。点击下方“更换 Cookie 并重试”，直接在本任务内更新认证。</p>`;
  } else if (job.error) {
    statusHint = `<p class="hint error-hint"><b>处理失败：</b>${escapeHtml(job.error)}<br>检查上方配置后选择继续失败批次；若修改批次大小等关键参数，请从头重跑。</p>`;
  } else if (completedDryRun) {
    statusHint = `<p class="hint warning-hint"><b>这不是翻译结果：</b>上次任务开启了试运行，没有调用翻译模型，所以字幕里只有“[待翻译]”占位文字。请在下方输入 API Key，然后点击“开始正式翻译”。</p>`;
  } else if (deliveryBlocked) {
    statusHint = `<p class="hint warning-hint"><b>还不能作为交付成品：</b>自动字幕已经生成，但还有 ${metricNumber(unresolvedReviewCount)} 条必须确认。先点击“处理必审风险”，确认后再下载可交付字幕；如需先查看自动结果，可下载“自动版字幕”。</p>`;
  } else if (job.status === 'completed') {
    statusHint = `<p class="hint success-hint"><b>菲比检查完毕！</b>必须处理的风险已经清零，现在可以下载可交付字幕。</p>`;
  } else if (job.status === 'waiting_capcut') {
    statusHint = `<p class="hint">这是旧版等待任务；可直接使用已下载的自动字幕继续，也可补充剪映字幕。</p>`;
  } else if (job.status === 'ready_for_translation') {
    statusHint = `<p class="hint">素材已就绪，确认当前模型后即可开始翻译。</p>`;
  }
  const logOpen = appSettings.log_visibility === 'open'
    || (appSettings.log_visibility === 'auto' && ['running', 'queued', 'failed'].includes(job.status))
    ? 'open'
    : '';
  return {
    job,
    completedDryRun,
    logs,
    artifactNames,
    overviewLinkedArtifact,
    deliveryArtifacts,
    technicalArtifactNames,
    technicalArtifacts,
    primaryDownload,
    burnedVideoExport,
    liveStatus,
    presentation,
    activeStep,
    workflow,
    action,
    jobActions,
    configLine,
    metricsView,
    deliveryView,
    statusHint,
    logOpen,
  };
}

function bindOverviewTabInteractions(root) {
  root.querySelector(".capcut-form")?.addEventListener("submit", uploadCapcut);
  root.querySelector(".open-capcut-app")?.addEventListener("click", openCapcutApp);
  root.querySelector(".optional-capcut-toggle")?.addEventListener("change", event => {
    const controls = event.currentTarget.closest(".optional-capcut-source")
      ?.querySelector(".optional-capcut-controls");
    if (controls) controls.hidden = !event.currentTarget.checked;
  });
  root.querySelector(".edit-translation-config")?.addEventListener("click", event => {
    const slot = event.currentTarget.closest(".translation-settings")
      ?.querySelector(".translation-config-slot");
    if (slot && !slot.firstElementChild) slot.innerHTML = renderTranslationConfigRepair(selectedJob);
    slot?.querySelector('[name="model"]')?.focus();
  });
  root.querySelector(".continue-automatic-source")?.addEventListener("click", continueWithAutomaticSource);
  root.querySelector(".start-translation")?.addEventListener("click", startTranslation);
  bindDeliveryInteractions(root);
  root.querySelector('.cancel-job')?.addEventListener('click', cancelJob);
  root.querySelector('.pause-job')?.addEventListener('click', pauseJob);
  root.querySelector('.resume-job')?.addEventListener('click', resumeJob);
  root.querySelector('.delete-job')?.addEventListener('click', deleteJob);
  root.querySelector('.retry-job')?.addEventListener('click', retryJob);
  root.querySelector('.restart-job')?.addEventListener('click', retryJob);
  root.querySelector('.open-youtube-recovery')?.addEventListener('click', openYoutubeRecovery);
  root.querySelector('.youtube-recovery-form')?.addEventListener('submit', retryDownloadWithAuth);
  root.querySelector('#retry-youtube-auth')?.addEventListener('change', syncRetryYoutubeAuth);
  root.querySelector('.paste-youtube-cookies')?.addEventListener('click', pasteYoutubeCookies);
  root.querySelector('.force-reset-job')?.addEventListener('click', forceResetJob);
  root.querySelectorAll('.open-review').forEach(button => {
    button.addEventListener('click', () => activateView('review'));
  });
  root.querySelector('.rename-job')?.addEventListener('click', renameSelectedJob);
  root.querySelector('.archive-job')?.addEventListener('click', archiveSelectedJob);
}

function bindPipelineTabInteractions() {}

function bindLogTabInteractions(root) {
  const panel = root.querySelector('.log-panel');
  panel?.addEventListener('toggle', () => {
    if (panel.open) requestAnimationFrame(scrollDetailLogToBottom);
  });
  requestAnimationFrame(scrollDetailLogToBottom);
}

function bindFilesTabInteractions(root) {
  root.querySelector('.open-downloads')?.addEventListener('click', openDownloadsFolder);
  root.querySelector('.organize-technical')?.addEventListener('click', organizeSelectedJob);
}

function renderOverviewTab(context) {
  const { job, presentation } = context;
  const panel = $('#detail-panel-overview');
  panel.innerHTML = `<div class="detail-heading">
    <div>
      <p class="eyebrow task-kicker">TASK / ${escapeHtml(jobTitle(job))} / ${escapeHtml(job.id)}</p>
      <h2 class="stage" data-overview-stage>${escapeHtml(stageLabel(job))}</h2>
      <div class="task-rename"><input id="task-custom-name" name="task_custom_name" aria-label="自定义任务名称" autocomplete="off" value="${escapeHtml(job.custom_name || '')}" placeholder="给任务起个容易识别的名字…"><button type="button" class="rename-job">保存名称</button></div>
    </div>
    ${displayStatus(job.status)}
  </div>
  ${context.workflow}
  <section class="overview-phoebe-status" aria-label="菲比任务状态">
    <span data-overview-phoebe-icon aria-hidden="true">${escapeHtml(presentation.icon)}</span>
    <div><p class="eyebrow">PHOEBE STATUS</p><b data-overview-phoebe-title>${escapeHtml(presentation.title)}</b><small data-overview-phoebe-message>${escapeHtml(presentation.message)}</small></div>
  </section>
  ${context.statusHint}
  ${context.action}
  ${context.jobActions}
  <p class="form-note" id="job-action-note" aria-live="polite"></p>
  <section class="overview-download" aria-labelledby="overview-download-title">
    <div class="section-title"><h3 id="overview-download-title">最终交付</h3><small>主字幕下载</small></div>
    ${context.primaryDownload}
    ${context.burnedVideoExport}
    ${job.status === 'completed' ? `<button type="button" class="open-job-folder" data-job="${escapeHtml(job.id)}">打开任务目录</button>` : ''}
  </section>`;
  bindOverviewTabInteractions(panel);
}

function renderPipelineTab(context) {
  const { job, activeStep } = context;
  const activeFlowLabel = FLOW_STEPS[Math.max(0, Math.min(FLOW_STEPS.length - 1, activeStep - 1))];
  const panel = $('#detail-panel-pipeline');
  panel.innerHTML = `<div class="pipeline-stage-summary" aria-label="当前 Pipeline 阶段">
    <div><span>CURRENT STAGE</span><b data-pipeline-stage>${escapeHtml(job.stage || stageLabel(job))}</b><small data-pipeline-stage-label>${escapeHtml(stageLabel(job))}</small></div>
    <div><span>WORKFLOW POSITION</span><b data-pipeline-flow-label>${escapeHtml(activeFlowLabel)}</b><small data-pipeline-flow-position>${activeStep} / ${FLOW_STEPS.length}</small></div>
  </div>
  ${context.liveStatus}
  ${context.configLine}
  ${context.metricsView}
  ${context.deliveryView}`;
  bindPipelineTabInteractions(panel);
}

function renderLogTab(context) {
  const panel = $('#detail-panel-log');
  panel.innerHTML = `<div class="detail-log-heading"><div><p class="eyebrow">TECHNICAL LOG</p><h2>任务实时日志</h2></div><small>${(context.job.logs || []).length} 条事件</small></div>
    <details class="log-panel" ${context.logOpen}>
      <summary>技术日志 <span>${(context.job.logs || []).length} 条事件</span></summary>
      <div class="logs log-scroll" aria-live="polite" aria-label="任务实时日志">${context.logs || '<div>等待任务事件…</div>'}</div>
    </details>`;
  bindLogTabInteractions(panel);
  trackLogScroll(panel.querySelector('.log-scroll'));
  if (logAutoFollow) scrollDetailLogToBottom();
}

function renderFilesTab(context) {
  const { job } = context;
  const preferredReference = context.overviewLinkedArtifact
    ? `<div class="file-primary-reference"><b>${escapeHtml(ARTIFACT_LABELS[context.overviewLinkedArtifact] || context.overviewLinkedArtifact)}</b><span>主下载位于 Overview，避免重复链接</span></div>`
    : '';
  const panel = $('#detail-panel-files');
  panel.innerHTML = `<section class="artifact-section" aria-labelledby="artifact-title">
    <div class="section-title"><h3 id="artifact-title">下载与存储</h3><small>${formatBytes(job.disk_bytes)} · ${context.artifactNames.length} 个文件</small></div>
    ${preferredReference}
    <div class="artifact-list">${context.deliveryArtifacts || '<span class="artifact-empty">没有其他交付文件</span>'}</div>
    <div class="download-actions">
      <button type="button" class="open-downloads">打开 Download 文件夹</button>
      ${job.status === 'completed' && !context.completedDryRun ? '<button type="button" class="organize-technical">整理过程文件</button>' : ''}
    </div>
    <p class="form-note">只保留最终字幕、已审校字幕、视频和封面在当前目录；英文来源、Whisper、Round 1/2、断点、风险记录、配置和证据目录会收进“过程文件”，不会删除。</p>
    <details class="technical-artifacts"><summary>过程文件 <span>${context.technicalArtifactNames.length} 个${job.technical_files_folder ? ' · 已整理' : ''}</span></summary>
      <div class="artifact-list">${context.technicalArtifacts || '<span class="artifact-empty">没有技术产物</span>'}</div>
    </details>
  </section>`;
  bindFilesTabInteractions(panel);
}

/* FE-P0-02：筛选与分页只有一套状态（#risk-filter + riskPage），
   详情 Risk 标签与独立 Review 视图共用；切换表面不再丢上下文。 */
function currentRiskFilter() {
  return $('#risk-filter')?.value || 'unresolved';
}

function applyRiskFilter(items) {
  const filter = currentRiskFilter();
  if (filter === 'unresolved') {
    return items.filter(item => ['pending', 'review', 'failed'].includes(item.state));
  }
  if (filter) return items.filter(item => item.state === filter);
  return items;
}

function detailRiskItems() {
  return applyRiskFilter(allRiskItems);
}

function bindDetailRiskTabInteractions(root) {
  const filter = root.querySelector('[data-detail-risk-filter]');
  filter?.addEventListener('change', async () => {
    if (!await confirmDiscardRiskEdit()) {
      filter.value = currentRiskFilter();
      return;
    }
    riskDirty = false;
    /* 写回唯一状态源（review 侧的 select），两个表面同步重渲染 */
    const shared = $('#risk-filter');
    if (shared) shared.value = filter.value;
    riskPage = 1;
    riskItems = applyRiskFilter(allRiskItems);
    renderRiskPage();
    renderDetailRiskTab();
    syncUrl({ riskFilter: currentRiskFilter(), page: riskPage });
  });
  root.querySelectorAll('[data-detail-risk-refresh], [data-detail-risk-retry]').forEach(button => {
    button.addEventListener('click', async () => {
      if (!await confirmDiscardRiskEdit()) return;
      riskDirty = false;
      loadRisks({ preservePage: true });
    });
  });
  root.querySelector('[data-open-review-view]')?.addEventListener('click', () => activateView('review'));
  root.querySelector('[data-detail-risk-prev]')?.addEventListener('click', async () => {
    if (!await confirmDiscardRiskEdit()) return;
    riskDirty = false;
    riskPage = Math.max(1, riskPage - 1);
    renderRiskPage();
    renderDetailRiskTab();
  });
  root.querySelector('[data-detail-risk-next]')?.addEventListener('click', async () => {
    if (!await confirmDiscardRiskEdit()) return;
    riskDirty = false;
    riskPage += 1;
    renderRiskPage();
    renderDetailRiskTab();
  });
  const list = root.querySelector('[data-detail-risk-list]');
  if (list) {
    bindRiskCards(list);
    list.addEventListener('keydown', event => {
      const card = list.querySelector('.risk.is-active') || list.querySelector('.risk');
      if (!card) return;
      const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
      if (event.key === 'Enter' && document.activeElement?.tagName === 'TEXTAREA' && !event.shiftKey) {
        event.preventDefault();
        saveRiskCard(card, 'resolved');
      } else if (!typing && event.key === '1') {
        event.preventDefault();
        saveRiskCard(card, 'verified');
      } else if (!typing && event.key === '2' && card.querySelector('[data-use-round2="1"]')) {
        event.preventDefault();
        saveRiskCard(card, 'resolved', true);
      }
    });
  }
}

function renderDetailRiskTab({ error = '' } = {}) {
  const panel = $('#detail-panel-risk');
  if (!panel || !selectedJob) return;
  if (riskDirty && panel.querySelector('.risk')) return;
  if (error) {
    panel.innerHTML = `<div class="detail-risk-heading"><div><p class="eyebrow">RISK QUEUE</p><h2>风险审校</h2></div></div>
      <div class="empty error-state"><span class="empty-mark" aria-hidden="true">!</span><b>审校队列读取失败</b><small>${escapeHtml(error)}</small><button type="button" data-detail-risk-retry>重新读取审校队列</button></div>`;
    bindDetailRiskTabInteractions(panel);
    return;
  }
  if (risksLoadedJobId !== selectedJob.id) {
    panel.innerHTML = `<div class="detail-risk-heading"><div><p class="eyebrow">RISK QUEUE</p><h2>风险审校</h2></div></div>
      <div class="loading-state" role="status"><span class="loading-orbit" aria-hidden="true"></span><b>正在读取审校队列…</b><small>与独立 Risk Review 使用同一任务数据</small></div>`;
    bindDetailRiskTabInteractions(panel);
    return;
  }

  const items = detailRiskItems();
  const pageCount = Math.max(1, Math.ceil(items.length / RISK_PAGE_SIZE));
  riskPage = Math.min(Math.max(1, riskPage), pageCount);
  const startIndex = (riskPage - 1) * RISK_PAGE_SIZE;
  const visible = items.slice(startIndex, startIndex + RISK_PAGE_SIZE);
  const unresolved = allRiskItems.filter(item => item.review_required && ['pending', 'review', 'failed'].includes(item.state)).length;
  const automatic = allRiskItems.filter(item => item.state === 'auto_resolved').length;
  const advisory = allRiskItems.filter(item => item.state === 'advisory').length;
  const humanDone = allRiskItems.filter(item => ['resolved', 'verified'].includes(item.state)).length;
  const option = (value, label) => `<option value="${value}" ${currentRiskFilter() === value ? 'selected' : ''}>${label}</option>`;
  const cards = visible.length
    ? visible.map(item => renderRiskCardMarkup(item, 'detail-risk')).join('')
    : `<div class="empty"><span class="empty-mark" aria-hidden="true">✓</span><b>${currentRiskFilter() === 'unresolved' ? '没有待处理风险' : '没有符合筛选条件的风险项'}</b><small>${currentRiskFilter() === 'unresolved' ? '可以继续检查交付文件' : '尝试切换其他状态'}</small></div>`;

  panel.innerHTML = `<div class="detail-risk-heading"><div><p class="eyebrow">RISK QUEUE</p><h2>风险审校</h2></div><small>与独立 Review 视图同步</small></div>
    <div class="detail-risk-toolbar">
      <label>风险状态<select data-detail-risk-filter autocomplete="off">
        ${option('unresolved', '待我处理')}${option('review', '待人工复核')}${option('pending', '待处理')}${option('resolved', '已修正')}${option('verified', '已确认')}${option('auto_resolved', '自动处理完成')}${option('advisory', '仅供参考')}${option('', '全部风险')}
      </select></label>
      <div><button type="button" class="icon-button" data-detail-risk-refresh aria-label="刷新风险">↻</button> <button type="button" data-open-review-view>打开独立审校视图</button></div>
    </div>
    <div class="detail-risk-summary" aria-label="风险摘要">
      <div><span>真正待处理</span><b>${metricNumber(unresolved)}</b></div><div><span>已自动处理</span><b>${metricNumber(automatic)}</b></div>
      <div><span>仅供参考</span><b>${metricNumber(advisory)}</b></div><div><span>已人工完成</span><b>${metricNumber(humanDone)}</b></div>
    </div>
    <div class="risk-list detail-risk-list" data-detail-risk-list aria-live="polite">${cards}</div>
    <nav class="detail-risk-pagination" aria-label="详情风险分页" ${pageCount <= 1 ? 'hidden' : ''}>
      <button type="button" data-detail-risk-prev ${riskPage <= 1 ? 'disabled' : ''}>上一页</button>
      <span>${riskPage} / ${pageCount} 页 · 共 ${items.length} 条</span>
      <button type="button" data-detail-risk-next ${riskPage >= pageCount ? 'disabled' : ''}>下一页</button>
    </nav>`;
  bindDetailRiskTabInteractions(panel);
}

function renderDetailEmpty(message = '') {
  const copy = message ? escapeHtml(message) : '点一条任务，菲比继续';
  const descriptions = {
    overview: '这里会显示进度、待办操作、产物与技术日志',
    pipeline: '选择任务后查看 Pipeline',
    log: '选择任务后查看 Log',
    files: '选择任务后查看 Files',
    risk: '选择任务后查看 Risk',
  };
  DETAIL_TAB_NAMES.forEach(tabName => {
    const panel = document.getElementById(`detail-panel-${tabName}`);
    if (!panel) return;
    panel.innerHTML = `<div class="empty detail-empty">${tabName === 'overview' ? '<span class="empty-mark" aria-hidden="true">⌁</span>' : ''}<b>${copy}</b><small>${descriptions[tabName]}</small></div>`;
  });
  restoreDetailTabState();
}

function renderDetail() {
  renderReviewDelivery();
  if (!selectedJob) {
    renderDetailEmpty();
    return;
  }
  updatePhoebeAssistant(selectedJob);
  const context = buildDetailContext(selectedJob);
  renderOverviewTab(context);
  renderPipelineTab(context);
  renderLogTab(context);
  renderFilesTab(context);
  if (!riskDirty || !$('#detail-panel-risk')?.querySelector('.risk')) renderDetailRiskTab();
  restoreDetailTabState();
}

function metricNumber(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? new Intl.NumberFormat('zh-CN').format(number) : '—';
}

function metricPercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : '—';
}

function metricSeconds(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return '—';
  return number >= 60 ? `${(number / 60).toFixed(1)} 分` : `${number.toFixed(1)} 秒`;
}

function renderMetrics(job) {
  const metrics = job.metrics || {};
  const round1 = metrics.round1 || {};
  const round2 = metrics.round2 || {};
  const quality = metrics.quality || {};
  const sources = metrics.sources || job.source_qualities || {};
  const hasMetrics = Object.keys(round1).length || Object.keys(round2).length
    || Object.keys(quality).length || Object.keys(sources).length;
  if (!hasMetrics) return '';

  const pricingView = data => {
    const pricing = data.pricing || {};
    if (!pricing.estimable) {
      return '<span class="metric-cost-unavailable" title="目前只按 DeepSeek 官方模型价格估算">暂不估算</span>';
    }
    const amount = Number(pricing.amount || 0);
    const detail = `模型：${pricing.model || data.model || '未识别模型'}；输入 ¥${pricing.input_rate}/百万 Token，输出 ¥${pricing.output_rate}/百万 Token；按缓存未命中价估算，中转站实际账单可能不同`;
    return `<span class="metric-cost" title="${escapeHtml(detail)}">¥${amount.toFixed(amount < 0.01 ? 6 : 4)}</span>`;
  };

  const stageCard = (label, data, accent) => `<article class="metric-stage ${accent}">
    <div class="metric-stage-head"><span>${label}</span><b>${metricNumber(data.successful_batches)} / ${metricNumber(data.batch_count)} 批</b></div>
    <div class="metric-token"><strong>${metricNumber(data.tokens_in)}</strong><span>IN</span><i>→</i><strong>${metricNumber(data.tokens_out)}</strong><span>OUT</span></div>
    <dl>
      <div><dt>墙钟耗时</dt><dd>${metricSeconds(data.wall_seconds || data.elapsed_seconds)}</dd></div>
      <div><dt>API 尝试</dt><dd>${metricNumber(data.response_attempts || data.attempts)}</dd></div>
      <div><dt>平均术语</dt><dd>${Number(data.average_glossary_terms || 0).toFixed(1)}</dd></div>
      <div><dt>官方价估算</dt><dd>${pricingView(data)}</dd></div>
    </dl>
  </article>`;

  const sourceRows = Object.entries(sources).map(([name, source]) => {
    const label = { capcut: 'CapCut', youtube: 'YouTube', whisper: 'Whisper' }[name] || name;
    const score = Number(source?.score || 0);
    return `<div class="source-score">
      <span>${escapeHtml(label)}</span>
      <div class="source-score-track"><i style="width:${Math.max(0, Math.min(100, score))}%"></i></div>
      <b>${metricNumber(score)}</b>
    </div>`;
  }).join('');
  const stageCards = [
    Object.keys(round1).length ? stageCard('ROUND 1 / 全量翻译', round1, 'metric-round1') : '',
    Object.keys(round2).length ? stageCard('ROUND 2 / 风险精校', round2, 'metric-round2') : '',
  ].filter(Boolean).join('');
  const qualityView = Object.keys(quality).length ? `<div class="quality-strip">
    <div><span>双源匹配率</span><b>${metricPercent(quality.dual_source_match_rate)}</b></div>
    <div><span>需处理风险率</span><b>${metricPercent(quality.review_required_rate)}</b></div>
    <div><span>算法提示率</span><b>${metricPercent(quality.risk_rate)}</b></div>
    <div><span>音频复核率</span><b>${metricPercent(quality.audio_evidence_rate)}</b></div>
    <div><span>Union 字幕</span><b>${metricNumber(quality.union_subtitles)}</b></div>
  </div>` : '';

  return `<section class="metrics-board" aria-labelledby="metrics-title">
    <div class="section-title metric-title"><h3 id="metrics-title">成本与质量仪表</h3><small>每次完成后固化</small></div>
    ${stageCards ? `<div class="metric-stage-grid">${stageCards}</div>` : ''}
    ${qualityView}
    ${sourceRows ? `<div class="source-scores"><span class="source-scores-label">英文源质量</span>${sourceRows}</div>` : ''}
  </section>`;
}

/* ---- 风险审校 ---- */

const RISK_STATE_LABELS = {
  pending: '待处理', review: '待人工复核', resolved: '已修正', verified: '已确认', failed: '处理失败',
  on_demand: '按需生成', auto_resolved: '自动处理完成', advisory: '仅供参考',
};

const TRIAGE_REASON_LABELS = {
  whitelisted_entity: '已知外部名，沿用译文',
  source_conflict_en: '转写冲突，已按剪映源接受',
  advisory_only: '仅提示级信号',
};

function riskReasonLabel(reason) {
  return RISK_REASON_LABELS[reason] || String(reason || '').replaceAll('_', ' ');
}

function diffTokens(left = '', right = '') {
  const a = Array.from(String(left));
  const b = Array.from(String(right));
  const table = Array.from({ length: a.length + 1 }, () => new Uint16Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const leftParts = [];
  const rightParts = [];
  let i = 0;
  let j = 0;
  const push = (parts, value, changed) => {
    if (!value) return;
    const last = parts.at(-1);
    if (last && last.changed === changed) last.value += value;
    else parts.push({ value, changed });
  };
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      push(leftParts, a[i], false); push(rightParts, b[j], false); i += 1; j += 1;
    } else if (j < b.length && (i === a.length || table[i][j + 1] >= table[i + 1][j])) {
      push(rightParts, b[j], true); j += 1;
    } else {
      push(leftParts, a[i], true); i += 1;
    }
  }
  const render = parts => parts.map(part => part.changed
    ? `<mark>${escapeHtml(part.value)}</mark>`
    : escapeHtml(part.value)).join('');
  return { left: render(leftParts), right: render(rightParts) };
}

async function confirmDiscardRiskEdit() {
  if (!riskDirty || riskSaving) return true;
  return confirmAction({
    title: '放弃未保存的字幕修改？',
    message: '当前文本框的人工编辑尚未保存。离开后这些修改无法恢复。',
    confirmLabel: '放弃修改',
    danger: true,
  });
}

function setActiveRisk(card) {
  if (!card) return;
  const surface = card.closest('.risk-list') || document;
  const surfaceKey = surface.matches?.('[data-detail-risk-list]') ? 'detail' : 'review';
  surface.querySelectorAll('.risk.is-active').forEach(item => {
    item.classList.remove('is-active');
    item.removeAttribute('aria-current');
  });
  card.classList.add('is-active');
  card.setAttribute('aria-current', 'true');
  activeRiskKeys[surfaceKey] = card.dataset.key || '';
}

async function saveRiskCard(card, action, useRound2 = false) {
  if (!card || riskSaving) return false;
  const item = allRiskItems.find(entry => entry.key === card.dataset.key)
    || riskItems.find(entry => entry.key === card.dataset.key);
  if (!item) return false;
  const jobId = risksLoadedJobId;
  if (!jobId || selectedJob?.id !== jobId) return false;
  const fromDetailTab = Boolean(card.closest('[data-detail-risk-list]'));
  const feedback = card.querySelector('.risk-note');
  const buttons = card.querySelectorAll('button');
  let submitText = card.querySelector('textarea').value.trim();
  const rememberForFuture = Boolean(
    card.querySelector('[data-memory-approve]')?.checked
  );
  const memoryErrorType = String(
    card.querySelector('[data-memory-error-type]')?.value || '其他'
  );
  if (action === 'verified') submitText = String(item.translated || '').trim();
  if (useRound2) submitText = String(item.round2_text || '').trim();
  if (useRound2 && !submitText) {
    feedback.textContent = 'Round 2 没有修正结果';
    return false;
  }
  riskSaving = true;
  buttons.forEach(button => { button.disabled = true; });
  feedback.textContent = '正在保存…';
  try {
    await api(`/api/jobs/${jobId}/risks/${encodeURIComponent(item.key)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        state: action,
        round2_text: submitText,
        remember_for_future: action === 'resolved' && rememberForFuture,
        memory_error_type: memoryErrorType,
      }),
    });
    if (selectedJob?.id !== jobId) return true;
    riskDirty = false;
    await refreshSelectedJob(jobId);
    if (selectedJob?.id !== jobId) return true;
    const actionLabel = action === 'verified' ? '已采用 Round 1 译文' : '已保存为最终译文';
    showToast(actionLabel);
    await loadRisks({ preservePage: true });
    const refreshedSurface = fromDetailTab ? $('[data-detail-risk-list]') : $('#risk-list');
    const refreshedCard = refreshedSurface?.querySelector(`.risk[data-key="${CSS.escape(item.key)}"]`)
      || refreshedSurface?.querySelector('.risk');
    refreshedCard?.querySelector('textarea')?.focus();
    return true;
  } catch (error) {
    feedback.textContent = error.message;
    showToast(error.message, 'error');
    buttons.forEach(button => { button.disabled = false; });
    return false;
  } finally {
    riskSaving = false;
  }
}

function renderRiskCardMarkup(item, idPrefix = 'risk') {
  const stateLabel = RISK_STATE_LABELS[item.state] || item.state;
  const reasons = (item.reasons || []).map(riskReasonLabel).join('、') || '需人工确认';
  const hasRound2 = Boolean(item.round2_text && String(item.round2_text).trim());
  const canGenerateRound2 = !hasRound2 && item.automatic_state === 'on_demand';
  const hasTranslated = Boolean(item.translated && String(item.translated).trim());
  const sourceLanguage = item.source_language || selectedJob?.options?.source_language || 'en';
  const sourceLabel = sourceLanguageLabel(sourceLanguage);
  const primaryEvidence = item.primary_evidence ?? item.capcut_en ?? '';
  const secondaryEvidence = item.secondary_evidence ?? item.whisper_en ?? '';
  const hasCapcut = Boolean(String(primaryEvidence).trim());
  const hasWhisper = Boolean(String(secondaryEvidence).trim());
  const initialText = item.manual_text || (hasTranslated ? item.translated : (hasRound2 ? item.round2_text : ''));
  const evidenceParts = [];
  if (hasCapcut) evidenceParts.push(`<div class="evidence-row"><span class="evidence-label">主要${sourceLabel}证据</span><span class="evidence-text">${escapeHtml(primaryEvidence)}</span></div>`);
  if (hasWhisper) evidenceParts.push(`<div class="evidence-row"><span class="evidence-label">辅助${sourceLabel}证据</span><span class="evidence-text">${escapeHtml(secondaryEvidence)}</span></div>`);
  if (hasTranslated && hasRound2) {
    const diff = diffTokens(item.translated, item.round2_text);
    evidenceParts.push(`<div class="evidence-row evidence-zh diff-before"><span class="evidence-label">Round 1 译文</span><span class="evidence-text">${diff.left}</span></div>`);
    evidenceParts.push(`<div class="evidence-row evidence-zh diff-after"><span class="evidence-label">Round 2 建议</span><span class="evidence-text">${diff.right}</span></div>`);
  } else if (hasTranslated) {
    evidenceParts.push(`<div class="evidence-row evidence-zh"><span class="evidence-label">Round 1 译文</span><span class="evidence-text">${escapeHtml(item.translated)}</span></div>`);
  }
  if (item.local_evidence_text) evidenceParts.push(`<div class="evidence-row local-evidence"><span class="evidence-label">局部 Whisper 复核</span><span class="evidence-text">${escapeHtml(item.local_evidence_text)}</span></div>`);
  const riskId = escapeHtml(item.key).replace(/[^a-zA-Z0-9_-]/g, '-');
  const editorId = `${idPrefix}-editor-${riskId}`;
  const titleId = `${idPrefix}-title-${riskId}`;
  return `<article class="risk risk-${escapeHtml(item.state)}" data-key="${escapeHtml(item.key)}"
    data-start="${escapeHtml(item.start)}" data-end="${escapeHtml(item.end)}" tabindex="0"
    aria-labelledby="${titleId}">
    <div class="risk-head">
      <div class="risk-title"><span class="risk-id" id="${titleId}">字幕 #${escapeHtml(item.subtitle_id || '—')}</span>
        <span class="risk-time">${escapeHtml(item.start)} → ${escapeHtml(item.end)}</span></div>
      <div class="risk-meta"><span class="risk-state risk-state-${escapeHtml(item.state)}">${escapeHtml(stateLabel)}</span>
        ${item.automatic_state === 'resolved' ? '<span class="auto-result">Round 2 已生成</span>' : ''}
        ${item.auto_resolved && item.auto_resolved_reason ? `<span class="auto-triage" title="自动放行依据">${escapeHtml(TRIAGE_REASON_LABELS[item.auto_resolved_reason] || item.auto_resolved_reason)}</span>` : ''}
        ${item.spot_check ? '<span class="spot-check" title="5% 抽检样本，请人工确认">抽检样本</span>' : ''}
        <span class="risk-reasons" title="风险原因">${escapeHtml(reasons)}</span></div>
    </div>
    <div class="evidence">${evidenceParts.join('')}</div>
    <div class="risk-edit"><label class="risk-edit-label" for="${editorId}">最终中文字幕</label>
      <textarea id="${editorId}" name="manual_text" autocomplete="off" placeholder="输入修正后的中文译文…">${escapeHtml(initialText)}</textarea></div>
    <div class="memory-controls">
      <label class="memory-approval"><input type="checkbox" data-memory-approve>
        <span>以后遇到完全相同的英文时，优先沿用这版</span></label>
      <label class="memory-error-label">修改类型
        <select data-memory-error-type>
          <option value="人名">人名</option><option value="术语">术语</option>
          <option value="漏译">漏译</option><option value="指代/上下文">指代/上下文</option>
          <option value="语气/表达">语气/表达</option><option value="其他" selected>其他</option>
        </select></label>
      <small>不勾选也会保存为个人修改记录，但不会影响以后任务。</small>
    </div>
    <div class="risk-actions">
      <button type="button" class="fix" data-action="resolved">保存（Enter）</button>
      <button type="button" data-action="verified" title="快捷键 1">用 Round 1</button>
      ${hasRound2 ? '<button type="button" data-action="resolved" data-use-round2="1" title="快捷键 2">用 Round 2</button>' : ''}
      ${canGenerateRound2 ? '<button type="button" data-generate-round2="1">生成 Round 2 建议</button>' : ''}
    </div><p class="risk-note" aria-live="polite"></p></article>`;
}

function bindRiskCards(target) {
  target.querySelectorAll('.risk').forEach(card => {
    card.addEventListener('click', () => setActiveRisk(card));
    card.addEventListener('keydown', event => {
      if (event.target !== card) return;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        setActiveRisk(card);
        card.querySelector('textarea')?.focus();
        return;
      }
      if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
      event.preventDefault();
      const cards = [...target.querySelectorAll('.risk')];
      const index = cards.indexOf(card);
      const next = cards[index + (event.key === 'ArrowDown' ? 1 : -1)];
      if (!next) return;
      setActiveRisk(next);
      next.focus();
    });
    card.querySelector('textarea')?.addEventListener('input', () => {
      setActiveRisk(card);
      riskDirty = true;
    });
    card.querySelectorAll('[data-memory-approve], [data-memory-error-type]').forEach(control => {
      control.addEventListener('change', () => {
        setActiveRisk(card);
        riskDirty = true;
      });
    });
  });
  target.querySelectorAll('.risk-actions button').forEach(button => {
    button.onclick = async () => {
      const card = button.closest('.risk');
      setActiveRisk(card);
      const item = allRiskItems.find(entry => entry.key === card.dataset.key)
        || riskItems.find(entry => entry.key === card.dataset.key);
      if (!item) return;
      if (button.dataset.generateRound2 === '1') {
        const payload = new FormData();
        try {
          await api(`/api/jobs/${selectedJob.id}/risks/${encodeURIComponent(item.key)}/round2`, { method: 'POST', body: payload });
          showToast('Round 2 建议已生成');
          await loadRisks({ preservePage: true });
        } catch (error) { showToast(error.message, 'error'); }
        return;
      }
      await saveRiskCard(card, button.dataset.action, button.dataset.useRound2 === '1');
    };
  });
  const surfaceKey = target.matches('[data-detail-risk-list]') ? 'detail' : 'review';
  const activeKey = activeRiskKeys[surfaceKey];
  const active = activeKey && target.querySelector(`.risk[data-key="${CSS.escape(activeKey)}"]`);
  setActiveRisk(active || target.querySelector('.risk'));
}

function renderRiskPage() {
  renderReviewDelivery();
  const target = $('#risk-list');
  const pageCount = Math.max(1, Math.ceil(riskItems.length / RISK_PAGE_SIZE));
  riskPage = Math.min(Math.max(1, riskPage), pageCount);
  const startIndex = (riskPage - 1) * RISK_PAGE_SIZE;
  const visible = riskItems.slice(startIndex, startIndex + RISK_PAGE_SIZE);
  const filter = $('#risk-filter').value;
  $('#bulk-recommended').disabled = !riskItems.length;
  $('#bulk-verify').disabled = !riskItems.length;
  if (!visible.length) {
    target.innerHTML = `<div class="empty"><span class="empty-mark" aria-hidden="true">✓</span>
      <b>${filter === 'unresolved' ? '没有待处理风险' : '没有符合筛选条件的风险项'}</b>
      <small>${filter === 'unresolved' ? '可以下载已审校中文字幕' : '尝试切换其他状态'}</small></div>`;
    $('#risk-pagination').hidden = true;
    return;
  }
  target.innerHTML = visible.map(item => renderRiskCardMarkup(item, 'risk')).join('');
  $('#risk-page-status').textContent = `${riskPage} / ${pageCount} 页 · 共 ${riskItems.length} 条`;
  $('#risk-prev').disabled = riskPage <= 1;
  $('#risk-next').disabled = riskPage >= pageCount;
  $('#risk-pagination').hidden = pageCount <= 1;
  syncUrl({
    riskFilter: $('#risk-filter')?.value || 'unresolved',
    page: riskPage,
  });
  bindRiskCards(target);
}

function reviewableJobs() {
  const jobs = allJobs.filter(job => job.status === 'completed' && !job.options?.dry_run);
  if (selectedJob?.status === 'completed' && !selectedJob.options?.dry_run
      && !jobs.some(job => job.id === selectedJob.id)) {
    jobs.unshift(selectedJob);
  }
  return jobs.sort((left, right) => String(right.completed_at || right.updated_at || '')
    .localeCompare(String(left.completed_at || left.updated_at || '')));
}

function renderReviewTaskContext() {
  const select = $('#review-task-select');
  const title = $('#review-task-title');
  const meta = $('#review-task-meta');
  const cover = $('#review-task-cover');
  if (!select || !title || !meta || !cover) return;

  const jobs = reviewableJobs();
  const current = selectedJob?.status === 'completed' && !selectedJob.options?.dry_run
    ? selectedJob
    : null;
  const options = jobs.map(job => `<option value="${escapeHtml(job.id)}">${escapeHtml(jobTitle(job))} · ${escapeHtml(formatDate(job.completed_at || job.updated_at))}</option>`).join('');
  select.disabled = jobs.length === 0;
  select.innerHTML = current
    ? options
    : `<option value="" selected disabled>${jobs.length ? '选择已完成任务…' : '暂无可审校的已完成任务'}</option>${options}`;
  if (current) select.value = current.id;

  const thumbnail = current?.artifacts?.thumbnail
    ? `/api/jobs/${current.id}/artifacts/thumbnail`
    : '';
  if (thumbnail) {
    const image = document.createElement('img');
    image.src = thumbnail;
    image.alt = '';
    image.width = 80;
    image.height = 45;
    cover.replaceChildren(image);
    cover.classList.add('has-cover');
  } else {
    cover.textContent = '审';
    cover.classList.remove('has-cover');
  }

  if (!current) {
    title.textContent = jobs.length ? '选择一个已完成任务开始审校' : '还没有可审校的已完成任务';
    meta.textContent = jobs.length
      ? '可直接在这里选择任务，不必返回工作台'
      : '完成翻译与风险分析后，任务会出现在这里';
    return;
  }
  const unresolved = unresolvedReviewRequiredCount(current);
  title.textContent = jobTitle(current);
  meta.textContent = `${current.metadata?.channel || '本地字幕任务'} · 完成于 ${formatDate(current.completed_at || current.updated_at)} · ${unresolved ? `还剩 ${metricNumber(unresolved)} 条必审项` : '必审风险已清零'}`;
}

async function loadRisks({ preservePage = false } = {}) {
  const requestVersion = ++risksRequestVersion;
  const target = $('#risk-list');
  if (!selectedJob) {
    allRiskItems = [];
    riskItems = [];
    risksLoadedJobId = '';
    renderReviewDelivery();
    renderReviewTaskContext();
    $('#risk-summary').innerHTML = '';
    $('#risk-nav-count').hidden = true;
    $('#risk-pagination').hidden = true;
    target.innerHTML = '<div class="empty">选择一个已完成任务后，风险队列会显示在这里</div>';
    return;
  }
  const requestedJobId = selectedJob.id;
  target.setAttribute('aria-busy', 'true');
  if (!allRiskItems.length) {
    target.innerHTML = `<div class="loading-state" role="status">
      <span class="loading-orbit" aria-hidden="true"></span>
      <b>正在读取审校队列…</b><small>风险快照与人工状态会分别加载</small>
    </div>`;
  }
  try {
    let risks = await api(`/api/jobs/${selectedJob.id}/risks`);
    if (requestVersion !== risksRequestVersion || selectedJob?.id !== requestedJobId) return;
    allRiskItems = risks;
    risksLoadedJobId = requestedJobId;
    const unresolvedRequired = risks.filter(item =>
      item.review_required && ['pending', 'review', 'failed'].includes(item.state)
    ).length;
    const reviewCountChanged = selectedJob?.id === requestedJobId
      && selectedJob._unresolvedReviewRequired !== unresolvedRequired;
    if (selectedJob?.id === requestedJobId) {
      selectedJob._unresolvedReviewRequired = unresolvedRequired;
      selectedJob.review_pending_count = unresolvedRequired;
    }
    renderReviewTaskContext();
    const automatic = risks.filter(item => item.state === 'auto_resolved').length;
    const advisory = risks.filter(item => item.state === 'advisory').length;
    const humanDone = risks.filter(item =>
      ['resolved', 'verified'].includes(item.state)
    ).length;
    $('#risk-summary').innerHTML = `
      <div class="${unresolvedRequired ? 'attention' : 'clear'}"><span>真正待处理</span><b>${metricNumber(unresolvedRequired)}</b></div>
      <div><span>已自动处理</span><b>${metricNumber(automatic)}</b></div>
      <div><span>仅供参考（不阻塞）</span><b>${metricNumber(advisory)}</b></div>
      <div><span>已人工完成</span><b>${metricNumber(humanDone)}</b></div>`;
    const navCount = $('#risk-nav-count');
    navCount.textContent = metricNumber(unresolvedRequired);
    navCount.hidden = unresolvedRequired === 0;
    const deliveryRisk = $('#delivery-risk-status');
    if (deliveryRisk) {
      deliveryRisk.className = unresolvedRequired ? 'wait' : 'pass';
      deliveryRisk.innerHTML = `<span aria-hidden="true">${unresolvedRequired ? '!' : '✓'}</span>${unresolvedRequired ? `还有 ${metricNumber(unresolvedRequired)} 条必须确认` : '必须处理的风险已经清零'}`;
    }
    if (reviewCountChanged && selectedJob?.status === 'completed') renderDetail();
    /* FE-P0-02：唯一筛选实现，详情 Risk 与 Review 视图共用 */
    riskItems = applyRiskFilter(risks);
    if (!preservePage) riskPage = 1;
    renderRiskPage();
    renderDetailRiskTab();
    syncUrl({
      riskFilter: $('#risk-filter')?.value || 'unresolved',
      page: riskPage,
    });
  } catch (error) {
    if (requestVersion !== risksRequestVersion) return;
    allRiskItems = [];
    riskItems = [];
    risksLoadedJobId = '';
    target.innerHTML = `<div class="empty error-state">
      <span class="empty-mark" aria-hidden="true">!</span>
      <b>审校队列读取失败</b><small>${escapeHtml(error.message)}</small>
      <button type="button" data-retry="risks">重新读取审校队列</button>
    </div>`;
    renderDetailRiskTab({ error: error.message });
  } finally {
    if (requestVersion === risksRequestVersion) target.removeAttribute('aria-busy');
  }
}

$('#risk-list')?.addEventListener('click', event => {
  if (event.target.closest('[data-retry="risks"]')) {
    loadRisks({ preservePage: true });
  }
});

/* ---- 表单：从 URL 创建 ---- */

function syncYoutubeAuth() {
  const selector = $('#youtube-auth');
  const row = $('#youtube-cookie-paste-row');
  const text = $('#youtube-cookies-text');
  if (!selector || !row || !text) return;
  if (!['none', 'chrome', 'file'].includes(selector.value)) selector.value = 'none';
  const usesPastedText = selector.value === 'file';
  row.hidden = !usesPastedText;
  text.required = usesPastedText;
  if (!usesPastedText) text.value = '';
}

async function pasteInitialYoutubeCookies() {
  const textarea = $('#youtube-cookies-text');
  const note = $('#url-note');
  if (!textarea) return;
  try {
    const content = await navigator.clipboard.readText();
    if (!content.trim()) throw new Error('剪贴板里没有文本');
    textarea.value = content;
    textarea.focus();
    if (note) note.textContent = `已从剪贴板读取 ${content.length} 个字符；创建任务前不会发送。`;
  } catch (_) {
    textarea.focus();
    if (note) note.textContent = '浏览器没有允许读取剪贴板，请在文本框内按 Ctrl + V。';
  }
}

$('#youtube-auth').addEventListener('change', () => {
  syncYoutubeAuth();
  saveFormMemory($('#url-form'));
});
$('#paste-youtube-cookies').addEventListener('click', pasteInitialYoutubeCookies);
syncYoutubeAuth();

function sourceLanguageLabel(value) {
  if (value === 'ja') return '日语';
  if (value === 'ko') return '韩语';
  return '英语';
}

function syncSourceLanguageCopy(form) {
  const language = form.querySelector('[name="source_language"]')?.value || 'en';
  const label = sourceLanguageLabel(language);
  const primary = form.querySelector('#capcut-upload');
  const secondary = form.querySelector('#whisper-upload');
  if (primary?.previousElementSibling?.tagName === 'LABEL') {
    primary.previousElementSibling.textContent = `${label}主字幕`;
  }
  if (secondary?.previousElementSibling?.tagName === 'LABEL') {
    secondary.previousElementSibling.textContent = `${label}辅助字幕`;
  }
}

document.querySelectorAll('[data-language-controls] select[name="source_language"]').forEach(select => {
  select.addEventListener('change', event => {
    syncSourceLanguageCopy(event.currentTarget.form);
    saveFormMemory(event.currentTarget.form);
  });
});
syncSourceLanguageCopy($('#job-form'));

$('#url-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter;
  setBusy(submit, true, '正在创建下载任务…');
  const note = $('#url-note');
  note.textContent = '正在创建下载任务…';
  const enteredKey = String(event.target.elements.api_key?.value || '').trim();
  if (!userSettings.api_key_configured && !enteredKey) {
    note.textContent = '请先输入并安全保存 API Key';
    event.target.elements.api_key?.focus();
    setBusy(submit, false);
    return;
  }
  let form;
  try {
    await persistUserSettingsForm(event.target);
    form = new FormData(event.target);
    form.delete("api_key");
    ["download_video", "download_subtitles", "download_thumbnail", "local_whisper"].forEach(key => {
      if (!form.has(key)) form.set(key, "false");
    });
    saveFormMemory(event.target);
    const job = await api("/api/jobs/from-url", { method: "POST", body: form });
    note.textContent = '';
    closeNewTaskDialog();
    await loadJobs();
    await selectJob(job.id);
    showToast('下载任务已创建');
  } catch (error) {
    note.textContent = error.message.replace(/^"|"$/g, '');
    showToast(note.textContent, 'error');
  } finally {
    form?.delete('youtube_cookies_text');
    const cookieText = event.target.querySelector('[name="youtube_cookies_text"]');
    if (cookieText) cookieText.value = '';
    setBusy(submit, false);
  }
});

/* ---- 表单：直接上传 SRT ---- */

$('#job-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter;
  setBusy(submit, true, '正在上传素材…');
  const note = $('#form-note');
  note.textContent = '正在上传并创建任务…';
  const form = new FormData(event.target);
  saveFormMemory(event.target);
  try {
    const job = await api('/api/jobs', { method: 'POST', body: form });
    note.textContent = '';
    closeNewTaskDialog();
    await loadJobs();
    await selectJob(job.id);
    showToast('素材已上传，可以配置翻译');
  } catch (error) {
    note.textContent = error.message.replace(/^"|"$/g, '');
    showToast(note.textContent, 'error');
  } finally { setBusy(submit, false); }
});

/* ---- 上传剪映字幕 ---- */

async function openCapcutApp(event) {
  const button = event.currentTarget;
  const note = document.querySelector('.capcut-launch-note');
  setBusy(button, true, '正在打开…');
  if (note) note.textContent = '';
  try {
    await api('/api/apps/capcut/open', { method: 'POST' });
    if (note) note.textContent = '已发送启动请求；如果剪映已在运行，窗口可能直接切到前台';
    showToast('正在打开剪映');
  } catch (error) {
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function continueWithAutomaticSource(event) {
  const button = event.currentTarget;
  const jobId = button.dataset.job;
  setBusy(button, true, "正在准备自动字幕…");
  try {
    await api(`/api/jobs/${jobId}/continue-with-automatic-source`, { method: "POST" });
    await selectJob(jobId);
    showToast("已使用自动字幕，可以开始翻译");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function uploadCapcut(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = event.submitter;
  setBusy(submit, true, '正在解析字幕…');
  const note = form.querySelector('.form-note');
  try {
    await api(`/api/jobs/${selectedJob.id}/capcut`, { method: 'POST', body: new FormData(form) });
    selectedJob = await api(`/api/jobs/${selectedJob.id}`);
    renderDetail();
    loadJobs();
    showToast('剪映字幕已上传，可以开始翻译');
  } catch (error) {
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  } finally { setBusy(submit, false); }
}

/* ---- 开始翻译 ---- */

async function startTranslation() {
  const jobId = selectedJob?.id;
  if (!jobId) return;
  const settings = collectTranslationSettings();
  if (!settings) return;
  const note = $('#translate-note');
  const button = document.querySelector('.start-translation');
  setBusy(button, true, '正在启动翻译…');
  try {
    await api(`/api/jobs/${jobId}/start`, { method: 'POST', body: settings.payload });
    await selectJob(jobId);
    showToast('翻译已开始，页面会实时更新进度');
  } catch (error) {
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  } finally { setBusy(button, false); }
}

async function requestBurnedVideo(event) {
  const jobId = event?.currentTarget?.dataset.job;
  if (!jobId || deliveryRequests.has(jobId)) return;
  const job = selectedJob?.id === jobId ? selectedJob : allJobs.find(item => item.id === jobId);
  if (job?.exports?.burned_video?.status === 'completed'
      && job.artifacts?.['final.zh.burned.mp4'] && job.burned_video_current !== false) return;
  deliveryRequests.add(jobId);
  const button = event?.currentTarget;
  setBusy(button, true, '正在排队…');
  try {
    const updated = await api(`/api/jobs/${jobId}/exports/burned-video`, { method: 'POST' });
    if (selectedJob?.id === jobId) { selectedJob = updated; renderDetail(); }
    loadJobs();
    showToast('交付文件已排队');
  } catch (error) {
    const note = $('#job-action-note');
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  } finally {
    deliveryRequests.delete(jobId);
    setBusy(button, false);
    renderReviewDelivery();
  }
}

async function cancelBurnedVideo(event) {
  const jobId = event?.currentTarget?.dataset.job;
  if (!jobId) return;
  const button = event?.currentTarget;
  setBusy(button, true, '正在取消…');
  try {
    const updated = await api(`/api/jobs/${jobId}/exports/burned-video/cancel`, { method: 'POST' });
    if (selectedJob?.id === jobId) { selectedJob = updated; renderDetail(); }
    loadJobs();
    showToast('已取消生成带字幕视频');
  } catch (error) {
    const note = $('#job-action-note');
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

/* ---- 停止与暂停任务 ---- */

async function cancelJob() {
  const jobId = selectedJob?.id;
  if (!jobId) return;
  try {
    await api(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
    stopStream();
    selectedJob = await api(`/api/jobs/${jobId}`);
    renderDetail();
    loadJobs();
    showToast('任务已停止，已有文件已保留');
  } catch (error) {
    const note = $('#job-action-note') || $('#translate-note') || $('#capcut-note');
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  }
}

async function pauseJob() {
  const jobId = selectedJob?.id;
  if (!jobId) return;
  try {
    await api(`/api/jobs/${jobId}/pause`, { method: 'POST' });
    stopStream();
    selectedJob = await api(`/api/jobs/${jobId}`);
    renderDetail();
    loadJobs();
    showToast('任务已暂停，已有进度已保留');
  } catch (error) {
    const note = $('#job-action-note') || $('#translate-note') || $('#capcut-note');
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  }
}

async function resumeJob() {
  const jobId = selectedJob?.id;
  if (!jobId) return;
  try {
    await api(`/api/jobs/${jobId}/resume`, { method: 'POST' });
    await selectJob(jobId);
    showToast('任务已继续');
  } catch (error) {
    const note = $('#job-action-note') || $('#translate-note') || $('#capcut-note');
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  }
}

async function forceResetJob() {
  if (!await confirmAction({
    title: '强制停止卡住的任务？',
    message: '这会中断当前后台进程并把任务标记为失败，已写入的阶段产物仍会保留。',
    confirmLabel: '强制停止',
    danger: true,
  })) return;
  try {
    stopStream();
    await api(`/api/jobs/${selectedJob.id}/force-reset`, { method: 'POST' });
    selectedJob = await api(`/api/jobs/${selectedJob.id}`);
    renderDetail();
    loadJobs();
    showToast('任务已强制停止，可以从失败批次重试');
  } catch (error) {
    const note = $('#job-action-note') || $('#translate-note') || $('#capcut-note');
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  }
}

/* ---- 删除任务 ---- */

async function deleteJob() {
  if (!await confirmAction({
    title: '删除任务及全部文件？',
    message: '视频、字幕、Manifest、风险记录和人工修改都会永久删除，无法恢复。',
    confirmLabel: '永久删除',
    danger: true,
  })) return;
  try {
    const jobId = selectedJob.id;
    if (['queued', 'running'].includes(selectedJob.status)) {
      selectedJob = await api(`/api/jobs/${jobId}/pause`, { method: 'POST' });
      stopStream();
      renderDetail();
      loadJobs();
    }
    await api(`/api/jobs/${jobId}`, { method: 'DELETE' });
    stopStream();
    selectionVersion += 1;
    selectedJob = null;
    syncUrl({ jobId: null });
    renderDetailEmpty();
    loadJobs();
    showToast('任务及其文件已删除');
  } catch (error) {
    const note = $('#job-action-note') || $('#translate-note') || $('#capcut-note');
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  }
}

function syncRetryYoutubeAuth(event) {
  const form = event?.target?.closest('form') || document.querySelector('.youtube-recovery-form');
  if (!form) return;
  const selector = form.querySelector('[name="youtube_auth"]');
  const row = form.querySelector('.retry-cookie-paste-row');
  const text = form.querySelector('[name="youtube_cookies_text"]');
  if (!selector || !row || !text) return;
  const usesPastedText = selector.value === 'file';
  row.hidden = !usesPastedText;
  text.required = usesPastedText;
  if (!usesPastedText) text.value = '';
}

async function pasteYoutubeCookies(event) {
  const form = event.currentTarget.closest('form');
  const textarea = form?.querySelector('[name="youtube_cookies_text"]');
  const note = form?.querySelector('.youtube-recovery-note');
  if (!textarea) return;
  try {
    const content = await navigator.clipboard.readText();
    if (!content.trim()) throw new Error('剪贴板里没有文本');
    textarea.value = content;
    textarea.focus();
    if (note) note.textContent = `已从剪贴板读取 ${content.length} 个字符；提交前不会发送。`;
  } catch (_) {
    textarea.focus();
    if (note) note.textContent = '浏览器没有允许读取剪贴板，请在文本框内按 Ctrl + V。';
  }
}

function openYoutubeRecovery(event) {
  const panel = document.querySelector('.youtube-recovery');
  if (!panel) return;
  const opening = panel.hidden;
  panel.hidden = !opening;
  event.currentTarget.setAttribute('aria-expanded', String(opening));
  if (opening) {
    syncRetryYoutubeAuth();
    panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
    panel.querySelector('select')?.focus();
  }
}

async function retryDownloadWithAuth(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const jobId = form.dataset.job;
  const note = form.querySelector('.youtube-recovery-note');
  const button = form.querySelector('.retry-auth-submit');
  const payload = new FormData(form);
  payload.set('restart', 'false');
  setBusy(button, true, '正在更新 Cookie…');
  note.textContent = '正在保存新的认证信息并重新排队…';
  try {
    await api(`/api/jobs/${jobId}/retry`, {
      method: 'POST',
      body: payload,
    });
    const memory = loadFormMemory();
    memory.youtube_auth = String(payload.get('youtube_auth') || 'none');
    localStorage.setItem('subtitle_console', JSON.stringify(memory));
    note.textContent = '';
    await selectJob(jobId);
    showToast('Cookie 已更新，正在重新下载');
  } catch (error) {
    note.textContent = error.message;
    showToast(error.message, 'error');
  } finally {
    payload.delete('youtube_cookies_text');
    const cookieText = form.querySelector('[name="youtube_cookies_text"]');
    if (cookieText) cookieText.value = '';
    setBusy(button, false);
  }
}

async function retryJob(e) {
  const jobId = e.target.closest('button').dataset.job;
  const restart = e.target.closest('button').dataset.restart === 'true';
  const isDownloadRetry = selectedJob?.status === 'failed'
    && selectedJob?.url
    && !selectedJob?.inputs?.capcut_en;
  const requiresFreshKey = selectedJob?.error_code === 'authentication_error';
  const settings = isDownloadRetry
    ? { payload: new FormData(), enteredKey: '', key: '' }
    : collectTranslationSettings({ requireFreshKey: requiresFreshKey });
  if (!settings) return;
  const button = e.target.closest('button');
  button.disabled = true;
  settings.payload.set('restart', String(restart));
  try {
    await api(`/api/jobs/${jobId}/retry`, {
      method: 'POST',
      body: settings.payload,
    });
    await selectJob(jobId);
    showToast(restart ? '任务已从头重新开始' : '任务已继续执行');
  } catch (error) {
    const note = $('#job-action-note') || $('#translate-note');
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  } finally { button.disabled = false; }
}

/* ---- 术语表 ---- */

let glossaryData = [];

function glossarySourceLanguage() {
  return $('#glossary-source-language')?.value || 'en';
}

async function loadGlossary(query = '') {
  const requestVersion = ++glossaryRequestVersion;
  const target = $('#glossary-list');
  try {
    const params = new URLSearchParams();
    params.set('source_language', glossarySourceLanguage());
    params.set('target_language', 'zh-CN');
    if (query) params.set('q', query);
    const terms = await api(`/api/glossary?${params}`);
    if (requestVersion !== glossaryRequestVersion) return;
    glossaryData = terms;
    renderGlossary();
    const stats = await api('/api/glossary/stats');
    if (requestVersion !== glossaryRequestVersion) return;
    $('#glossary-stats').textContent = `共 ${stats.total} 条 · ${stats.by_game.map(g => `${g[0]}: ${g[1]}`).join(' / ')}`;
    syncUrl({ glossaryQuery: query });
  } catch (error) {
    if (requestVersion !== glossaryRequestVersion) return;
    target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

function renderGlossary() {
  const target = $('#glossary-list');
  if (!glossaryData.length) {
    target.innerHTML = '<div class="empty">没有匹配的术语</div>';
    return;
  }
  target.innerHTML = glossaryData.map((term, i) => `
    <div class="glossary-item" data-index="${i}" data-en="${escapeHtml(term.english)}">
      <span class="g-en">${escapeHtml(term.english)}</span>
      <span class="g-zh">${escapeHtml(term.chinese)}</span>
      <span class="g-cat">${escapeHtml(term.category || '—')}</span>
      <div class="g-actions">
        <button type="button" class="edit" title="编辑 ${escapeHtml(term.english)}">编辑</button>
        <button type="button" class="del" title="删除 ${escapeHtml(term.english)}">删除</button>
      </div>
    </div>`).join('');
  target.querySelectorAll('.g-actions .edit').forEach(btn => btn.onclick = () => enterEditMode(btn.closest('.glossary-item')));
  target.querySelectorAll('.g-actions .del').forEach(btn => btn.onclick = () => deleteTerm(btn.closest('.glossary-item')));
}

function enterEditMode(item) {
  const index = parseInt(item.dataset.index);
  const term = glossaryData[index];
  item.classList.add('editing');
  item.innerHTML = `
    <input class="edit-en" value="${escapeHtml(term.english)}" name="edit_english" aria-label="英文术语" autocomplete="off" placeholder="英文术语…">
    <input class="edit-zh" value="${escapeHtml(term.chinese)}" name="edit_chinese" aria-label="中文译名" autocomplete="off" placeholder="中文译名…">
    <select class="edit-cat" name="edit_category" aria-label="术语分类" autocomplete="off">
      <option value="">无</option>
      <option value="character" ${term.category === 'character' ? 'selected' : ''}>角色</option>
      <option value="system" ${term.category === 'system' ? 'selected' : ''}>系统</option>
      <option value="currency" ${term.category === 'currency' ? 'selected' : ''}>货币</option>
      <option value="enemy" ${term.category === 'enemy' ? 'selected' : ''}>敌人</option>
      <option value="lore" ${term.category === 'lore' ? 'selected' : ''}>设定</option>
      <option value="location" ${term.category === 'location' ? 'selected' : ''}>地点</option>
      <option value="faction" ${term.category === 'faction' ? 'selected' : ''}>阵营</option>
    </select>
    <div class="g-actions">
      <button type="button" class="save">保存</button>
      <button type="button" class="cancel-edit">取消</button>
    </div>`;
  item.querySelector('.save').onclick = () => saveEdit(item, index, term);
  item.querySelector('.cancel-edit').onclick = () => renderGlossary();
}

async function saveEdit(item, index, oldTerm) {
  const en = item.querySelector('.edit-en').value.trim();
  const zh = item.querySelector('.edit-zh').value.trim();
  const cat = item.querySelector('.edit-cat').value;
  if (!en || !zh) return;
  try {
    await api(`/api/glossary/${encodeURIComponent(oldTerm.english)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ english: en, game: 'wuwa', chinese: zh, category: cat,
        source_language: glossarySourceLanguage(), target_language: 'zh-CN' }),
    });
    glossaryData[index] = { english: en, chinese: zh, game: 'wuwa', category: cat };
    renderGlossary();
    showToast(`已更新术语：${en}`);
  } catch (error) {
    $('#glossary-add-form').hidden = false;
    const note = $('#term-note');
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  }
}

async function deleteTerm(item) {
  const en = item.dataset.en;
  if (!await confirmAction({
    title: '删除术语？',
    message: `“${en}”将不再注入之后创建的翻译任务。已有任务快照不受影响。`,
    confirmLabel: '删除术语',
    danger: true,
  })) return;
  try {
    await api(`/api/glossary/${encodeURIComponent(en)}?game=wuwa&source_language=${encodeURIComponent(glossarySourceLanguage())}&target_language=zh-CN`, { method: 'DELETE' });
    await loadGlossary($('#glossary-search').value);
    showToast(`已删除术语：${en}`);
  } catch (error) {
    $('#glossary-add-form').hidden = false;
    const note = $('#term-note');
    if (note) note.textContent = error.message;
    showToast(error.message, 'error');
  }
}

function bindGlossaryEvents() {
  $('#load-glossary').onclick = () => loadGlossary($('#glossary-search').value);
  $('#glossary-search').oninput = debounce(
    event => loadGlossary(event.target.value),
    260,
  );
  $('#glossary-source-language')?.addEventListener('change', () => {
    loadGlossary($('#glossary-search').value);
  });
  $('#add-term-toggle').onclick = () => {
    const form = $('#glossary-add-form');
    form.hidden = !form.hidden;
    $('#add-term-toggle').setAttribute('aria-expanded', String(!form.hidden));
    if (!form.hidden) $('#term-en').focus();
  };
  $('#term-save').onclick = async () => {
    const en = $('#term-en').value.trim();
    const zh = $('#term-zh').value.trim();
    const cat = $('#term-cat').value;
    const note = $('#term-note');
    if (!en || !zh) { note.textContent = '英文和中文都不能为空'; return; }
    $('#term-save').disabled = true;
    try {
      await api('/api/glossary', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        english: en, chinese: zh, game: 'wuwa', category: cat,
        source_language: glossarySourceLanguage(), target_language: 'zh-CN',
      }) });
      $('#term-en').value = '';
      $('#term-zh').value = '';
      note.textContent = `已保存: ${en} = ${zh}`;
      await loadGlossary($('#glossary-search').value);
      showToast(`已添加术语：${en}`);
    } catch (error) {
      note.textContent = error.message;
      showToast(error.message, 'error');
    } finally { $('#term-save').disabled = false; }
  };
}

/* ---- 提示词模板 ---- */

let promptOriginal = '';

function promptSourceLanguage() {
  return $('#prompt-source-language')?.value || 'en';
}

async function loadPrompt(confirmDiscard = true) {
  const editor = $('#prompt-editor');
  if (
    confirmDiscard
    && promptOriginal
    && editor.value !== promptOriginal
    && !await confirmAction({
      title: '放弃提示词修改？',
      message: '编辑器中的内容尚未保存，重新加载后无法恢复。',
      confirmLabel: '放弃并重新加载',
      danger: true,
    })
  ) return;
  try {
    const data = await api(
      `/api/prompt-template?source_language=${encodeURIComponent(promptSourceLanguage())}&target_language=zh-CN`
    );
    editor.value = data.content;
    promptOriginal = data.content;
    $('#prompt-note').textContent = `已加载 · ${data.content.length} 字符`;
  } catch (error) {
    editor.value = '';
    $('#prompt-note').textContent = error.message;
  }
}

async function savePrompt() {
  const content = $('#prompt-editor').value;
  const note = $('#prompt-note');
  $('#save-prompt').disabled = true;
  try {
    await api('/api/prompt-template', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
      content,
      source_language: promptSourceLanguage(),
      target_language: 'zh-CN',
    }) });
    promptOriginal = content;
    note.textContent = `已保存 · ${content.length} 字符`;
    showToast('提示词模板已保存，将用于之后开始的任务');
  } catch (error) {
    note.textContent = error.message;
    showToast(error.message, 'error');
  } finally { $('#save-prompt').disabled = false; }
}

async function renameSelectedJob() {
  if (!selectedJob) return;
  const customName = $('#task-custom-name')?.value.trim() || '';
  try {
    selectedJob = await api(`/api/jobs/${selectedJob.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ custom_name: customName }),
    });
    renderDetail();
    await loadJobs();
    showToast(customName ? '任务名称已保存' : '已恢复自动任务名称');
  } catch (error) { showToast(error.message, 'error'); }
}

async function archiveSelectedJob() {
  if (!selectedJob) return;
  try {
    selectedJob = await api(`/api/jobs/${selectedJob.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ archived: !selectedJob.archived }),
    });
    renderDetail();
    await loadJobs();
    showToast(selectedJob.archived ? '任务已归档' : '任务已取消归档');
  } catch (error) { showToast(error.message, 'error'); }
}

async function openJobFolder(event) {
  const jobId = event?.currentTarget?.dataset.job;
  if (!jobId) return;
  try {
    await api(`/api/jobs/${jobId}/open-folder`, { method: 'POST' });
  } catch (error) { showToast(error.message, 'error'); }
}

async function openDownloadsFolder() {
  try {
    await api('/api/downloads/open', { method: 'POST' });
  } catch (error) { showToast(error.message, 'error'); }
}

async function cleanupSelectedJob(kind) {
  if (!selectedJob) return;
  const message = kind === 'video'
    ? '确定清理本任务的源视频和源音频吗？字幕会保留。'
    : '确定清理 Manifest、风险 JSON 等技术文件吗？清理后无法重新生成审校视图。最终字幕会保留。';
  if (!await confirmAction({
    title: kind === 'video' ? '清理源媒体但保留字幕？' : '清理技术文件？',
    message,
    confirmLabel: kind === 'video' ? '清理源媒体' : '清理文件',
    danger: true,
  })) return;
  try {
    const result = await api(`/api/jobs/${selectedJob.id}/cleanup/${kind}`, { method: 'POST' });
    selectedJob = result.job;
    renderDetail();
    await loadJobs();
    showToast(`已清理 ${result.removed.length} 个文件`);
  } catch (error) { showToast(error.message, 'error'); }
}

async function organizeSelectedJob() {
  if (!selectedJob) return;
  try {
    const result = await api(`/api/jobs/${selectedJob.id}/organize`, { method: 'POST' });
    selectedJob = result.job;
    renderDetail();
    await loadJobs();
    const moved = result.moved.length + result.moved_directories.length;
    showToast(moved ? `已整理 ${moved} 项过程文件` : '文件已经整理好了');
  } catch (error) { showToast(error.message, 'error'); }
}

function bindPromptEvents() {
  $('#load-prompt').onclick = () => loadPrompt(true);
  $('#save-prompt').onclick = savePrompt;
  $('#prompt-source-language')?.addEventListener('change', () => loadPrompt(true));
  $('#prompt-editor').addEventListener('input', () => {
    if (!promptOriginal) return;
    $('#prompt-note').textContent = $('#prompt-editor').value === promptOriginal
      ? `已加载 · ${promptOriginal.length} 字符`
      : '有尚未保存的修改';
  });
  $('#prompt-editor').addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      savePrompt();
    }
  });
}

/* ---- 导航 ---- */

function normalizeNavigationView(view) {
  return NAVIGATION_VIEWS.has(view) ? view : 'workbench';
}

function focusViewHeading(panel) {
  const heading = panel.querySelector('h2');
  if (!heading) return;
  heading.setAttribute('tabindex', '-1');
  requestAnimationFrame(() => heading.focus({ preventScroll: true }));
}

function announceView(panel) {
  const announcer = $('#view-announcer');
  const heading = panel.querySelector('h2');
  if (announcer && heading) announcer.textContent = `已打开：${heading.textContent.trim()}`;
}

async function activateView(view, { sync = true, focus = sync } = {}) {
  view = normalizeNavigationView(view);
  const leavingReview = view !== 'review' && document.getElementById('review')?.classList.contains('active');
  const leavingDetailRisk = view !== 'workbench'
    && document.getElementById('workbench')?.classList.contains('active')
    && activeDetailTab === 'risk';
  if ((leavingReview || leavingDetailRisk) && !await confirmDiscardRiskEdit()) return;
  if (leavingReview || leavingDetailRisk || (view !== 'review' && view !== 'workbench')) riskDirty = false;
  const button = Array.from(document.querySelectorAll('.nav'))
    .find(candidate => candidate.dataset.view === view);
  const panel = document.getElementById(view);
  if (!button || !panel) return;
  document.querySelectorAll('.nav, .view').forEach(element => element.classList.remove('active'));
  document.querySelectorAll('.nav').forEach(element => {
    element.setAttribute('aria-pressed', 'false');
    element.removeAttribute('aria-current');
  });
  button.classList.add('active');
  button.setAttribute('aria-pressed', 'true');
  button.setAttribute('aria-current', 'page');
  panel.classList.add('active');
  document.body.classList.toggle('workbench-active', view === 'workbench');
  if (focus) {
    focusViewHeading(panel);
    announceView(panel);
  }
  if (sync) syncUrl({ view, jobId: selectedJob?.id });
  if (view === 'glossary') loadGlossary($('#glossary-search')?.value || '');
  if (view === 'review') {
    if (!allJobs.length) await loadJobs();
    renderReviewTaskContext();
    loadRisks();
  }
  if (view === 'prompt' && !promptOriginal) loadPrompt(false);
  if (view === 'settings') populateSettingsForm();
}

document.querySelectorAll('.nav').forEach(button => {
  button.onclick = () => activateView(button.dataset.view);
});

function filterFaqItems() {
  const input = $('#faq-search');
  const count = $('#faq-result-count');
  const empty = $('#faq-empty');
  if (!input || !count || !empty) return;

  const query = input.value.trim().toLocaleLowerCase('zh-CN');
  let visible = 0;
  document.querySelectorAll('.faq-group').forEach(group => {
    let visibleInGroup = 0;
    group.querySelectorAll('.faq-item').forEach(item => {
      const text = `${item.dataset.faqKeywords || ''} ${item.textContent || ''}`
        .toLocaleLowerCase('zh-CN');
      const matches = !query || text.includes(query);
      item.hidden = !matches;
      if (matches) {
        visible += 1;
        visibleInGroup += 1;
      }
    });
    group.hidden = visibleInGroup === 0;
  });

  count.textContent = query ? `找到 ${visible} 个相关问题` : `${visible} 个常见问题`;
  empty.hidden = visible !== 0;
  syncUrl({ faqQuery: input.value });
}

async function runHelpAction(action) {
  if (action === 'new-task') {
    await activateView('workbench');
    openNewTaskDialog('url', document.activeElement);
    const input = $('#video-url');
    input?.focus();
    return;
  }
  if (action === 'download-options') {
    await activateView('workbench');
    openNewTaskDialog('url', document.activeElement);
    const settings = document.querySelector('.advanced-settings');
    if (settings) settings.open = true;
    const auth = $('#youtube-auth');
    auth?.focus();
    return;
  }
  if (action === 'glossary') {
    await activateView('glossary');
    $('#glossary-search')?.focus();
    return;
  }
  if (action === 'review') {
    await activateView('review');
  }
}

async function openFaqForCode(code) {
  await activateView('faq');
  const input = $('#faq-search');
  if (input) input.value = '';
  filterFaqItems();
  const item = Array.from(document.querySelectorAll('.faq-item[data-faq-code]'))
    .find(node => node.dataset.faqCode === code);
  if (!item) return;
  item.open = true;
  item.scrollIntoView({ block: 'center' });
  item.classList.add('faq-item-flash');
  setTimeout(() => item.classList.remove('faq-item-flash'), 1600);
}

/* 错误横幅随任务列表和详情重绘动态生成，init 期的直接绑定拿不到，故用委托。 */
document.addEventListener('click', async (event) => {
  const faqButton = event.target.closest('.job-error-faq[data-faq-code]');
  if (faqButton) {
    event.stopPropagation();
    await openFaqForCode(faqButton.dataset.faqCode);
    return;
  }
  const viewButton = event.target.closest('[data-error-view]');
  if (viewButton) {
    event.stopPropagation();
    await activateView(viewButton.dataset.errorView);
  }
});

$('#faq-search')?.addEventListener('input', debounce(filterFaqItems, 120));
document.querySelectorAll('[data-help-action]').forEach(button => {
  button.addEventListener('click', () => runHelpAction(button.dataset.helpAction));
});
filterFaqItems();

$('#app-settings-form').addEventListener('submit', saveAppSettings);
$('#forget-api-key')?.addEventListener('click', forgetSavedApiKey);
$('#reset-app-settings').onclick = resetAppSettings;

["base_url", "model", "round2_model", "proxy", "batch_size", "api_key"]
  .forEach(name => {
    const input = $(`#url-form [name="${name}"]`);
    input?.addEventListener("change", async () => {
      try {
        await persistUserSettingsForm($("#url-form"));
      } catch (error) {
        $("#url-note").textContent = error.message;
      }
    });
  });
$("#url-local-whisper")?.addEventListener("change", async () => {
  try {
    await persistUserSettingsForm($("#url-form"));
  } catch (error) {
    $("#url-note").textContent = error.message;
  }
});

$('#refresh').onclick = async () => {
  await Promise.all([loadJobs(), refreshSelectedJob()]);
};
$('#job-search').oninput = debounce(loadJobs, 180);
$('#job-status-filter').onchange = () => {
  jobSummaryFilter = '';
  loadJobs();
};
$('#job-status-summary')?.addEventListener('click', event => {
  const button = event.target.closest('[data-job-summary-filter]');
  if (!button) return;
  const next = button.dataset.jobSummaryFilter || '';
  jobSummaryFilter = jobSummaryFilter === next ? '' : next;
  const select = $('#job-status-filter');
  if (select) select.value = '';
  loadJobs();
});
$('#continue-last').onclick = async () => {
  const remembered = localStorage.getItem('subtitle_last_job');
  const candidate = allJobs.find(job => job.id === remembered) || allJobs[0];
  if (candidate) await selectJob(candidate.id);
  else showToast('还没有可以继续的任务', 'error');
};
$('#load-risks').onclick = loadRisks;
$('#review-task-select').onchange = async event => {
  const jobId = event.target.value;
  if (!jobId || jobId === selectedJob?.id) return;
  await selectJob(jobId, { preserveRiskPage: true });
};
$('#risk-filter').onchange = async () => {
  if (!await confirmDiscardRiskEdit()) return;
  riskDirty = false;
  loadRisks();
};
$('#risk-prev').onclick = async () => {
  if (!await confirmDiscardRiskEdit()) return;
  riskDirty = false;
  riskPage -= 1;
  renderRiskPage();
  $('#risk-list').scrollIntoView({ behavior: 'smooth', block: 'start' });
};
$('#risk-next').onclick = async () => {
  if (!await confirmDiscardRiskEdit()) return;
  riskDirty = false;
  riskPage += 1;
  renderRiskPage();
  $('#risk-list').scrollIntoView({ behavior: 'smooth', block: 'start' });
};

async function bulkReview(useRecommended) {
  if (!selectedJob || !riskItems.length) return;
  const total = riskItems.length;
  const label = useRecommended ? '采用每条推荐结果' : '全部确认 Round 1 无误';
  if (!await confirmAction({
    title: `批量处理 ${total} 条字幕？`,
    message: `将对当前筛选结果${label}。已人工编辑但尚未保存的文本不会包含在本次操作中。`,
    confirmLabel: useRecommended ? '批量采用推荐' : '全部确认 Round 1',
  })) return;
  const button = useRecommended ? $('#bulk-recommended') : $('#bulk-verify');
  setBusy(button, true, '批量保存中…');
  try {
    const items = riskItems.map(item => {
      const round2 = String(item.round2_text || '').trim();
      const round1 = String(item.translated || '').trim();
      const text = useRecommended && round2 ? round2 : round1;
      const state = useRecommended && round2 ? 'resolved' : 'verified';
      return { key: item.key, state, text };
    });
    await api(`/api/jobs/${selectedJob.id}/risks/bulk`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items }),
    });
    riskDirty = false;
    await refreshSelectedJob(selectedJob.id);
    await loadRisks();
    showToast(`已批量处理 ${total} 条字幕`);
  } catch (error) {
    showToast(`批量处理未完成：${error.message}`, 'error');
    await loadRisks();
  } finally {
    setBusy(button, false);
  }
}

$('#bulk-recommended').onclick = () => bulkReview(true);
$('#bulk-verify').onclick = () => bulkReview(false);

document.addEventListener('keydown', event => {
  if (!document.getElementById('review')?.classList.contains('active')) return;
  const reviewRiskList = $('#risk-list');
  const card = reviewRiskList?.querySelector('.risk.is-active') || reviewRiskList?.querySelector('.risk');
  if (!card) return;
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
  if (event.key === 'Enter' && document.activeElement?.tagName === 'TEXTAREA' && !event.shiftKey) {
    event.preventDefault();
    saveRiskCard(card, 'resolved');
  } else if (!typing && event.key === '1') {
    event.preventDefault();
    saveRiskCard(card, 'verified');
  } else if (!typing && event.key === '2') {
    const button = card.querySelector('[data-use-round2="1"]');
    if (button) {
      event.preventDefault();
      saveRiskCard(card, 'resolved', true);
    }
  }
});

/* ---- 初始化 ---- */

function healthDiagnosticSummary(health) {
  const diagnostics = [];
  const stuckJobs = Array.isArray(health.stuck_jobs) ? health.stuck_jobs : [];
  const recentFailures = Array.isArray(health.recent_failures) ? health.recent_failures : [];
  const diskPercent = Number(health.disk?.percent);
  if (stuckJobs.length) diagnostics.push(`疑似卡住 ${stuckJobs.length} 个`);
  if (recentFailures.length) {
    diagnostics.push(`近期失败 ${recentFailures.slice(0, 3).join(' / ')}`);
  }
  if (Number.isFinite(diskPercent) && diskPercent >= 80) {
    diagnostics.push(`任务磁盘 ${Math.round(diskPercent)}%`);
  }
  return diagnostics;
}

api('/api/health').then(health => {
  if (localSessionExpired) return;
  const warnings = Array.isArray(health.warnings) ? health.warnings : [];
  const diagnostics = healthDiagnosticSummary(health);
  const status = $('#health');
  if (health.version && $('#app-version')) {
    $('#app-version').textContent = `v${health.version}`;
  }
  const details = [...diagnostics, ...warnings];
  status.textContent = details.length
    ? `在线 · ${details.join(' · ')}`
    : '127.0.0.1 / 在线';
  status.title = diagnostics.length
    ? `诊断：${diagnostics.join(' · ')}。点击任务卡片查看详情或重试。`
    : warnings.length
      ? '基础翻译仍可使用；缺失的可选能力会在需要时不可用。'
      : '下载、转码与本地识别能力均已就绪。';
  applyCapabilityLimits(health);
}).catch(() => {
  $('#health').textContent = localSessionExpired ? '会话已失效' : '服务不可用';
});
applyMemory();
syncYoutubeAuth();
applyAppSettings(loadAppSettings());
populateSettingsForm();
window.dispatchEvent(new CustomEvent('phoebe-pet-settings', {
  detail: {
    motionReduced: Boolean(appSettings.motion_reduced),
    petId: appSettings.pet_id,
  },
}));
updatePhoebeAssistant(null);
document.querySelectorAll('.form-note').forEach(note => note.setAttribute('aria-live', 'polite'));
bindGlossaryEvents();
bindPromptEvents();

let scrollFrame = 0;
const COMPACT_ENTER_Y = 230;
const COMPACT_EXIT_Y = 90;

function syncCompactHeader() {
  const compact = document.body.classList.contains('header-compact');
  if (!compact && window.scrollY >= COMPACT_ENTER_Y) {
    document.body.classList.add('header-compact');
  } else if (compact && window.scrollY <= COMPACT_EXIT_Y) {
    document.body.classList.remove('header-compact');
  }
}

window.addEventListener('scroll', () => {
  if (scrollFrame) return;
  scrollFrame = requestAnimationFrame(() => {
    syncCompactHeader();
    scrollFrame = 0;
  });
}, { passive: true });
syncCompactHeader();

async function initialize() {
  const params = new URLSearchParams(window.location.search);
  const requestedView = normalizeNavigationView(params.get('view'));
  const hydratedFields = [
    ['#job-search', 'jobs_q'],
    ['#job-status-filter', 'jobs_status'],
    ['#risk-filter', 'risk'],
    ['#faq-search', 'faq_q'],
    ['#glossary-search', 'glossary_q'],
  ];
  hydratedFields.forEach(([selector, parameter]) => {
    const element = $(selector);
    const value = params.get(parameter);
    if (element && value !== null) element.value = value;
  });
  const requestedPage = Number.parseInt(params.get('page') || '1', 10);
  riskPage = Number.isFinite(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  filterFaqItems();
  await loadUserSettings();
  populateSettingsForm();
  await activateView(requestedView, { sync: false });
  const jobs = await loadJobs();
  startGlobalJobStream();
  const requestedJob = params.get('job');
  if (requestedJob && jobs.some(job => job.id === requestedJob)) {
    await selectJob(requestedJob, { preserveRiskPage: true });
  }
}

initialize().catch(error => {
  if (error.status === 401) showSessionRecovery();
  else showToast('工作台加载失败，请确认本地服务后刷新', 'error');
});

window.addEventListener('beforeunload', event => {
  globalJobEventSource?.close();
  if (riskDirty || (promptOriginal && $('#prompt-editor').value !== promptOriginal)) {
    event.preventDefault();
    event.returnValue = '';
  }
});
