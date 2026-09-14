"""Internal WhatsApp Web page scripts. Never projected to Muse."""

from __future__ import annotations

import json

_CHAT_NAME = r"""
function chatName(n) {
  const t = n.querySelector('[data-testid="cell-frame-title"], [data-testid="conversation-info-header-chat-title"]');
  if (t) {
    const raw = t.getAttribute('title') || t.innerText || '';
    if (raw) return String(raw).split('\n')[0].trim();
  }
  const titled = n.querySelector('[title]');
  if (titled) {
    const raw = titled.getAttribute('title') || '';
    if (raw) return String(raw).split('\n')[0].trim();
  }
  const span = n.querySelector('span[dir="auto"]');
  if (span) {
    const raw = span.getAttribute('title') || span.innerText || '';
    if (raw) return String(raw).split('\n')[0].trim();
  }
  return '';
}
function chatNodes() {
  return Array.from(document.querySelectorAll(
    '[data-testid="cell-frame-container"], #pane-side [role="listitem"], [aria-label="Chat list"] [role="listitem"], #pane-side [role="row"], [data-testid^="list-item"]'
  ));
}
function searchInput() {
  return document.querySelector('input[aria-label="Search or start a new chat"]')
    || document.querySelector('[data-testid="chat-list-search-container"] input')
    || document.querySelector('[data-testid="chat-list-search"]')
    || document.querySelector('#side input[type="text"]')
    || document.querySelector('input[aria-label="Search name, number or @username"]')
    || document.querySelector('#side [contenteditable="true"][role="textbox"]')
    || document.querySelector('#side [contenteditable="true"]');
}
function setInputValue(input, q) {
  input.focus();
  if (input.tagName === 'INPUT' || input.tagName === 'TEXTAREA') {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, q);
  } else {
    input.textContent = q;
  }
  input.dispatchEvent(new InputEvent('input', { bubbles: true, data: q, inputType: 'insertText' }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
}
function composeBox() {
  return document.querySelector('[data-testid="conversation-compose-box-input"]')
    || document.querySelector('footer [contenteditable="true"]')
    || document.querySelector('#main [contenteditable="true"][role="textbox"]');
}
function sendControl() {
  const labeled = document.querySelector('footer button[aria-label*="Send" i], #main button[aria-label*="Send" i]');
  if (labeled) return labeled;
  const icon = document.querySelector('[data-icon="wds-ic-send-filled"], [data-icon="send"], [data-testid="send"]');
  if (!icon) return null;
  return icon.closest('button') || icon.closest('[role="button"]') || icon.parentElement || icon;
}
"""


def status_js() -> str:
    return """
const text = (document.body && document.body.innerText || '').slice(0, 2500);
const qr = /scan the qr|keep me signed in|log in to whatsapp|link with phone/i.test(text)
  || !!document.querySelector('canvas[aria-label*="QR" i], canvas[aria-label*="qr" i]');
const list = document.querySelector('#pane-side, [aria-label="Chat list"], [data-testid="chat-list"]');
return JSON.stringify({
  ok: true,
  authenticated: !qr && !!list,
  qr: qr,
  diagnosis: qr ? 'session_logged_out' : (list ? 'ok' : 'ui_changed'),
  backing: 'whatsapp_web',
  activated: false,
  focus_theft: 0
});
"""


def type_search_js(query: str) -> str:
    q = json.dumps(query)
    return f"""
{_CHAT_NAME}
const q = {q};
const input = searchInput();
if (!input) return JSON.stringify({{ok: false, error: 'search_input_missing', diagnosis: 'ui_changed', activated: false, focus_theft: 0}});
setInputValue(input, q);
return JSON.stringify({{ok: true, typed: true, activated: false, focus_theft: 0}});
"""


