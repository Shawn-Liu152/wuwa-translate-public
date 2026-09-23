const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/static/app.js'), 'utf8');

function functionSource(name) {
  const match = source.match(new RegExp(`function ${name}\\([^]*?^}`, 'm'));
  assert.ok(match, `${name} exists`);
  return match[0];
}

test('ordinary information is not presented as a successful operation', () => {
  const makeElement = () => ({
    children: [],
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute() {},
  });
  const toast = makeElement();
  const context = {
    $: () => toast,
    document: {createElement: makeElement},
    clearTimeout() {},
    setTimeout: () => 1,
    toastTimer: null,
  };
  vm.createContext(context);
  vm.runInContext(functionSource('showToast'), context);

  context.showToast('没有任务被归档', 'info');

  assert.equal(toast.className, 'toast toast-info');
  assert.equal(toast.children[1].children[0].textContent, '提示');
  assert.equal(toast.children[1].children[1].textContent, '没有任务被归档');
});

test('batch retry asks for a saved key before confirmation or API mutation', async () => {
  const calls = [];
  const context = {
    allJobs: [{id: 'failed-job', status: 'failed', error_code: 'api_error', options: {dry_run: false}, inputs: {}}],
    userSettings: {api_key_configured: false},
    showToast: (message, kind) => calls.push({type: 'toast', message, kind}),
    confirmAction: async () => { calls.push({type: 'confirm'}); return true; },
    api: async () => { calls.push({type: 'api'}); return {}; },
    window: {},
  };
  vm.createContext(context);
  vm.runInContext(functionSource('batchRetryEligibility'), context);
  const action = source.match(/window\.batchJobAction = async function batchJobAction\([^]*?^};/m);
  assert.ok(action, 'batch action exists');
  vm.runInContext(action[0], context);

  await context.window.batchJobAction('retry', ['failed-job']);

  assert.deepEqual(calls.map(call => call.type), ['toast']);
  assert.equal(calls[0].kind, 'warning');
  assert.match(calls[0].message, /保存 API Key/);
});
