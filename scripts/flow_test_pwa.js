#!/usr/bin/env node
/* Real-PWA flow test against the isolated server started by
   scripts/flow_test_request_flow.py --browser.

   Mode A (native): injects an EvieShell-shaped bridge whose mic_read streams
   100 ms PCM chunks; web capture is poisoned. Proves the PWA takes the native
   path end-to-end (no getUserMedia) and streams frames over the live WS.

   Mode B (web): no bridge; Chrome's fake media device feeds the real
   getUserMedia -> AudioWorklet -> WS path.

   Emits one line per step:  FLOW {"step":...,"ok":true,"detail":"..."}
*/
const { chromium } = require('playwright');

const BASE = process.env.EV_FLOW_BASE || 'http://127.0.0.1:8100';
const TOKEN = process.env.EV_FLOW_TOKEN || '';
const TIMEOUT = 45000;

let failed = 0;
function step(name, ok, detail) {
  if (!ok) failed += 1;
  process.stdout.write('FLOW ' + JSON.stringify({ step: name, ok: !!ok, detail: String(detail || '') }) + '\n');
}

const NATIVE_MOCK = `
  window.__mock = { micReads: 0, micStops: 0, gumCalls: 0, micStarts: 0 };
  (function () {
    function enc(bytes) {
      var s = '';
      for (var i = 0; i < bytes.length; i += 1) s += String.fromCharCode(bytes[i]);
      return btoa(s);
    }
    function pcmChunk(seq) {
      var n = 1600, b = new Uint8Array(n * 2);
      for (var i = 0; i < n; i += 1) {
        var v = Math.round(5000 * Math.sin(2 * Math.PI * 220 * (seq * n + i) / 16000));
        b[i * 2] = v & 0xff; b[i * 2 + 1] = (v >> 8) & 0xff;
      }
      return enc(b);
    }
    window.EvieNativeShell = {
      version: 'flow-test',
      capabilities: ['foreground_voice', 'camera', 'text', 'notification', 'microphone', 'native_microphone'],
      post: function (payload) {
        return new Promise(function (resolve) {
          var t = payload && payload.type;
          if (t === 'capabilities') return resolve({
            ok: true,
            endpoint_capabilities: ['foreground_voice','camera','text','notification','microphone','location','clipboard','native_microphone'],
            permissions: { microphone: 'granted' },
            hardware: { model: 'iPhone17,1' }
          });
          if (t === 'requestPermission') return resolve({ ok: true, permission: 'granted' });
          if (t === 'mic_start') { window.__mock.micStarts += 1; return resolve({ ok: true, sample_rate: 16000, frame_ms: 100 }); }
          if (t === 'mic_read') {
            window.__mock.micReads += 1;
            var seq = window.__mock.micReads;
            return setTimeout(function () {
              resolve({ ok: true, pcm_b64: pcmChunk(seq), samples: 1600, sample_rate: 16000 });
            }, 20);
          }
          if (t === 'mic_stop') { window.__mock.micStops += 1; return resolve({ ok: true }); }
          if (t === 'notification_status') return resolve({ ok: true, authorization: 'granted', delivery: 'poll' });
          return resolve({ ok: true });
        });
      }
    };
    try {
      var md = navigator.mediaDevices;
      if (md) {
        Object.defineProperty(md, 'getUserMedia', {
          configurable: true,
          value: function () {
            window.__mock.gumCalls += 1;
            return Promise.reject(new Error('NATIVE_PATH_MUST_NOT_CALL_GUM'));
          }
        });
      }
    } catch (err) {}
  })();
`;

async function injectCreds(page, token) {
  await page.goto(BASE + '/evie/', { waitUntil: 'domcontentloaded' });
  await page.evaluate(async (value) => {
    await new Promise((resolve, reject) => {
      const req = indexedDB.open('evie-pwa', 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('cred')) db.createObjectStore('cred');
      };
      req.onsuccess = () => {
        const db = req.result;
        const tx = db.transaction('cred', 'readwrite');
        tx.objectStore('cred').put(value, 'device_token');
        tx.objectStore('cred').put(null, 'access_token');
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
      };
      req.onerror = () => reject(req.error);
    });
  }, token);
}

async function waitForReady(page, label) {
  await page.waitForFunction(() => {
    const mood = document.getElementById('mood');
    const text = (mood && mood.textContent) || '';
    const talk = document.getElementById('talk');
    return /Ready|Listening|Voice unavailable/.test(text) && !!talk;
  }, null, { timeout: TIMEOUT });
  const mood = await page.textContent('#mood');
  step(label + ': boot to Ready', /Ready|Voice unavailable/.test(mood), 'mood=' + mood);
}

