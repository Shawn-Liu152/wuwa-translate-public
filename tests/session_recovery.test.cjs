const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/static/workbench-core.js'), 'utf8');

function harness(respond) {
  const calls = [], events = [];
  const location = {hash:'', pathname:'/app', search:'?view=review&job=current-task'};
  const window = {
    location, history:{replaceState(_a,_b,url) { location.hash=''; this.url=url; }},
    setTimeout, clearTimeout, dispatchEvent: event => events.push(event.type),
  };
  vm.runInNewContext(source, {
    window, URLSearchParams, Headers, AbortController,
    CustomEvent: class { constructor(type) { this.type=type; } },
    fetch: async (url, options) => { calls.push({url,options}); return respond(url, options); },
  });
  return {core:window.SubtitleWorkbenchCore,window,calls,events};
}
const ok = data => new Response(JSON.stringify(data), {status:200});
const denied = (status=401, code='') => new Response(JSON.stringify({detail:'Denied',code}), {status});

test('missing cookie at initialization is session expiry, not an empty list', async () => {
  const h=harness(() => denied());
  await assert.rejects(h.core.apiRequest('/api/jobs'), {status:401});
  await assert.rejects(h.core.apiRequest('/api/jobs'), {status:401});
  assert.deepEqual(h.calls.map(c=>c.url), ['/api/session']);
  assert.deepEqual(h.events, ['subtitle-session-expired']);
});

test('rejected POST is never replayed; explicit reconnect refreshes CSRF and keeps task URL', async () => {
  let restored=false, posts=0;
  const h=harness((url,options) => {
    if(url==='/api/session') return ok({csrf_token:restored?'new-csrf':'old-csrf'});
    if(options.method==='POST') { posts++; return restored?ok({ok:true}):denied(); }
    return ok({id:'current-task'});
  });
  await assert.rejects(h.core.apiRequest('/api/jobs/current-task/export',{method:'POST'}), {status:401});
  restored=true;
  await h.core.reconnectLocalSession();
  assert.equal(posts,1);
  assert.equal(h.window.location.search,'?view=review&job=current-task');
  await h.core.apiRequest('/api/jobs/current-task/export',{method:'POST'});
  assert.equal(h.calls.at(-1).options.headers.get('X-Subtitle-CSRF'),'new-csrf');
  assert.equal(posts,2);
});

test('reconnect failure remains blocked without background traffic', async () => {
  const h=harness(()=>denied());
  await assert.rejects(h.core.apiRequest('/api/jobs'));
  await assert.rejects(h.core.reconnectLocalSession());
  await assert.rejects(h.core.apiRequest('/api/jobs'));
  assert.equal(h.calls.length,2);
  assert.equal(h.events.length,1);
});

test('CSRF rotation invalidates cached session without replaying mutation', async () => {
  const h=harness(url=>url==='/api/session'?ok({csrf_token:'csrf'}):denied(403,'local_csrf_invalid'));
  await assert.rejects(h.core.apiRequest('/api/jobs/current-task',{method:'PATCH'}), {status:403});
  assert.equal(h.events.length,1);
  assert.equal(h.calls.length,2);
});

test('ordinary forbidden response is not misclassified as session expiry', async () => {
  const h=harness(url=>url==='/api/session'?ok({csrf_token:'csrf'}):denied(403,'operation_blocked'));
  await assert.rejects(h.core.apiRequest('/api/jobs/current-task',{method:'POST'}), {status:403});
  assert.equal(h.events.length,0);
});

test('network failure is not misclassified as session expiry', async () => {
  const h=harness(()=>{throw new TypeError('offline');});
  await assert.rejects(h.core.apiRequest('/api/jobs'));
  assert.equal(h.events.length,0);
});

test('late failure from previous epoch cannot invalidate renewed session', async () => {
  let finish;
  const h=harness(url=>url==='/api/session'?ok({csrf_token:'csrf'}):new Promise(resolve=>{finish=resolve;}));
  const pending=h.core.apiRequest('/api/jobs/current-task',{method:'POST'});
  await new Promise(resolve=>setImmediate(resolve));
  await h.core.reconnectLocalSession();
  finish(denied());
  await assert.rejects(pending);
  assert.equal(h.events.length,0);
});

test('bootstrap fragment removed while current task stays in URL', async () => {
  const h=harness(()=>ok({csrf_token:'csrf'}));
  h.window.location.hash='#bootstrap=synthetic-test-only';
  await h.core.apiRequest('/api/jobs');
  assert.equal(h.window.location.hash,'');
  assert.equal(h.window.history.url,'/app?view=review&job=current-task');
  assert.equal(h.calls[0].url,'/api/session/bootstrap');
});
