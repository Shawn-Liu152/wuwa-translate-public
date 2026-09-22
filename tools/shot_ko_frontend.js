const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const consoleErrors = [];
  page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
  page.on('pageerror', err => consoleErrors.push(String(err)));

  await page.goto('http://127.0.0.1:8768/app', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);

  // 1) URL 创建任务表单：源语言选韩语
  await page.selectOption('#source-language', 'ko');
  await page.waitForTimeout(400);
  await page.screenshot({ path: 'output/ko_front_01_url_form.png' });

  // 2) 工作台全景：任务列表（应含韩语任务卡）
  await page.screenshot({ path: 'output/ko_front_02_workbench.png' });

  // 3) 风险审校视图
  await page.click('[data-view="review"]').catch(() => {});
  await page.waitForTimeout(1800);
  await page.screenshot({ path: 'output/ko_front_03_review.png' });

  // 4) 术语表视图（韩语筛选）
  await page.click('[data-view="glossary"]').catch(() => {});
  await page.waitForTimeout(800);
  await page.selectOption('#glossary-source-language', 'ko', { force: true }).catch(() => {});
  await page.waitForTimeout(600);
  await page.screenshot({ path: 'output/ko_front_04_glossary.png' });

  // DOM 断言：4 处 select 都含 ko 选项
  const koOptions = await page.evaluate(() => ({
    urlForm: [...document.querySelectorAll('#source-language option')].map(o => o.value),
    uploadForm: [...document.querySelectorAll('#direct-source-language option')].map(o => o.value),
    glossary: [...document.querySelectorAll('#glossary-source-language option')].map(o => o.value),
    prompt: [...document.querySelectorAll('#prompt-source-language option')].map(o => o.value),
    jobCardsKo: [...document.querySelectorAll('[data-job-id], .job-card, .task-card')]
      .filter(c => (c.textContent || '').includes('韩语')).length,
  }));
  console.log('KO 选项检查:', JSON.stringify(koOptions));
  console.log('console errors:', consoleErrors.length ? consoleErrors.slice(0, 5) : 'none');
  await browser.close();
})();
