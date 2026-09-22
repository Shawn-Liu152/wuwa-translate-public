/**
 * 阶段 D — 本地回环视觉验证（真实 Chrome，经 playwright-core channel 直连，
 * 不下载任何浏览器内核）。
 *
 * 验收项（审计文档 5 节阶段 D）：
 *   - 会话 bootstrap 成功（任务列表不再 401）
 *   - 1440x1000 / 390x844 均无横向溢出
 *   - 一级导航切换正常（工作台 / 风险审校 / 设置）
 *   - 任务详情 5 个标签可切换
 *
 * E-1 追加验收项：
 *   - 失败横幅透出错误码 / 上游状态 / 原因 / 建议动作
 *   - 上游原因文案被当纯文本转义，不能变成真节点
 *   - 两个视口挂上横幅后仍无横向溢出
 *   - 点“查看处理办法”跳到 FAQ 并展开对应条目
 *
 * 运行（Git Bash）：
 *   NODE_PATH 不适用于 ESM，用 createRequire 指向托管 node workspace。
 */

import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { existsSync, promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PY = process.env.PY || 'python';
const PORT = Number(process.env.VISUAL_PORT || 8799);
const CHROME_CANDIDATES = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
];

// NODE_PATH 不适用于 ESM，只能靠 createRequire 锚定一个能解析到
// playwright-core 的位置。锚点按优先级探测，避免写死任何机器专属路径。
const REQUIRE_ANCHORS = [
  process.env.NODE_WORKSPACE,
  path.join(ROOT, 'package.json'),
  path.join(os.homedir(), '.workbuddy', 'binaries', 'node', 'workspace', 'package.json'),
].filter(Boolean);

function loadPlaywright() {
  for (const anchor of REQUIRE_ANCHORS) {
    try {
      return createRequire(anchor)('playwright-core');
    } catch {
      // 该锚点解析不到，试下一个
    }
  }
  throw new Error(
    '找不到 playwright-core；请设 NODE_WORKSPACE 指向装有它的 node workspace',
  );
}

const { chromium } = loadPlaywright();

