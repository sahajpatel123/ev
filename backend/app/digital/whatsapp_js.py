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
  const icon = document.querySelector('[data-icon="wds-ic-send-filled"], [data-icon="send"], [data-testid="send"]');
  if (!icon) {
    return document.querySelector('footer button[aria-label*="Send" i], #main button[aria-label*="Send" i]');
  }
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
  if (seen.has(ref)) return;
  if (!q || isSelf || name.toLowerCase().includes(String(q).toLowerCase()) || /message yourself/i.test(String(q))) {{
    seen.add(ref);
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
const header = document.querySelector('[data-testid="conversation-info-header-chat-title"]');
const headerName = header ? (header.innerText || '').split('\\n')[0].trim() : '';
if (selfWant && document.querySelector('#main [data-testid="conversation-compose-box-input"]')) {{
  return JSON.stringify({{ok: true, chat_ref: '__self__', name: headerName || 'Message yourself', already: true, activated: false, focus_theft: 0}});
}}
if (want && headerName && want.toLowerCase() === headerName.toLowerCase() && document.querySelector('#main')) {{
  return JSON.stringify({{ok: true, chat_ref: want, name: headerName, already: true, activated: false, focus_theft: 0}});
}}
if (selfWant) {{
  const row = document.querySelector('[data-testid="message-yourself-row"]');
  if (row) {{
    row.click();
    return JSON.stringify({{ok: true, chat_ref: '__self__', name: chatName(row) || 'Message yourself', activated: false, focus_theft: 0}});
  }}
}}
const nodes = chatNodes();
const wantDigits = String(want).replace(/\\D+/g, '');
let found = null;
for (const n of nodes) {{
  const name = chatName(n);
  const sec = ((n.querySelector('[data-testid="cell-frame-secondary"]') || {{}}).innerText || '');
  if (name === want || name.toLowerCase() === String(want).toLowerCase()) {{ found = n; break; }}
  if (selfWant && /message yourself/i.test(sec)) {{ found = n; break; }}
  if (!found && wantDigits.length >= 7) {{
    const hay = (String(n.getAttribute('data-id') || '') + ' ' + String(n.innerText || '') + ' ' + String(sec || '')).replace(/\\D+/g, '');
    if (hay.includes(wantDigits)) {{ found = n; break; }}
  }}
}}
if (!found) return JSON.stringify({{ok: false, error: 'chat_not_found', sent: false, activated: false, focus_theft: 0}});
found.click();
const opened = chatName(found);
const isSelf = found.getAttribute('data-testid') === 'message-yourself-row' || /message yourself/i.test(((found.querySelector('[data-testid="cell-frame-secondary"]') || {{}}).innerText || ''));
return JSON.stringify({{ok: true, chat_ref: isSelf ? '__self__' : opened, name: opened, activated: false, focus_theft: 0}});
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
  msgs.push({{id: String(msgs.length+1), from_me: fromMe, text: text.slice(0, 500), timestamp: pre.slice(0, 80), sender: fromMe ? 'owner' : title}});
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
const box = composeBox();
if (!box) return JSON.stringify({{ok: false, error: 'compose_box_missing', sent: false, diagnosis: 'ui_changed', activated: false, focus_theft: 0}});
box.focus();
document.execCommand('selectAll', false, null);
const inserted = document.execCommand('insertText', false, text);
const now = (box.innerText || box.textContent || '').replace(/\\u200b/g, '').trim();
return JSON.stringify({{ok: inserted || now.length > 0, composed: text, present_len: now.length, sent: false, activated: false, focus_theft: 0}});
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