async function talkAndAssert(page, label, mode) {
  const wsStats = { count: 0, sent: 0 };
  page.on('websocket', (ws) => {
    if (!ws.url().includes('/v1/voice/live')) return;
    wsStats.count += 1;
    ws.on('framesent', () => { wsStats.sent += 1; });
  });

  await page.click('#talk');
  const deadline = Date.now() + TIMEOUT;
  let mood = '';
  while (Date.now() < deadline) {
    mood = (await page.textContent('#mood')) || '';
    const mock = mode === 'native' ? await page.evaluate(() => window.__mock || {}) : {};
    const nativeReady = mode === 'native' && (mock.micReads || 0) >= 3 && /Listening/.test(mood);
    const webReady = mode === 'web' && /Listening/.test(mood);
    if (nativeReady || webReady) break;
    if (/Voice unavailable|failed/i.test(mood)) break;
    await page.waitForTimeout(250);
  }
  const mock = mode === 'native' ? await page.evaluate(() => window.__mock || {}) : {};
  step(label + ': live WS opened', wsStats.count >= 1, 'sockets=' + wsStats.count);
  step(label + ': PCM frames over WS', wsStats.sent >= 1, 'framesSent=' + wsStats.sent);
  step(label + ': mood Listening', /Listening/.test(mood), 'mood=' + mood);
  if (mode === 'native') {
    step(label + ': native mic_read streaming', (mock.micReads || 0) >= 3, 'micReads=' + mock.micReads);
    step(label + ': native path never called getUserMedia', (mock.gumCalls || 0) === 0, 'gumCalls=' + mock.gumCalls);
  }

  // Stop the session and confirm the native engine is released.
  await page.waitForTimeout(1500);
  await page.click('#talk');
  await page.waitForTimeout(1500);
  if (mode === 'native') {
    const after = await page.evaluate(() => window.__mock || {});
    step(label + ': mic_stop on teardown', (after.micStops || 0) >= 1, 'micStops=' + after.micStops);
  }
  const finalMood = (await page.textContent('#mood')) || '';
  step(label + ': session stopped cleanly', !/Connecting/.test(finalMood), 'mood=' + finalMood);
}

async function runMode(browser, mode, token) {
  // Service workers are blocked so every load is fresh and route injection
  // for the native bridge is always visible. Inline init scripts are blocked
  // by the PWA's CSP, so the bridge is served as a same-origin external file.
  const context = await browser.newContext({
    viewport: { width: 430, height: 900 },
    serviceWorkers: 'block',
  });
  await context.grantPermissions(['microphone'], { origin: BASE }).catch(() => {});
  const page = await context.newPage();
  if (mode === 'native') {
    await page.route('**/evie/flow-bridge.js', (route) =>
      route.fulfill({ status: 200, contentType: 'application/javascript', body: NATIVE_MOCK })
    );
    await page.route('**/evie/', async (route) => {
      const response = await route.fetch();
      const body = (await response.text()).replace('<head>', '<head><script src="/evie/flow-bridge.js"></script>');
      await route.fulfill({ response, body });
    });
  }
  const errors = [];
  page.on('pageerror', (err) => errors.push(String(err && err.message || err).slice(0, 160)));
  page.on('console', (msg) => { if (msg.type() === 'error') errors.push('console: ' + msg.text().slice(0, 120)); });
  try {
    await injectCreds(page, token);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await waitForReady(page, mode);
    await talkAndAssert(page, mode, mode);
    step(mode + ': page error free', errors.length === 0, errors.slice(0, 2).join(' | ') || 'none');
  } catch (err) {
    step(mode + ': flow completed', false, String((err && err.message) || err).slice(0, 160));
    try {
      const mood = await page.textContent('#mood').catch(() => '?');
      const caption = await page.textContent('#reply').catch(() => '?');
      const talk = await page.textContent('#talk').catch(() => '?');
      const mock = await page.evaluate(() => window.__mock || null).catch(() => null);
      step(mode + ': failure state', false, 'mood=' + mood + ' talk=' + talk + ' mock=' + JSON.stringify(mock));
      console.error('[' + mode + '] caption=' + String(caption).slice(0, 200) + ' errors=' + errors.slice(0, 4).join(' | '));
      await page.screenshot({ path: '/tmp/evie-flow-fail-' + mode + '.png' });
    } catch (err2) {}
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
    args: [
      '--use-fake-ui-for-media-stream',
      '--use-fake-device-for-media-stream',
      '--autoplay-policy=no-user-gesture-required',
      // Headless Chrome's Local Network Access check blocks ws://127.0.0.1
      // intermittently; the API/WS flows are already proven by the Python
      // harness, so disable the check for the browser run only.
      '--disable-features=LocalNetworkAccessChecks',
    ],
  });
  try {
    await runMode(browser, 'native', TOKEN);
    await runMode(browser, 'web', TOKEN);
  } finally {
    await browser.close();
  }
  process.stdout.write('FLOW ' + JSON.stringify({ step: 'browser summary', ok: failed === 0, detail: 'failed=' + failed }) + '\n');
  process.exit(failed === 0 ? 0 : 1);
})().catch((err) => {
  step('browser harness', false, String((err && err.message) || err).slice(0, 200));
  process.exit(1);
});
