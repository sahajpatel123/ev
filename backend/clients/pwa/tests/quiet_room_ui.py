"""Isolated WebKit UI acceptance checks; no production services or credentials.

Run with a Python that has Playwright and its WebKit browser installed:
  python backend/clients/pwa/tests/quiet_room_ui.py
Screenshots are written to /private/tmp/evie-atelier-ui.
Network responses and voice state below are explicitly synthetic UI fixtures.
"""
import base64
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path('/private/tmp/evie-atelier-ui')
ARTIFACTS.mkdir(exist_ok=True)
embedded = re.search(r'const MATERIAL_DATA = "data:image/webp;base64,([A-Za-z0-9+/=]+)"', (ROOT / 'presence.js').read_text())
assert embedded, 'photographic material is missing from the release-hashed bundle'
assert base64.b64decode(embedded.group(1)) == (ROOT / 'assets/presence-material-v1.webp').read_bytes(), 'bundled material differs from its source asset'


def route_request(route):
    path = urlparse(route.request.url).path
    if path.startswith('/evie/'):
        asset = ROOT / (path.removeprefix('/evie/') or 'index.html')
        if asset.is_file() and asset.parent == ROOT:
            types = {'.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.json': 'application/json', '.svg': 'image/svg+xml'}
            route.fulfill(path=str(asset), content_type=types.get(asset.suffix, 'application/octet-stream'))
            return
    route.fulfill(status=200, content_type='application/json', body='{}')


