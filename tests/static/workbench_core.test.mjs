/**
 * workbench-core 基础设施单元测试（Node 直接运行，无浏览器依赖）
 *
 * 对应审计文档 FE-P0-03 的验收标准：
 *   1. 连续 100 次 progress 事件只产生一次批量渲染帧
 *   2. 同一资源同时只有一个活跃 GET（并发去重）
 *   3. 切换任务后旧请求可被取消，不会写入新任务
 *
 * 运行：node tests/static/workbench_core.test.mjs
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));

/* 最小 window 桩：workbench-core 只在调用期访问 window 的成员 */
globalThis.window = {
  setTimeout: (callback, ms) => setTimeout(callback, ms),
};

const source = readFileSync(join(here, '../../web/static/workbench-core.js'), 'utf8');
/* IIFE 会把 SubtitleWorkbenchCore 挂到 globalThis.window 上 */
new Function(source)();

const Core = globalThis.window.SubtitleWorkbenchCore;
const tick = ms => new Promise(resolve => setTimeout(resolve, ms));

/* ------------------------------------------------------------------ */

async function testSchedulerCoalescesBurst() {
  const scheduler = Core.createScheduler();
  let renders = 0;
  let lastValue = 0;

  for (let i = 1; i <= 100; i += 1) {
    scheduler.schedule('progress', () => {
      renders += 1;
      lastValue = i;
    });
  }

  assert.equal(scheduler.pendingSize, 1, '同键任务应互相覆盖');
  await tick(40);

  assert.equal(renders, 1, '验收：100 次事件只产生一次渲染');
  assert.equal(scheduler.flushCount, 1, '验收：只有一帧');
  assert.equal(lastValue, 100, '取最新值');
  assert.equal(scheduler.pendingSize, 0, 'flush 后队列为空');
}

async function testSchedulerCoalescesDifferentKeysIntoOneFrame() {
  const scheduler = Core.createScheduler();
  const rendered = [];

  scheduler.schedule('detail', () => rendered.push('detail'));
  scheduler.schedule('jobs-list', () => rendered.push('jobs-list'));
  scheduler.schedule('detail', () => rendered.push('detail-2'));

  await tick(40);
  assert.equal(scheduler.flushCount, 1, '三个任务同一帧');
  /* 同键覆盖（last wins）：detail 被 detail-2 替换，只执行一次 */
  assert.deepEqual(rendered.sort(), ['detail-2', 'jobs-list']);
}

async function testSchedulerFallsBackToTimer() {
  let frameCalls = 0;
  const scheduler = Core.createScheduler({
    raf: callback => {
      frameCalls += 1;
      return setTimeout(callback, 5);
    },
  });
  scheduler.schedule('x', () => {});
  await tick(30);
  assert.equal(frameCalls, 1, '自定义 raf 被使用');
}

async function testTransportDedupesConcurrentGets() {
  let fetches = 0;
  const transport = Core.createTransport({
    request: async path => {
      fetches += 1;
      await tick(20);
      return { path };
    },
  });

  const [a, b, c] = await Promise.all([
    transport.get('/api/jobs'),
    transport.get('/api/jobs'),
    transport.get('/api/jobs'),
  ]);

  assert.equal(fetches, 1, '验收：并发 GET 只发一次请求');
  assert.equal(a.path, '/api/jobs');
  assert.deepEqual([b.path, c.path], ['/api/jobs', '/api/jobs']);
  const stats = transport.stats();
  assert.equal(stats.coalescedCount, 2, '两次调用复用在飞请求');
  assert.equal(stats.inflight, 0, '完成后清空');
}

async function testTransportSeparatesDistinctKeys() {
  let fetches = 0;
  const transport = Core.createTransport({
    request: async path => {
      fetches += 1;
      await tick(10);
      return { path };
    },
  });

  const results = await Promise.all([
    transport.get('/api/jobs/1', { key: 'job:1' }),
    transport.get('/api/jobs/1', { key: 'job:1' }),
    transport.get('/api/jobs/2', { key: 'job:2' }),
  ]);

  assert.equal(fetches, 2, 'job:1 去重、job:2 独立');
  assert.equal(results[2].path, '/api/jobs/2');
}

async function testTransportCancelSupersededRequest() {
  let aborted = 0;
  let settled = 0;
  const transport = Core.createTransport({
    request: async (path, { signal } = {}) => {
      await tick(50);
      if (signal?.aborted) {
        aborted += 1;
        throw new Error('aborted');
      }
      settled += 1;
      return { path };
    },
  });

  const first = transport.get('/api/jobs/1', { key: 'job:1' });
  assert.equal(transport.isInflight('job:1'), true);

  transport.cancel('job:1');
  assert.equal(transport.isInflight('job:1'), false, '取消后立即出队');

  await assert.rejects(first, /aborted/, '旧请求被中止');
  await tick(10);
  assert.equal(aborted, 1);
  assert.equal(settled, 0, '验收：被取消的请求不会写入状态');

  const second = await transport.get('/api/jobs/1', { key: 'job:1' });
  assert.equal(second.path, '/api/jobs/1', '取消后可重新请求');
}

async function testTransportNeverCoalescesWhenSettled() {
  let fetches = 0;
  const transport = Core.createTransport({
    request: async () => {
      fetches += 1;
      return { ok: true };
    },
  });

  await transport.get('/api/jobs');
  await transport.get('/api/jobs');
  assert.equal(fetches, 2, '已完成的请求不参与去重');
}

/* ------------------------------------------------------------------ */

const tests = [
  testSchedulerCoalescesBurst,
  testSchedulerCoalescesDifferentKeysIntoOneFrame,
  testSchedulerFallsBackToTimer,
  testTransportDedupesConcurrentGets,
  testTransportSeparatesDistinctKeys,
  testTransportCancelSupersededRequest,
  testTransportNeverCoalescesWhenSettled,
];

let failed = 0;
for (const test of tests) {
  try {
    await test();
    console.log(`  PASS  ${test.name}`);
  } catch (error) {
    failed += 1;
    console.error(`  FAIL  ${test.name}`);
    console.error(`        ${error.message}`);
  }
}

console.log(failed ? `\n${failed} failed` : `\n${tests.length} passed`);
process.exit(failed ? 1 : 0);