const results = { checks: [], screenshots: [] };
const check = (name, passed, detail = '') => {
  results.checks.push({ name, passed, detail });
  console.log(`  ${passed ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
};

async function waitServer(url, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch (_) { /* 服务未就绪 */ }
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  throw new Error('本地服务未就绪');
}

const server = spawn(PY, [
  path.join(ROOT, 'tools', 'visual_server.py'),
  String(PORT),
  path.join(ROOT, '.visual_token.txt'),
], { cwd: ROOT, stdio: 'ignore' });

const executablePath = CHROME_CANDIDATES.find(candidate => existsSync(candidate));
await fs.mkdir(path.join(ROOT, 'results', 'visual'), { recursive: true });
try {
  if (!executablePath) throw new Error('未找到本机 Chrome/Edge');
  const base = `http://127.0.0.1:${PORT}`;
  await waitServer(`${base}/app`);
  const bootstrap = (await fs.readFile(path.join(ROOT, '.visual_token.txt'), 'utf8')).trim();
  const workbenchUrl = `${base}/app#bootstrap=${bootstrap}`;

  const browser = await chromium.launch({ executablePath, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();

  /* --- 会话 bootstrap --- */
  const jobsResponse = page.waitForResponse(
    response => response.url().includes('/api/jobs')
      && !response.url().includes('stream'),
    { timeout: 20000 },
  ).catch(() => null);
  await page.goto(workbenchUrl, { waitUntil: 'domcontentloaded' });
  const jobs = await jobsResponse;
  check('会话 bootstrap 成功（/api/jobs 为 200）', Boolean(jobs && jobs.status() === 200),
    jobs ? `HTTP ${jobs.status()}` : '未捕获到 /api/jobs 请求');

  await page.waitForTimeout(1500);
  /* 以最终状态为准：只检查 #jobs-list 内部（FAQ/Guide 文案里也含同名短语） */
  const finalState = await page.evaluate(() => {
    const list = document.getElementById('jobs-list');
    return {
      errorVisible: Boolean(list?.querySelector('.error-state')),
      jobCards: list?.querySelectorAll('.job[data-id]').length || 0,
      emptyState: Boolean(list?.querySelector('.empty:not(.error-state)')),
    };
  });
  check('任务列表最终状态健康',
    !finalState.errorVisible && (finalState.jobCards > 0 || finalState.emptyState),
    `任务卡=${finalState.jobCards} 空态=${finalState.emptyState}`);

  /* --- 桌面视口 --- */
  await page.screenshot({ path: path.join(ROOT, 'results', 'visual', 'workbench_desktop.png') });
  results.screenshots.push('workbench_desktop.png');
  const desktopOverflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  check('桌面 1440 无横向溢出', desktopOverflow.scroll <= desktopOverflow.client + 1,
    `scrollWidth=${desktopOverflow.scroll} clientWidth=${desktopOverflow.client}`);

  /* --- 一级导航：风险审校 --- */
  await page.click('[data-view="review"]');
  await page.waitForTimeout(900);
  const reviewActive = await page.evaluate(() =>
    document.getElementById('review')?.classList.contains('active'));
  check('风险审校视图可打开', Boolean(reviewActive));
  await page.screenshot({ path: path.join(ROOT, 'results', 'visual', 'review_desktop.png') });
  results.screenshots.push('review_desktop.png');

  /* --- 设置视图 --- */
  await page.click('[data-view="settings"]');
  await page.waitForTimeout(600);
  const settingsActive = await page.evaluate(() =>
    document.getElementById('settings')?.classList.contains('active'));
  check('设置视图可打开', Boolean(settingsActive));

  /* --- 详情标签（回到工作台；无任务时详情为空态，仅在有任务时测试切换）--- */
  await page.click('[data-view="workbench"]');
  await page.waitForTimeout(600);
  const hasJobCard = await page.locator('#jobs-list .job[data-id]').count();
  const tabNames = ['overview', 'pipeline', 'log', 'files', 'risk'];
  if (hasJobCard) {
    let tabsOk = true;
    for (const tab of tabNames) {
      const button = page.locator(`[data-detail-tab="${tab}"]`);
      if (await button.count()) {
        await button.click();
        await page.waitForTimeout(150);
        const active = await page.evaluate(
          name => document.querySelector(`#detail-tab-${name}`)?.getAttribute('aria-selected'),
          tab,
        );
        if (active !== 'true') tabsOk = false;
      } else {
        tabsOk = false;
      }
    }
    check('详情 5 个标签可切换', tabsOk);
    await page.locator('[data-detail-tab="risk"]').click();
    await page.waitForTimeout(400);
  } else {
    const emptyPanels = await page.evaluate(names => names.every(
      name => document.getElementById(`detail-panel-${name}`)?.textContent?.length > 0,
    ), tabNames);
    check('无任务时详情 5 个面板均渲染空态', emptyPanels);
  }
  await page.screenshot({ path: path.join(ROOT, 'results', 'visual', 'detail_risk_desktop.png') });
  results.screenshots.push('detail_risk_desktop.png');

  /* --- E-1：失败态错误横幅 ---
     先看真实链路：任务列表里真正失败的任务卡应当已经带横幅。
     探针包在 #e1-banner-probe 里，用直接子选择器即可把它排除掉。 */
  const realBanners = await page.evaluate(() => {
    const wraps = Array.from(document.querySelectorAll('#jobs-list .job-wrap'));
    const failed = wraps.filter(wrap => wrap.querySelector('.job[data-status="failed"]'));
    const banners = wraps
      .map(wrap => wrap.querySelector(':scope > .job-error-banner'))
      .filter(Boolean);
    const first = banners[0];
    return {
      failedCards: failed.length,
      banners: banners.length,
      code: first?.querySelector('.job-error-code')?.textContent || '',
      reason: first?.querySelector('.job-error-reason')?.textContent || '',
      action: first?.querySelector('.job-error-action, .job-error-action-text')?.textContent || '',
      faq: Boolean(first?.querySelector('.job-error-faq')),
    };
  });
  if (realBanners.failedCards) {
    check('E-1 真实失败任务卡都带横幅，且错误码/原因/建议动作齐全',
      realBanners.banners === realBanners.failedCards
        && realBanners.code.length > 0
        && realBanners.reason.length > 0
        && realBanners.action.length > 0
        && realBanners.faq,
      JSON.stringify(realBanners));
    const realBanner = page.locator('#jobs-list .job-wrap > .job-error-banner').first();
    await realBanner.scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    await realBanner.screenshot({ path: path.join(ROOT, 'results', 'visual', 'error_banner_real_job.png') });
    results.screenshots.push('error_banner_real_job.png');
  } else {
    console.log('  SKIP  E-1 真实失败任务卡（当前列表没有失败任务）');
  }

  /* 用页面自己的 renderErrorBanner 补一条固定用例（app.js 是经典脚本，函数即全局），
     不在验证脚本里复制标记，否则测的是副本而不是出货代码。 */
  const banner = await page.evaluate(() => {
    const list = document.getElementById('jobs-list');
    const host = list?.querySelector('.job-wrap') || list;
    if (!host) return null;
    const probe = document.createElement('div');
    probe.id = 'e1-banner-probe';
    probe.innerHTML = renderErrorBanner({
      status: 'failed',
      error_code: 'authentication_error',
      error: 'API Key 无效、已过期或无权访问当前模型',
      upstream_status: 401,
    });
    host.appendChild(probe);
    const node = probe.querySelector('.job-error-banner');
    return {
      rendered: Boolean(node),
      code: node?.querySelector('.job-error-code')?.textContent || '',
      upstream: node?.querySelector('.job-error-status')?.textContent || '',
      reason: node?.querySelector('.job-error-reason')?.textContent || '',
      action: node?.querySelector('.job-error-action')?.textContent || '',
      faqCode: node?.querySelector('.job-error-faq')?.dataset?.faqCode || '',
    };
  });
  check('E-1 横幅透出错误码 / 上游状态 / 原因 / 建议动作',
    banner?.rendered === true
      && banner.code === 'authentication_error'
      && banner.upstream === '上游 HTTP 401'
      && banner.reason.includes('API Key')
      && banner.action.length > 0
      && banner.faqCode === 'authentication_error',
    JSON.stringify(banner));

  /* 原因文案来自上游，可能是恶意接口回显：必须整段当纯文本，不能变成真节点。 */
  const escaped = await page.evaluate(() => {
    const probe = document.createElement('div');
    probe.innerHTML = renderErrorBanner({
      status: 'failed',
      error_code: 'internal_error',
      error: '<img src=x onerror="window.__e1_pwned=1">操作失败',
    });
    document.body.appendChild(probe);
    const outcome = {
      liveNode: Boolean(probe.querySelector('img')),
      text: probe.querySelector('.job-error-reason')?.textContent || '',
      pwned: Boolean(window.__e1_pwned),
    };
    probe.remove();
    return outcome;
  });
  check('E-1 横幅把上游原因当纯文本转义',
    escaped.liveNode === false && escaped.pwned === false && escaped.text.includes('<img'),
    JSON.stringify(escaped));

  const bannerOverflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  check('E-1 桌面 1440 挂横幅后仍无横向溢出',
    bannerOverflow.scroll <= bannerOverflow.client + 1,
    `scrollWidth=${bannerOverflow.scroll} clientWidth=${bannerOverflow.client}`);
  await page.screenshot({ path: path.join(ROOT, 'results', 'visual', 'error_banner_desktop.png') });
  results.screenshots.push('error_banner_desktop.png');

  /* 整页截图在窄屏会把横幅推到折叠线以下，另存一张元素级截图供肉眼验收。 */
  const desktopProbe = page.locator('#e1-banner-probe .job-error-banner');
  await desktopProbe.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await desktopProbe.screenshot({ path: path.join(ROOT, 'results', 'visual', 'error_banner_desktop_element.png') });
  results.screenshots.push('error_banner_desktop_element.png');

  /* authentication_error 带 actionView，建议动作是个真按钮，点了必须切到设置页。 */
  await page.locator('#e1-banner-probe .job-error-action').click();
  await page.waitForTimeout(900);
  const actionJump = await page.evaluate(() => ({
    settings: document.getElementById('settings')?.classList.contains('active') === true,
    workbench: document.getElementById('workbench')?.classList.contains('active') === true,
  }));
  check('E-1 点建议动作按钮跳到设置页', actionJump.settings && !actionJump.workbench,
    JSON.stringify(actionJump));
  await page.click('[data-view="workbench"]');
  await page.waitForTimeout(700);

  await page.locator('#e1-banner-probe .job-error-faq').click();
  await page.waitForTimeout(1400);
  const faqJump = await page.evaluate(() => {
    const item = document.querySelector('.faq-item[data-faq-code="authentication_error"]');
    return {
      faqActive: document.getElementById('faq')?.classList.contains('active') === true,
      found: Boolean(item),
      open: item?.open === true,
      visible: Boolean(item && !item.hidden && item.offsetParent !== null),
      searchCleared: document.getElementById('faq-search')?.value === '',
    };
  });
  check('E-1 点“查看处理办法”跳到 FAQ 并展开对应条目',
    faqJump.faqActive && faqJump.found && faqJump.open && faqJump.visible && faqJump.searchCleared,
    JSON.stringify(faqJump));

  /* --- 窄屏视口 --- */
  await page.setViewportSize({ width: 390, height: 844 });
  await page.click('[data-view="workbench"]');
  await page.waitForTimeout(900);
  const mobileOverflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  check('窄屏 390 无横向溢出', mobileOverflow.scroll <= mobileOverflow.client + 1,
    `scrollWidth=${mobileOverflow.scroll} clientWidth=${mobileOverflow.client}`);
  await page.screenshot({ path: path.join(ROOT, 'results', 'visual', 'workbench_mobile.png') });
  results.screenshots.push('workbench_mobile.png');

  /* E-1：桌面那个探针在切视图后仍然存在，所以窄屏要先清掉再重挂，
     否则同 id 出现两个节点，locator 会撞 strict mode。
     这条专门守 flex-wrap + min-width:0，防止横幅在 390px 撑出横向滚动。 */
  await page.evaluate(() => {
    document.getElementById('e1-banner-probe')?.remove();
    const list = document.getElementById('jobs-list');
    const host = list?.querySelector('.job-wrap') || list;
    if (!host) return;
    const probe = document.createElement('div');
    probe.id = 'e1-banner-probe';
    probe.innerHTML = renderErrorBanner({
      status: 'failed',
      error_code: 'translation_batch_error',
      error: '翻译批次失败，请重试',
    });
    host.appendChild(probe);
  });
  await page.waitForTimeout(400);
  const mobileBannerOverflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  check('E-1 窄屏 390 挂横幅后仍无横向溢出',
    mobileBannerOverflow.scroll <= mobileBannerOverflow.client + 1,
    `scrollWidth=${mobileBannerOverflow.scroll} clientWidth=${mobileBannerOverflow.client}`);
  await page.screenshot({ path: path.join(ROOT, 'results', 'visual', 'error_banner_mobile.png') });
  results.screenshots.push('error_banner_mobile.png');

  const mobileProbe = page.locator('#e1-banner-probe .job-error-banner');
  await mobileProbe.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await mobileProbe.screenshot({ path: path.join(ROOT, 'results', 'visual', 'error_banner_mobile_element.png') });
  results.screenshots.push('error_banner_mobile_element.png');

  await browser.close();
} catch (error) {
  check('验证流程完成', false, String(error));
} finally {
  server.kill();
}

await fs.writeFile(
  path.join(ROOT, 'results', 'visual', 'report.json'),
  JSON.stringify(results, null, 2),
  'utf8',
);
const failed = results.checks.filter(item => !item.passed).length;
console.log(failed ? `\n${failed} 项失败` : `\n全部 ${results.checks.length} 项通过`);
process.exit(failed ? 1 : 0);
