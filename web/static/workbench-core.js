/* Shared infrastructure for the subtitle workbench.
   Business rules stay in app.js; transport and modal behavior live here. */
(() => {
  'use strict';

  class ApiError extends Error {
    constructor(message, { status = 0, code = '', retryable = false } = {}) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
      this.retryable = retryable;
    }
  }

  const wait = milliseconds => new Promise(resolve => {
    window.setTimeout(resolve, milliseconds);
  });

  let localSessionPromise = null;
  let csrfToken = '';
  let sessionExpired = false;
  let sessionEpoch = 0;

  function expireLocalSession(error, epoch) {
    if (epoch !== sessionEpoch || !(error?.status === 401
        || (error?.status === 403 && error.code === 'local_csrf_invalid'))) return;
    localSessionPromise = null;
    csrfToken = '';
    if (sessionExpired) return;
    sessionExpired = true;
    window.dispatchEvent(new CustomEvent('subtitle-session-expired'));
  }

  async function reconnectLocalSession() {
    const epoch = ++sessionEpoch;
    localSessionPromise = null;
    csrfToken = '';
    try {
      await establishLocalSession();
      sessionExpired = false;
      localSessionPromise = Promise.resolve();
    } catch (error) {
      expireLocalSession(error, epoch);
      throw error;
    }
  }

  async function parseError(response) {
    const raw = await response.text();
    let payload = null;
    try {
      payload = JSON.parse(raw);
    } catch (_) {
      payload = null;
    }
    const message = payload?.detail || payload?.error || raw
      || `请求失败（HTTP ${response.status}）`;
    return new ApiError(String(message), {
      status: response.status,
      code: String(payload?.code || ''),
      retryable: response.status === 429 || response.status >= 500,
    });
  }

  function takeBootstrapFromFragment() {
    const parameters = new URLSearchParams(window.location.hash.slice(1));
    const bootstrap = String(parameters.get('bootstrap') || '').trim();
    if (bootstrap) {
      // APP-AUTH-001: fragments never reach HTTP logs; remove it immediately
      // so it is not retained in copied URLs or normal browser history.
      window.history.replaceState(
        null,
        '',
        `${window.location.pathname}${window.location.search}`,
      );
    }
    return bootstrap;
  }

  async function establishLocalSession() {
    const bootstrap = takeBootstrapFromFragment();
    const response = await fetch(
      bootstrap ? '/api/session/bootstrap' : '/api/session',
      bootstrap
        ? {
          method: 'POST',
          headers: { 'X-Subtitle-Bootstrap': bootstrap },
          credentials: 'same-origin',
          cache: 'no-store',
        }
        : {
          method: 'GET',
          credentials: 'same-origin',
          cache: 'no-store',
        },
    );
    if (!response.ok) throw await parseError(response);
    const payload = await response.json();
    csrfToken = String(payload?.csrf_token || '');
    if (!csrfToken) {
      throw new ApiError('工作台安全会话初始化失败，请关闭页面后重新启动', {
        status: 401,
        code: 'local_session_invalid',
      });
    }
  }

  function ensureLocalSession() {
    if (sessionExpired) return Promise.reject(new ApiError('会话已失效，请重新连接', {
      status: 401, code: 'local_session_expired',
    }));
    if (!localSessionPromise) {
      const epoch = sessionEpoch;
      localSessionPromise = establishLocalSession().catch(error => {
        if (epoch === sessionEpoch) localSessionPromise = null;
        expireLocalSession(error, epoch);
        throw error;
      });
    }
    return localSessionPromise;
  }

  async function apiRequest(path, options = {}) {
    const epoch = sessionEpoch;
    const method = String(options.method || 'GET').toUpperCase();
    const timeoutMs = Number(options.timeoutMs || 60000);
    const attempts = method === 'GET' ? 2 : 1;
    let lastError = null;

    if (String(path).startsWith('/api/')) await ensureLocalSession();

    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
      try {
        const headers = new Headers(options.headers || {});
        if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
          headers.set('X-Subtitle-CSRF', csrfToken);
        }
        const response = await fetch(path, {
          ...options,
          headers,
          credentials: 'same-origin',
          signal: options.signal || controller.signal,
        });
        if (!response.ok) throw await parseError(response);
        if (response.status === 204) return null;
        return await response.json();
      } catch (error) {
        expireLocalSession(error, epoch);
        if (error?.name === 'AbortError') {
          lastError = new ApiError(
            '请求等待时间过长，请检查本地服务或网络后重试',
            { code: 'request_timeout', retryable: method === 'GET' },
          );
        } else if (error instanceof ApiError) {
          lastError = error;
        } else {
          lastError = new ApiError(
            '无法连接本地服务，请确认工作台仍在运行',
            { code: 'network_error', retryable: method === 'GET' },
          );
        }
        if (attempt + 1 < attempts && lastError.retryable) {
          await wait(250 * (attempt + 1));
          continue;
        }
        throw lastError;
      } finally {
        window.clearTimeout(timeout);
      }
    }
    throw lastError;
  }

  let activeDialogResolve = null;

  function closeActiveDialog(value) {
    if (activeDialogResolve) {
      activeDialogResolve(value);
      activeDialogResolve = null;
    }
  }

  function confirmAction({
    title = '确认操作',
    message = '',
    confirmLabel = '确认',
    cancelLabel = '取消',
    danger = false,
  } = {}) {
    const dialog = document.getElementById('confirm-dialog');
    if (!dialog || typeof dialog.showModal !== 'function') {
      return Promise.resolve(window.confirm(message || title));
    }
    if (dialog.open) dialog.close('cancel');
    dialog.querySelector('#confirm-title').textContent = title;
    dialog.querySelector('#confirm-message').textContent = message;
    const accept = dialog.querySelector('#confirm-accept');
    const cancel = dialog.querySelector('#confirm-cancel');
    accept.textContent = confirmLabel;
    cancel.textContent = cancelLabel;
    accept.classList.toggle('danger', danger);

    return new Promise(resolve => {
      activeDialogResolve = resolve;
      dialog.addEventListener('close', () => {
        closeActiveDialog(dialog.returnValue === 'confirm');
      }, { once: true });
      dialog.addEventListener('cancel', event => {
        event.preventDefault();
        dialog.close('cancel');
      }, { once: true });
      dialog.showModal();
    });
  }

  function debounce(callback, delay = 180) {
    let timer = 0;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => callback(...args), delay);
    };
  }

  /* ---- Render scheduler -----------------------------------------------
     Coalesces many state updates into a single animation frame so
     high-frequency SSE progress events cannot queue up DOM work.
     Keying by a render target makes the last-scheduled task win. */
  function createScheduler({ raf = null, timer = null } = {}) {
    const scheduleFrame = raf
      || (typeof window !== 'undefined'
        && typeof window.requestAnimationFrame === 'function'
        ? callback => window.requestAnimationFrame(callback)
        : callback => (timer || setTimeout)(callback, 16));
    const pending = new Map();
    let frameHandle = 0;
    let flushCount = 0;

    function flush() {
      frameHandle = 0;
      if (!pending.size) return;
      const work = new Map(pending);
      pending.clear();
      flushCount += 1;
      work.forEach(task => {
        try {
          task();
        } catch (error) {
          if (typeof console !== 'undefined') console.error('[scheduler]', error);
        }
      });
    }

    return {
      schedule(key, task) {
        pending.set(key, task);
        if (frameHandle) return;
        frameHandle = scheduleFrame(flush);
      },
      cancel(key) {
        pending.delete(key);
      },
      get pendingSize() {
        return pending.size;
      },
      get flushCount() {
        return flushCount;
      },
    };
  }

  /* ---- Transport -------------------------------------------------------
     GET dedupe by resource key: concurrent callers share one in-flight
     promise; cancel() aborts a superseded request. Mutations are never
     coalesced. */
  function createTransport({ request } = {}) {
    if (typeof request !== 'function') {
      throw new TypeError('createTransport 需要 request 函数');
    }
    const inflight = new Map();
    let requestCount = 0;
    let coalescedCount = 0;

    function get(path, options = {}) {
      const key = options.key || path;
      const existing = inflight.get(key);
      if (existing) {
        coalescedCount += 1;
        return existing.promise;
      }
      requestCount += 1;
      const controller = new AbortController();
      const entry = {
        controller,
        promise: request(path, { ...options, signal: controller.signal }),
      };
      inflight.set(key, entry);
      entry.promise.then(
        () => {
          if (inflight.get(key) === entry) inflight.delete(key);
        },
        () => {
          if (inflight.get(key) === entry) inflight.delete(key);
        },
      );
      return entry.promise;
    }

    return {
      get,
      cancel(key) {
        const entry = inflight.get(key);
        if (!entry) return false;
        try {
          entry.controller.abort();
        } catch (_) {
          /* already settled */
        }
        inflight.delete(key);
        return true;
      },
      isInflight(key) {
        return inflight.has(key);
      },
      stats() {
        return {
          inflight: inflight.size,
          requestCount,
          coalescedCount,
        };
      },
    };
  }

  /* ---- Store -----------------------------------------------------------
     Tiny observable snapshot; app.js keeps owning the state shape, this
     only centralises read/write and notification. */
  function createStore(initialState = {}) {
    let state = { ...initialState };
    const listeners = new Set();
    return {
      get() {
        return state;
      },
      set(patch) {
        const next = typeof patch === 'function' ? patch(state) : patch;
        state = { ...state, ...next };
        listeners.forEach(listener => {
          try {
            listener(state);
          } catch (error) {
            if (typeof console !== 'undefined') console.error('[store]', error);
          }
        });
        return state;
      },
      subscribe(listener) {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    };
  }

  window.SubtitleWorkbenchCore = Object.freeze({
    ApiError,
    apiRequest,
    reconnectLocalSession,
    confirmAction,
    debounce,
    createScheduler,
    createTransport,
    createStore,
  });
})();