def search_chats_js(query: str) -> str:
    q = json.dumps(query)
    return f"""
{_CHAT_NAME}
const q = {q};
const items = [];
const seen = new Set();
const selfRow = document.querySelector('[data-testid="message-yourself-row"]');
if (selfRow) {{
  const name = chatName(selfRow) || 'Message yourself';
  const sec = ((selfRow.querySelector('[data-testid="cell-frame-secondary"]') || {{}}).innerText || '');
  items.push({{chat_ref: '__self__', name: name, kind: 'self', unread: 0, gist: String(sec || '').split('\\n')[0].trim().slice(0, 80)}});
  seen.add('__self__');
}}
if (!seen.has('__self__') && document.querySelector('#main [data-testid="conversation-compose-box-input"]')) {{
  items.unshift({{chat_ref: '__self__', name: 'Message yourself', kind: 'self', unread: 0}});
  seen.add('__self__');
}}
chatNodes().forEach((n) => {{
  const name = chatName(n);
  if (!name) return;
  if (/^\\d+\\s+unread\\b/i.test(name)) return;
  if (/^(locked chats|archived|communities|status|channels|new chat|meta ai)$/i.test(name)) return;
  const sec = ((n.querySelector('[data-testid="cell-frame-secondary"]') || {{}}).innerText || '');
  const isSelf = n.getAttribute('data-testid') === 'message-yourself-row' || /message yourself/i.test(sec);
  const ref = isSelf ? '__self__' : name;
  // Dedupe on name + secondary text, never on name alone: two chats that
  // display the same name must both reach the resolver so it refuses to
  // guess between them instead of silently picking the first row.
  const key = ref + '\\n' + String(sec || '').split('\\n')[0].trim().slice(0, 40);
  if (seen.has(key)) return;
  if (!q || isSelf || name.toLowerCase().includes(String(q).toLowerCase()) || /message yourself/i.test(String(q))) {{
    seen.add(key);
    items.push({{chat_ref: ref, name: name, unread: /\\d+/.test((n.innerText||'').split('\\n').slice(-1)[0] || '') ? 1 : 0, gist: String(sec || '').split('\\n')[0].trim().slice(0, 80)}});
  }}
}});
return JSON.stringify({{ok: true, chats: items.slice(0, 20), authenticated: true, activated: false, focus_theft: 0}});
"""


def open_new_chat_js() -> str:
    return f"""
{_CHAT_NAME}
const existing = document.querySelector('[data-testid="message-yourself-row"]') || document.querySelector('[data-testid="new-chat-drawer"]');
if (document.querySelector('[data-testid="message-yourself-row"]')) {{
  return JSON.stringify({{ok: true, already: true, activated: false, focus_theft: 0}});
}}
const btn = Array.from(document.querySelectorAll('[aria-label]')).find((e) => (e.getAttribute('aria-label') || '') === 'New chat');
if (!btn) return JSON.stringify({{ok: false, error: 'new_chat_missing', diagnosis: 'ui_changed', activated: false, focus_theft: 0}});
btn.click();
return JSON.stringify({{ok: true, opened_new_chat: true, activated: false, focus_theft: 0}});
"""