with sync_playwright() as p:
    browser = p.webkit.launch()
    reports = []
    cases = [(375, 667, 'light', 1), (402, 874, 'light', 1), (375, 667, 'dark', 1), (402, 874, 'dark', 1), (320, 568, 'light', 1), (667, 375, 'light', 1), (375, 667, 'light', 2)]
    if os.environ.get('EVIE_UI_CASE'):
        cases = [case for case in cases if '-'.join(map(str, case)) == os.environ['EVIE_UI_CASE']]
        assert cases, 'Unknown EVIE_UI_CASE'
    for width, height, theme, text_scale in cases:
        context = browser.new_context(viewport={'width': width, 'height': height}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme=theme, service_workers='block', reduced_motion='reduce')
        context.route('**/*', route_request)
        page = context.new_page()
        errors: list[str] = []
        page.on('pageerror', lambda err, sink=errors: sink.append(str(err)))
        page.goto('https://quiet.test/evie/')
        page.wait_for_function('typeof state !== "undefined" && state.orb && state.conn === "DISCONNECTED"')
        page.wait_for_function('state.orb.materialReady === true')
        assert page.locator('#welcome').is_visible(), 'pairing disappeared'
        assert page.evaluate('document.querySelector(".welcome .field").getBoundingClientRect().top - document.querySelector("#pair-code-help").getBoundingClientRect().bottom >= 20'), 'pairing label crowds instructions'
        page.screenshot(path=str(ARTIFACTS / f'{width}-{height}-{theme}-pairing.png'))
        page.evaluate('state.deviceToken="isolated-ui-fixture"; state.conn="READY"; state.ui="READY"; setMood("Ready"); render(); state.orb.draw()')
        if text_scale == 2:
            before_font = page.locator('#room-invitation').evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)')
            page.evaluate('''() => {
              const sizes = Array.from(document.querySelectorAll('p,h1,h2,h3,h4,button,input,textarea,summary,small,label,span')).map(el => [el, parseFloat(getComputedStyle(el).fontSize)]);
              sizes.forEach(([el, size]) => { el.style.fontSize = (size * 2) + 'px'; });
            }''')
            assert page.locator('#room-invitation').evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)') == before_font * 2
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal overflow'
        contrast = page.evaluate('''() => {
          const css = getComputedStyle(document.documentElement);
          const luminance = name => {
            const hex = css.getPropertyValue(name).trim().slice(1);
            const values = [0,2,4].map(i => parseInt(hex.slice(i, i+2),16)/255).map(c => c <= .04045 ? c/12.92 : ((c+.055)/1.055)**2.4);
            return values[0]*.2126 + values[1]*.7152 + values[2]*.0722;
          };
          return [['--ink','--paper'],['--muted','--paper'],['--ink','--elevated'],['--muted','--elevated'],['--talk-ink','--talk']].map(([a,b]) => { const x=luminance(a), y=luminance(b); return (Math.max(x,y)+.05)/(Math.min(x,y)+.05); });
        }''')
        assert min(contrast) >= 4.5, f'text palette contrast too low: {contrast}'
        assert page.locator('#ready-ui button:visible').count() == 4, 'idle room is not minimal'
        assert page.evaluate('!state.talking'), 'idle started microphone'
        if height >= 568 and text_scale == 1:
            assert page.evaluate('document.documentElement.scrollHeight <= innerHeight + 2'), 'idle requires scrolling'
            assert page.locator('#talk').evaluate('(el) => { const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }')
        prefix = f'{width}-{height}-{theme}-{text_scale}x'
        page.screenshot(path=str(ARTIFACTS / f'{prefix}-room.png'))
        page.locator('#more-btn').click()
        assert page.locator('#more-sheet').is_visible()
        assert page.locator('#more-sheet button').evaluate_all('(buttons)=>buttons.every(el=>parseFloat(getComputedStyle(el).borderTopWidth)===0 && parseFloat(getComputedStyle(el).borderBottomWidth)===0)'), 'button border remains'
        assert page.evaluate('document.querySelector("#ready-ui").inert')
        page.locator('#room-tool-search').fill('camera')
        assert page.locator('#look-btn').is_visible()
        page.locator('#room-tool-search').fill('zzzz-no-tool')
        assert page.locator('#room-tool-empty').is_visible()
        assert page.locator('.room-tool-group:visible').count() == 0
        page.locator('#room-tool-search').fill('today')
        page.locator('#room-tool-list [data-surface="today"]').click()
        assert page.locator('#today-sheet').is_visible()
        assert not page.locator('#more-sheet').is_visible()
        page.keyboard.press('Tab')
        assert page.evaluate('document.querySelector("#today-sheet").contains(document.activeElement)')
        page.keyboard.press('Escape')
        assert page.locator('#more-btn').evaluate('(el) => el === document.activeElement'), 'nested panel lost return focus'
        page.locator('#type-btn').click()
        assert page.locator('#text').evaluate('(el) => el === document.activeElement')
        page.locator('#text').fill('A test thought')
        page.evaluate('window.realSendText=sendText; sendText=async()=>{throw new Error("Fixture send failed")}; void 0')
        page.get_by_role('button', name='Send thought', exact=True).click()
        page.wait_for_function('document.querySelector("#reply").textContent === "Fixture send failed"')
        assert page.locator('#text').input_value() == 'A test thought', 'failed draft lost'
        page.evaluate('sendText=window.realSendText; void 0')
        page.locator('#room-write-close').click()
        page.evaluate('state.userLine="Keep some room in my afternoon."; state.caption="One thing at a time. You can set this reply aside and come back to it."; paintLive()')
        page.wait_for_timeout(50)
        assert page.locator('#room-exchange').evaluate('(el)=>el.open')
        page.screenshot(path=str(ARTIFACTS / f'{prefix}-reply.png'))
        page.locator('#room-exchange summary').click()
        page.wait_for_timeout(50)
        page.evaluate('state.caption += " Another streamed word."; paintLive()')
        assert not page.locator('#room-exchange').evaluate('(el)=>el.open'), 'stream reopened set-aside reply'
        page.evaluate('state.ui="ERROR"; setMood("Voice unavailable"); paintLive()')
        assert page.locator('#room-exchange').evaluate('(el)=>el.open'), 'recovery hidden'
        page.evaluate('state.ui="READY"; state.talking=true; state.webrtc={playBlocked:true, stop(){window.stoppedFixture=true}, enableAudio:async()=>{window.enabledFixture=true}}; setMood("Listening"); paintLive()')
        assert page.locator('#room-enable-audio').is_visible()
        page.locator('#more-btn').click()
        assert page.locator('#room-stop-session').is_visible(), 'Stop unreachable in tools'
        assert page.locator('#room-call-bar').evaluate('(el)=>document.querySelector("#more-sheet").contains(el)'), 'Stop outside modal accessibility scope'
        assert page.evaluate('document.querySelector("#more-sheet").getBoundingClientRect().bottom <= document.querySelector("#room-call-bar").getBoundingClientRect().top'), 'session control overlaps tool content'
        page.screenshot(path=str(ARTIFACTS / f'{prefix}-tools.png'))
        page.locator('#room-stop-session').click()
        page.wait_for_function('state.talking === false')
        assert not page.evaluate('window.enabledFixture === true'), 'Stop enabled audio instead'
        page.keyboard.press('Escape')
        page.evaluate('state.orb.setReduced(true); state.orb.draw()')
        first = page.locator('#orb').evaluate('(el) => el.toDataURL()')
        page.evaluate('state.orb.t += 10; state.orb.draw()')
        assert page.locator('#orb').evaluate('(el) => el.toDataURL()') == first
        assert page.evaluate('state.orb.raf === 0'), 'reduced-motion animation loop still running'
        page.evaluate('state.orb.setState("idle"); state.orb.setReduced(false); state.orb.draw()')
        page.locator('#orb').scroll_into_view_if_needed()
        page.wait_for_function('state.orb.raf !== 0')
        assert page.locator('#orb').get_attribute('data-presence-version') == 'living-glass-2'
        clock_start = page.evaluate('state.orb.t')
        page.wait_for_timeout(1000)
        assert page.evaluate('state.orb.t') - clock_start >= .8, 'visible idle animation clock is stalled or too slow'
        page.evaluate('state.orb.stopLoop(); state.orb.t=0; state.orb.draw()')
        float_start = page.locator('#orb').evaluate('(el)=>el.toDataURL()')
        page.evaluate('state.orb.t=2; state.orb.draw()')
        assert page.locator('#orb').evaluate('(el)=>el.toDataURL()') != float_start, 'centerpiece does not animate'
        page.evaluate('state.orb.setPaused(true); state.orb.start()')
        assert page.evaluate('state.orb.raf === 0'), 'paused material keeps animating'
        page.evaluate('state.orb.setPaused(false); state.orb.stopLoop()')
        page.locator('.room-presence').evaluate('(el)=>{el.style.width="190px";el.style.height="120px"}')
        page.wait_for_function('document.querySelector("#orb").width === Math.floor(document.querySelector("#orb").clientWidth * Math.min(2,devicePixelRatio)) && document.querySelector("#orb").height === Math.floor(document.querySelector("#orb").clientHeight * Math.min(2,devicePixelRatio))')
        page.locator('.room-presence').evaluate('(el)=>{el.style.width="";el.style.height=""}')
        page.evaluate('state.orb.setState("listening"); state.orb.stopLoop(); state.orb.amp=0; state.orb.draw()')
        quiet_frame = page.locator('#orb').evaluate('(el)=>el.toDataURL()')
        page.evaluate('state.orb.amp=.8; state.orb.draw()')
        assert page.locator('#orb').evaluate('(el)=>el.toDataURL()') != quiet_frame, 'voice amplitude has no visual effect'
        page.evaluate('state.orb.setReduced(true)')
        page.evaluate('window.realGetUserMedia=navigator.mediaDevices.getUserMedia; navigator.mediaDevices.getUserMedia=async()=>{window.fixtureDeniedCalled=true; throw new DOMException("Fixture camera denied", "NotAllowedError")}; void 0')
        page.evaluate('window.fixtureDeniedResult="pending"; captureCamera({}).then(()=>{window.fixtureDeniedResult=false},()=>{window.fixtureDeniedResult=true}); void 0')
        try:
            page.wait_for_function('window.fixtureDeniedResult !== "pending"', polling=100, timeout=10000)
        except Exception:
            print(page.evaluate('({called:window.fixtureDeniedCalled,result:window.fixtureDeniedResult,media:String(navigator.mediaDevices.getUserMedia),generation:state._roomCameraGeneration,hidden:document.hidden})'), errors, flush=True)
            raise
        assert page.evaluate('window.fixtureDeniedCalled && window.fixtureDeniedResult') and not page.locator('#camera-sheet').is_visible(), 'camera denial strands overlay'
        page.evaluate('navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{window.resolveFixtureCamera=resolve}); window.cameraResult=captureCamera({}).catch(err=>err.message); void 0')
        page.locator('#room-camera-close').click()
        page.evaluate('window.resolveFixtureCamera({getTracks:()=>[{stop(){window.fixtureTrackStopped=true}}]}); void 0')
        assert page.evaluate('window.cameraResult') == 'Camera cancelled.'
        assert page.evaluate('window.fixtureTrackStopped === true'), 'cancelled camera leaked track'
        page.evaluate('navigator.mediaDevices.getUserMedia=window.realGetUserMedia; void 0')
        assert not page.locator('#camera-sheet').is_visible()
        page.evaluate('applyAppearance("dark")')
        assert page.locator('meta[name="theme-color"]').first.get_attribute('content') == '#191c19'
        page.evaluate('applyAppearance("light")')
        assert page.locator('meta[name="theme-color"]').first.get_attribute('content') == '#f6f5f1'
        assert not errors, errors
        reports.append({'viewport': [width, height], 'theme': theme, 'text_scale': text_scale, 'minimum_palette_contrast': round(min(contrast), 2), 'result': 'PASS'})
        print(f'PASS {prefix}', flush=True)
        context.close()
    browser.close()
    print(json.dumps(reports, indent=2))
