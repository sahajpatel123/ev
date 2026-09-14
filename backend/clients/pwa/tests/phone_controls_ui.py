"""Real boot, hello and Talk handlers in isolated WebKit.

Only network and WebRTC/media boundaries are simulated. This does not prove
physical iPhone permissions, provider quality, or audible speech.
"""
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
requests: list[str] = []
mode = {'open': 'success'}


def route_request(route):
    path = urlparse(route.request.url).path
    if path.startswith('/evie/'):
        asset = ROOT / (path.removeprefix('/evie/') or 'index.html')
        if asset.is_file() and asset.parent == ROOT:
            types = {'.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.json': 'application/json'}
            route.fulfill(path=str(asset), content_type=types.get(asset.suffix, 'application/octet-stream'))
            return
    requests.append(path)
    body: dict = {'ok': True}
    status = 200
    if path.endswith('/pair'):
        body = {'device_token': 'isolated-device', 'access_token': 'isolated-access', 'device': {'role': 'companion'}}
    elif path.endswith('/session'):
        body = {'access_token': 'isolated-access', 'device': {'role': 'companion'}}
    elif path.endswith('/hello'):
        body = {'device': {'role': 'companion'}, 'home_station': 'ONLINE', 'recommended_backend': 'webrtc_strict', 'status': {'trust_state': 'PAIRED_SANDBOX'}}
    elif path.endswith('/live/open'):
        if mode['open'] == 'failure':
            status, body = 503, {'detail': 'Voice provider is unavailable. Please retry.'}
        else:
            body = {'session_id': 'fixture-session', 'lease_id': 'fixture-lease', 'media_backend': 'webrtc_strict', 'strict_webrtc': True}
    route.fulfill(status=status, content_type='application/json', body=json.dumps(body))


with sync_playwright() as p:
    browser = p.webkit.launch()
    context = browser.new_context(viewport={'width': 375, 'height': 667}, is_mobile=True, has_touch=True, service_workers='block', reduced_motion='reduce')
    context.route('**/*', route_request)
    page = context.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda err: errors.append(str(err) + '\n' + (err.stack or '')))
    page.goto('https://controls.test/evie/')
    page.wait_for_function('typeof state !== "undefined" && state.orb && state.conn === "DISCONNECTED"')
    page.locator('#pair-token').fill('ISOLATED-CODE')
    page.locator('#pair-btn').click()
    page.wait_for_function('state.conn === "READY" && state.ui === "READY"')
    assert '/v1/device-gateway/hello' in requests
    assert page.locator('#talk').is_enabled(), 'real hello did not enable Talk'
    page.wait_for_load_state('networkidle')
    page.reload()
    page.wait_for_function('state.conn === "READY" && state.ui === "READY"')
    assert requests.count('/v1/device-gateway/hello') >= 2, 'persisted boot skipped hello'
    print('PASS actual pairing → hello → reload → READY', flush=True)

    page.evaluate('''() => {
      window.fixtureMode = 'success';
      window.EvieWebRTC = class {
        constructor(options) { this.options = options; this.playBlocked = false; }
        async start() {
          window.fixtureRTC = this;
          if (window.fixtureMode === 'pending') await new Promise(resolve => { this.finish = resolve; });
          if (window.fixtureMode === 'denied') { const err = new Error('Microphone access denied.'); err.failed_stage = 'M02'; throw err; }
          if (this.closed) throw new Error('Cancelled');
          return { settings: { sampleRate: 48000 } };
        }
        stop() { this.closed = true; if (this.finish) this.finish(); }
        async enableAudio() { this.playBlocked = false; }
      };
    }''')
    page.locator('#talk').click()
    page.wait_for_function('state.talking && !state._talkInflight')
    assert page.evaluate('state.ui === "LISTENING"'), 'connected UI stuck CONNECTING'
    assert page.locator('#talk').is_enabled(), 'Stop disabled after successful startup'
    page.locator('#talk').click()
    page.wait_for_function('!state.talking && !state._voiceCleanup')
    assert page.evaluate('state.ui === "READY" && state.sessionId === null')
    print('PASS actual Talk → connected → enabled Stop → READY', flush=True)

    page.evaluate('window.fixtureMode = "pending"')
    page.locator('#talk').click()
    page.wait_for_function('state._talkInflight && state.webrtc')
    assert page.locator('#talk').is_enabled(), 'Stop disabled during connecting'
    page.locator('#talk').click()
    page.wait_for_function('!state._talkInflight && !state.talking && !state._voiceCleanup')
    assert page.evaluate('window.fixtureRTC.closed && state.webrtc === null')
    print('PASS cancel pending startup without a stuck button', flush=True)

    page.evaluate('window.fixtureMode = "denied"')
    page.locator('#talk').click()
    page.wait_for_function('!state._talkInflight && /denied/.test(state.caption)')
    assert page.locator('#talk').is_enabled()
    assert page.locator('#reply').is_visible()
    mode['open'] = 'failure'
    page.locator('#talk').click()
    page.wait_for_function('!state._talkInflight && /provider is unavailable/.test(state.caption)')
    assert page.locator('#talk').is_enabled()
    print('PASS microphone denial and live-open failure show retryable errors', flush=True)

    mode['open'] = 'success'
    page.evaluate('window.fixtureMode = "success"')
    page.locator('#talk').click()
    page.wait_for_function('state.talking && !state._talkInflight')
    page.evaluate('state.webrtc.playBlocked=true; paintLive()')
    page.locator('#more-btn').click()
    assert page.locator('#more-sheet #room-enable-audio').is_visible()
    page.locator('#room-enable-audio').click()
    page.wait_for_function('!state.webrtc.playBlocked')
    page.locator('#room-stop-session').click()
    page.wait_for_function('!state.talking && !state._voiceCleanup')
    page.keyboard.press('Escape')
    print('PASS audio recovery and Stop accessible inside modal', flush=True)

    # Native fetch is replaced only at the I/O boundary to test the real api().
    result = page.evaluate('''async () => {
      const original = window.fetch;
      const device = state.deviceToken;
      window.fetch = async (path, opts) => {
        if (path.endsWith('/session')) throw new TypeError('Fixture network loss');
        if (path === '/fixture-auth') return new Response('{}', { status: 401 });
        return original(path, opts);
      };
      try { await api('/fixture-auth'); } catch (_) {}
      const preserved = state.deviceToken === device && await loadToken() === device;
      window.fetch = (path, opts) => path !== '/fixture-timeout' ? original(path, opts) : new Promise((_resolve, reject) => opts.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))));
      let timeout = '';
      try { await api('/fixture-timeout', { _timeoutMs: 30 }); } catch (err) { timeout = err.message; }
      window.fetch = original;
      return { preserved, timeout };
    }''')
    assert result['preserved'], 'transient refresh erased pairing'
    assert 'too long' in result['timeout']
    assert not errors, errors
    print('PASS bounded request and pairing preserved on transient refresh failure', flush=True)
    context.close()
    browser.close()
    print(json.dumps({'groups_passed': 7, 'browser': 'isolated WebKit', 'real_phone_voice_proven': False}))