def open_chat_js(chat_ref: str) -> str:
    ref = json.dumps(chat_ref)
    return f"""
{_CHAT_NAME}
const want = {ref};
const selfWant = want === '__self__' || /^you$/i.test(want) || /message yourself/i.test(want);
function headerTitle() {{
  const header = document.querySelector('[data-testid="conversation-info-header-chat-title"]');
  return header ? (header.innerText || '').split('\\n')[0].trim() : '';
}}
function conversationOpen() {{
  return !!document.querySelector('[data-testid="conversation-compose-box-input"]')
    || !!document.querySelector('footer [contenteditable="true"]');
}}
function sameChat(a, b) {{
  // Sidebar titles (title attr) and headers (innerText) can differ by emoji
  // variation selectors, bidi marks, or spacing: compare normalized names.
  const normName = (s) => String(s || '').normalize('NFC').replace(/[\\uFE00-\\uFE0F\\u200e\\u200f\\u2060]/g, '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase();
  const na = normName(a), nb = normName(b);
  return !!na && !!nb && na === nb;
}}
function pressEnterOn(el) {{
  for (const type of ['keydown','keypress','keyup']) {{
    el.dispatchEvent(new KeyboardEvent(type, {{key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true, cancelable:true}}));
  }}
}}
const headerName = headerTitle();
if (want && sameChat(want, headerName) && conversationOpen()) {{
  return JSON.stringify({{ok: true, chat_ref: want, name: headerName, already: true, activated: false, focus_theft: 0}});
}}
if (selfWant) {{
  // The self row's .click() may open with a delay, so this branch both
  // clicks and verifies across round-trips. A same-named contact must never
  // be mistaken for Message yourself: accept only a uniquely-named open
  // thread, and never type "__self__" into search (it matches nothing).
  const row = document.querySelector('[data-testid="message-yourself-row"]');
  const rowName = row ? chatName(row) : '';
  const selfHeader = headerTitle();
  if (rowName && sameChat(rowName, selfHeader) && conversationOpen()) {{
    let dups = 0;
    chatNodes().forEach((n) => {{ if (sameChat(rowName, chatName(n))) dups++; }});
    if (dups <= 1) {{
      return JSON.stringify({{ok: true, chat_ref: '__self__', name: selfHeader, activated: false, focus_theft: 0}});
    }}
    return JSON.stringify({{ok: true, entered: false, opened: false, chat_ref: '__self__', error: 'self_name_ambiguous', diagnosis: 'self_name_ambiguous', activated: false, focus_theft: 0}});
  }}
  if (row) row.click();
  return JSON.stringify({{ok: true, entered: false, opened: false, chat_ref: '__self__', activated: false, focus_theft: 0}});
}}
const input = searchInput();
if (!input) return JSON.stringify({{ok: false, error: 'search_input_missing', diagnosis: 'ui_changed', activated: false, focus_theft: 0}});
// A bare .click() on a row no longer opens the thread; the search box plus
// Enter is the gesture WhatsApp honors in a background tab.
setInputValue(input, want);
pressEnterOn(document.activeElement || input);
// Verify the RIGHT chat opened: Enter picks the top search hit, which may be
// a different chat. A send must never go to a wrongly-opened thread.
const wantDigits = String(want).replace(/\\D+/g, '');
const openedHeader = headerTitle();
const headerDigits = String(openedHeader).replace(/\\D+/g, '');
const nameMatch = sameChat(want, openedHeader);
// For unsaved numbers the header IS the number in display format: require a
// digit match, never "any open thread". A search+Enter that landed anywhere
// else keeps reporting entered/opened:false so callers refuse.
const digitsMatch = wantDigits.length >= 7 && headerDigits.length >= 7
  && (headerDigits.endsWith(wantDigits) || wantDigits.endsWith(headerDigits));
if (conversationOpen() && (nameMatch || digitsMatch)) {{
  return JSON.stringify({{ok: true, chat_ref: nameMatch ? want : openedHeader, name: openedHeader, entered: true, opened: true, activated: false, focus_theft: 0}});
}}
return JSON.stringify({{ok: true, entered: true, opened: false, chat_ref: want, header: openedHeader, activated: false, focus_theft: 0}});
"""


