const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/static/app.js'), 'utf8');

function harness() {
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      innerHTML: '', textContent: '', hidden: false, value: 'unresolved', dataset: {},
      setAttribute() {}, removeAttribute() {}, addEventListener() {},
      querySelector() {return null;}, querySelectorAll() {return [];},
      classList: {contains() {return false;}, toggle() {}},
    });
    return elements.get(selector);
  };
  const context = {
    selectedJob: {id: 'review-A', status: 'completed', artifacts: {
      video: 'video.mp4', 'final.reviewed.zh.srt': 'final.reviewed.zh.srt',
    }, review_pending_count: 0, burn_review_ready: true, exports: {}},
    allJobs: [], risksLoadedJobId: 'review-A', riskItems: [], allRiskItems: [],
    riskSaving: false, riskDirty: false, risksRequestVersion: 0, jobsRequestVersion: 0,
    deliveryRequests: new Set(), subtitleBurnCapability: {available: true},
    $: element, elements, escapeHtml: value => String(value), metricNumber: String,
    CSS: {escape: String}, showToast() {}, setBusy() {}, renderDetail() {},
    loadJobs() {}, renderReviewTaskContext() {}, renderDetailRiskTab() {},
    renderRiskPage() {context.renderReviewDelivery();},
    applyRiskFilter: items => items.filter(item => item.state === 'review'),
    syncUrl() {}, riskPage: 1, activeDetailTab: 'overview',
    document: {hidden: false, body: {classList: {toggle() {}}}},
    renderJobSummary() {}, jobMatchesCurrentFilters: () => true,
    renderCurrentJobsListHtml: () => '', updateBatchBar() {}, applyJobsListHtml() {},
    JOBS_POLL_EMPTY_MS: 15000, JOBS_POLL_ACTIVE_MS: 2000, JOBS_POLL_IDLE_MS: 8000,
    ACTIVE_JOB_STATUSES: ['running', 'queued'], transport: {},
    requestAnimationFrame: fn => fn(),
  };
  vm.createContext(context);
  context.install = name => {
    const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?^}`, 'm'));
    assert.ok(match, `production function ${name} exists`);
    vm.runInContext(match[0], context);
  };
  for (const name of ['unresolvedReviewRequiredCount', 'renderBurnedVideoExport',
    'renderDeliveryFiles', 'bindDeliveryInteractions', 'renderReviewDelivery',
    'requestBurnedVideo', 'cancelBurnedVideo', 'openJobFolder', 'saveRiskCard',
    'loadRisks', 'jobsPollIntervalMs']) context.install(name);
  return context;
}

test('last saved required risk shows completion and current-task delivery CTA', async () => {
  const c = harness();
  c.selectedJob.review_pending_count = 1;
  c.selectedJob.burn_review_ready = false;
  c.allRiskItems = [{key: 'last', review_required: true, state: 'review', translated: 'Test'}];
  c.renderReviewDelivery();
  assert.equal(c.elements.get('#review-delivery').hidden, true);
  c.api = async (url, options) => {
    if (options?.method === 'PATCH') {
      assert.equal(url, '/api/jobs/review-A/risks/last');
      c.allRiskItems[0].state = 'verified';
      c.selectedJob.burn_review_ready = true;
      return {};
    }
    assert.equal(url, '/api/jobs/review-A/risks');
    return c.allRiskItems;
  };
  c.refreshSelectedJob = async id => assert.equal(id, 'review-A');
  const card = {dataset: {key: 'last'}, closest: () => null,
    querySelector: selector => selector === 'textarea' ? {value: 'Test'} : {},
    querySelectorAll: () => []};
  assert.equal(await c.saveRiskCard(card, 'verified'), true);
  assert.equal(c.selectedJob.review_pending_count, 0);
  const root = c.elements.get('#review-delivery');
  assert.equal(root.hidden, false);
  assert.match(root.innerHTML, /审校已完成/);
  assert.match(root.innerHTML, /生成交付文件/);
  assert.match(root.innerHTML, /data-job="review-A"/);
});

test('CTA uses bound review task even when list selection has changed', async () => {
  const c = harness();
  c.selectedJob = {id: 'list-B'};
  const calls = [];
  c.api = async url => {calls.push(url); return {id: 'review-A'};};
  await c.requestBurnedVideo({currentTarget: {dataset: {job: 'review-A'}}});
  assert.deepEqual(calls, ['/api/jobs/review-A/exports/burned-video']);
  assert.equal(c.selectedJob.id, 'list-B');
});

test('late save response never refreshes or switches another task', async () => {
  const c = harness();
  c.allRiskItems = [{key: 'last', translated: 'Test'}];
  c.api = async () => {c.selectedJob = {id: 'other'};};
  c.refreshSelectedJob = () => {throw Error('must not refresh other task');};
  await c.saveRiskCard({dataset: {key: 'last'}, closest: () => null,
    querySelector: () => ({value: 'Test'}), querySelectorAll: () => []}, 'verified');
  assert.equal(c.selectedJob.id, 'other');
});

test('completed current MP4 downloads without posting another request', async () => {
  const c = harness();
  c.selectedJob.exports = {burned_video: {status: 'completed'}};
  c.selectedJob.artifacts['final.zh.burned.mp4'] = 'final.zh.burned.mp4';
  c.selectedJob.burned_video_current = true;
  c.api = () => {throw Error('duplicate post');};
  c.renderReviewDelivery();
  assert.match(c.elements.get('#review-delivery').innerHTML, /黑框白字2 MP4/);
  assert.doesNotMatch(c.elements.get('#review-delivery').innerHTML, /generate-burned-video/);
  await c.requestBurnedVideo({currentTarget: {dataset: {job: 'review-A'}}});
  c.selectedJob.burned_video_current = false;
  assert.match(c.renderBurnedVideoExport(c.selectedJob, false), /generate-burned-video/);
});

for (const [state, expected] of [['queued', '等待生成'], ['running', '正在生成 32%'],
  ['cancelled', '重新生成交付文件'], ['failed', '重新生成交付文件'], ['completed', '下载带字幕视频']]) {
  test(`restored ${state} export renders in place with independent SRT`, () => {
    const c = harness();
    c.selectedJob.exports = {burned_video: {status: state, progress: 32}};
    c.selectedJob.artifacts['final.zh.burned.mp4'] = 'final.zh.burned.mp4';
    c.renderReviewDelivery();
    const markup = c.elements.get('#review-delivery').innerHTML;
    assert.ok(markup.includes(expected), markup);
    assert.match(markup, /已审校 SRT/);
    assert.match(markup, /打开任务目录/);
    if (state !== 'completed') assert.doesNotMatch(markup, /burned-video-download/);
  });
}

test('duplicate clicks coalesce while request is in flight', async () => {
  const c = harness();
  let finish;
  let calls = 0;
  c.api = () => {calls++; return new Promise(resolve => {finish = resolve;});};
  const event = {currentTarget: {dataset: {job: 'review-A'}}};
  const first = c.requestBurnedVideo(event);
  await c.requestBurnedVideo(event);
  assert.equal(calls, 1);
  finish(c.selectedJob);
  await first;
  assert.equal(c.deliveryRequests.size, 0);
});

test('polling recovers selected export; active export sets fast interval', async () => {
  const c = harness();
  c.install('loadJobs');
  const fresh = {...c.selectedJob, exports: {burned_video: {status: 'running'}}};
  const urls = [];
  c.transport.get = async url => {
    urls.push(url);
    return url === '/api/jobs' ? [{...fresh, summary_only:true, burned_video_current:false}] : fresh;
  };
  await c.loadJobs({silent: true});
  assert.equal(c.selectedJob.exports.burned_video.status, 'running');
  assert.equal(c.jobsPollIntervalMs(), 2000);
  assert.deepEqual(urls, ['/api/jobs', `/api/jobs/${fresh.id}`]);
});

test('SSE refresh updates selected export even if task is filtered out', () => {
  const c = harness();
  c.install('replaceJobCard');
  c.allJobs = [c.selectedJob];
  c.document.querySelector = () => null;
  c.jobMatchesCurrentFilters = () => false;
  c.updateVisibleJobCount = () => {};
  c.replaceJobCard({...c.selectedJob, exports: {burned_video: {status: 'completed'}}});
  assert.equal(c.selectedJob.exports.burned_video.status, 'completed');
});

test('SSE requests only a summary for a background task', async () => {
  const c = harness();
  c.install('refreshJobCard');
  c.allJobs = [];
  const urls = [], replaced = [];
  c.transport.get = async url => { urls.push(url); return {id:'background', summary_only:true}; };
  c.replaceJobCard = job => replaced.push(job);
  await c.refreshJobCard('background', 0);
  assert.deepEqual(urls, ['/api/jobs/background?summary=true']);
  assert.equal(replaced[0].summary_only, true);
});

test('selecting a task during summary fetch upgrades to complete detail', async () => {
  const c = harness();
  c.install('refreshJobCard');
  c.allJobs = [];
  const urls = [], replaced = [];
  c.transport.get = async url => {
    urls.push(url);
    c.selectedJob = {id:'background'};
    return {id:'background', summary_only:url.includes('?'), burned_video_current:!url.includes('?')};
  };
  c.replaceJobCard = job => replaced.push(job);
  await c.refreshJobCard('background', 0);
  assert.deepEqual(urls, ['/api/jobs/background?summary=true','/api/jobs/background']);
  assert.equal(replaced[0].summary_only, false);
  assert.equal(replaced[0].burned_video_current, true);
});

test('delivery does not expose a new workflow or API configuration', () => {
  const c = harness();
  c.renderReviewDelivery();
  assert.doesNotMatch(c.elements.get('#review-delivery').innerHTML,
    /剪映模式|烧录模式|API Key|Base URL|name="model"|name="proxy"/);
  c.selectedJob.burn_review_ready = false;
  c.renderReviewDelivery();
  assert.equal(c.elements.get('#review-delivery').hidden, true);
});

test('cancel and open-folder keep their bound job ID', async () => {
  const c = harness();
  const calls = [];
  c.api = async url => {calls.push(url); return {id: 'review-A'};};
  c.selectedJob = {id: 'list-B'};
  const event = {currentTarget: {dataset: {job: 'review-A'}}};
  await c.cancelBurnedVideo(event);
  await c.openJobFolder(event);
  assert.deepEqual(calls, ['/api/jobs/review-A/exports/burned-video/cancel',
    '/api/jobs/review-A/open-folder']);
  assert.equal(c.selectedJob.id, 'list-B');
});