def click_exact_chat_js(chat_ref: str) -> str:
    """Open the row whose name is exactly ``chat_ref`` (not the top search hit).

    A bare ``.click()`` does not navigate; a full mouse gesture does. Search
    reorders variants ("Mansi Makani" can outrank "Mansi"), so the exact row
    must be opened deliberately.
    """

    ref = json.dumps(chat_ref)
    return f"""
{_CHAT_NAME}
const want = {ref};
function normName(s) {{
  return String(s || '').normalize('NFC')
    .replace(/[\\uFE00-\\uFE0F\\u200e\\u200f\\u2060]/g, '')
    .replace(/\\u00a0/g, ' ')
    .replace(/\\s+/g, ' ')
    .trim()
    .toLowerCase();
}}
function mouseOpen(node) {{
  const r = node.getBoundingClientRect();
  const opts = {{bubbles: true, cancelable: true, view: window,
    clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0}};
  node.dispatchEvent(new MouseEvent('mouseover', opts));
  node.dispatchEvent(new MouseEvent('mousedown', opts));
  node.dispatchEvent(new MouseEvent('mouseup', opts));
  node.dispatchEvent(new MouseEvent('click', opts));
}}
const nodes = chatNodes();
function rowFor(el) {{
  return el && el.closest
    ? el.closest('[data-testid^="list-item"], [role="listitem"], [data-testid="cell-frame-container"]')
    : null;
}}
function shownName(node) {{
  const r = node.getBoundingClientRect();
  if (r.width < 4 || r.height < 4) return '';
  const under = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  const row = rowFor(under) || rowFor(node);
  return row ? chatName(row) : '';
}}
// The chat list is virtualized: a stored node can carry a stale name while a
// different row is painted at its position. Only click a point whose painted
// row really is the wanted name.
let hit = null;
for (const n of nodes) {{
  if (normName(chatName(n)) === normName(want) && normName(shownName(n)) === normName(want)) {{
    hit = n;
    break;
  }}
}}
if (!hit) {{
  for (const n of nodes) {{
    const r = n.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const under = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    const row = rowFor(under);
    if (row && normName(chatName(row)) === normName(want)) {{
      hit = row;
      break;
    }}
  }}
}}
const wantDigits = String(want).replace(/\\D+/g, '');
if (!hit && wantDigits.length >= 7) {{
  for (const n of nodes) {{
    const r = n.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const under = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    const row = rowFor(under) || n;
    const hay = (String(row.innerText || '') + ' ' + String(row.getAttribute('data-id') || '')).replace(/\\D+/g, '');
    if (hay.includes(wantDigits)) {{ hit = row; break; }}
  }}
}}
if (!hit) return JSON.stringify({{ok: false, error: 'exact_row_missing', activated: false, focus_theft: 0}});
const rect = hit.getBoundingClientRect();
const cx = rect.left + rect.width / 2;
const cy = rect.top + rect.height / 2;
const targetEl = document.elementFromPoint(cx, cy) || hit;
const opts = {{bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy, button: 0}};
targetEl.dispatchEvent(new MouseEvent('mouseover', opts));
targetEl.dispatchEvent(new MouseEvent('mousedown', opts));
targetEl.dispatchEvent(new MouseEvent('mouseup', opts));
targetEl.dispatchEvent(new MouseEvent('click', opts));
return JSON.stringify({{ok: true, clicked: true, name: chatName(hit), activated: false, focus_theft: 0}});
"""


def read_recent_js(limit: int) -> str:
    cap = max(1, min(int(limit), 80))
    return f"""
const cap = {cap};
const header = document.querySelector('[data-testid="conversation-info-header-chat-title"]')
  || document.querySelector('#main header')
  || document.querySelector('[data-testid="conversation-header"]');
const title = header ? (header.innerText || '').split('\\n')[0].trim() : '';
const msgs = [];
const rows = document.querySelectorAll('[data-testid="msg-container"]');
rows.forEach((row) => {{
  const preEl = row.querySelector('[data-pre-plain-text]');
  const pre = preEl ? (preEl.getAttribute('data-pre-plain-text') || '') : '';
  const copy = row.querySelector('.copyable-text') || row;
  const text = ((copy.innerText || row.innerText || '')).trim();
  if (!text) return;
  const fromMe = !!row.querySelector('[data-icon="tail-out"]') || /\\]\\s*You:/i.test(pre);
  const body = text.replace(/\\n\\d{1,2}:\\d{2}(\\s?[AP]M)?\\s*$/, '').trim();
  msgs.push({{id: String(msgs.length+1), from_me: fromMe, text: text.slice(0, 500), body: body.slice(0, 500), timestamp: pre.slice(0, 80), sender: fromMe ? 'owner' : title}});
}});
const bounded = msgs.slice(-cap);
return JSON.stringify({{ok: true, chat_ref: title, name: title, messages: bounded, complete_history: false, activated: false, focus_theft: 0}});
"""


def search_messages_js(query: str) -> str:
    q = json.dumps(query)
    return f"""
const q = String({q}).toLowerCase();
const hits = [];
const rows = document.querySelectorAll('[data-testid="msg-container"]');
rows.forEach((row) => {{
  const text = (row.innerText || '').trim();
  if (q && text.toLowerCase().includes(q)) {{
    hits.push({{text: text.slice(0, 400), from_me: !!row.querySelector('[data-icon="tail-out"]')}});
  }}
}});
return JSON.stringify({{ok: true, messages: hits.slice(0, 30), complete_history: false, activated: false, focus_theft: 0}});
"""


def compose_js(text: str) -> str:
    body = json.dumps(text)
    return f"""
{_CHAT_NAME}
const text = {body};
const norm = (s) => String(s || '').replace(/[\\u200b\\u200e\\u2060]/g, '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
const box = composeBox();
if (!box) return JSON.stringify({{ok: false, error: 'compose_box_missing', sent: false, diagnosis: 'ui_changed', activated: false, focus_theft: 0}});
// Read before touching: a foreign draft is refused, never replaced. EV's own
// exact text (a retried compose) is allowed through.
const prior = norm(box.innerText || box.textContent || '');
const want = norm(text);
if (prior && prior !== want) {{
  return JSON.stringify({{ok: false, foreign_draft: true, matched: false, sent: false, prior_len: prior.length, present_len: prior.length, activated: false, focus_theft: 0}});
}}
box.focus();
document.execCommand('selectAll', false, null);
const inserted = document.execCommand('insertText', false, text);
const now = norm(box.innerText || box.textContent || '');
// Only a normalized exact match counts: WhatsApp may re-render spaces,
// links, or emoji around what was typed.
const matched = now === want;
return JSON.stringify({{ok: inserted || matched, matched: matched, composed: text, present_len: now.length, sent: false, activated: false, focus_theft: 0}});
"""


def click_send_js() -> str:
    return f"""
{_CHAT_NAME}
const btn = sendControl();
if (!btn) return JSON.stringify({{ok: true, composed: true, sent: false, prepared: true, diagnosis: 'send_button_missing', activated: false, focus_theft: 0}});
btn.click();
return JSON.stringify({{ok: true, clicked: true, sent: true, activated: false, focus_theft: 0}});
"""


def send_js(text: str) -> str:
    """Legacy one-shot send. Prefer compose + click_send with a yield between."""
    body = json.dumps(text)
    return f"""
{_CHAT_NAME}
const text = {body};
const lastOut = () => {{
  const rows = Array.from(document.querySelectorAll('[data-testid="msg-container"]'));
  for (let i = rows.length - 1; i >= 0; i--) {{
    if (rows[i].querySelector('[data-icon="tail-out"]')) return rows[i].innerText || '';
  }}
  const last = rows[rows.length-1];
  return last ? (last.innerText || '') : '';
}};
const prior = lastOut();
if (prior && prior.indexOf(text) !== -1) {{
  return JSON.stringify({{ok: true, sent: true, duplicate_prevented: true, verified_in_thread: true, text_fragment: text.slice(0,80), activated: false, focus_theft: 0}});
}}
const box = composeBox();
if (!box) return JSON.stringify({{ok: false, error: 'compose_box_missing', sent: false, diagnosis: 'ui_changed', activated: false, focus_theft: 0}});
box.focus();
document.execCommand('selectAll', false, null);
document.execCommand('insertText', false, text);
const btn = sendControl();
if (!btn) return JSON.stringify({{ok: true, composed: text, sent: false, prepared: true, diagnosis: 'send_button_missing', activated: false, focus_theft: 0}});
btn.click();
const appeared = lastOut().indexOf(text.slice(0, 40)) !== -1;
return JSON.stringify({{ok: true, sent: appeared, verified_in_thread: appeared, text_fragment: text.slice(0,80), activated: false, focus_theft: 0}});
"""


def attach_js() -> str:
    """File inputs cannot be set from page JS. Never open a foreground picker."""
    return """
const input = document.querySelector('#main input[type="file"]') || document.querySelector('input[type="file"]');
return JSON.stringify({
  ok: false,
  sent: false,
  error: 'attachment_file_picker_blocked',
  diagnosis: 'cannot_set_file_input_from_js',
  file_input_present: !!input,
  activated: false,
  focus_theft: 0
});
"""
