"""Internal WhatsApp Web page scripts. Never projected to Muse."""

from __future__ import annotations

import json


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


def search_chats_js(query: str) -> str:
    q = json.dumps(query)
    return f"""
const q = {q};
const items = [];
const nodes = document.querySelectorAll('#pane-side [role="listitem"], [aria-label="Chat list"] [role="listitem"], [data-testid="cell-frame-container"]');
nodes.forEach((n) => {{
  const name = (n.getAttribute('title') || n.innerText || '').split('\\n')[0].trim();
  if (!name) return;
  if (!q || name.toLowerCase().includes(String(q).toLowerCase())) {{
    items.push({{chat_ref: name, name: name, unread: /\\d+/.test((n.innerText||'').split('\\n').slice(-1)[0] || '') ? 1 : 0}});
  }}
}});
return JSON.stringify({{ok: true, chats: items.slice(0, 20), authenticated: true, activated: false, focus_theft: 0}});
"""


def open_chat_js(chat_ref: str) -> str:
    ref = json.dumps(chat_ref)
    return f"""
const want = {ref};
const nodes = Array.from(document.querySelectorAll('#pane-side [role="listitem"], [aria-label="Chat list"] [role="listitem"], [data-testid="cell-frame-container"]'));
let found = null;
for (const n of nodes) {{
  const name = (n.getAttribute('title') || n.innerText || '').split('\\n')[0].trim();
  if (name === want || name.toLowerCase() === String(want).toLowerCase()) {{ found = n; break; }}
}}
if (!found) return JSON.stringify({{ok: false, error: 'chat_not_found', sent: false}});
found.click();
return JSON.stringify({{ok: true, chat_ref: want, name: want, activated: false, focus_theft: 0}});
"""


def read_recent_js(limit: int) -> str:
    cap = max(1, min(int(limit), 80))
    return f"""
const cap = {cap};
const header = document.querySelector('header') || document.querySelector('[data-testid="conversation-header"]');
const title = header ? (header.innerText || '').split('\\n')[0].trim() : '';
const msgs = [];
const rows = document.querySelectorAll('[data-testid="msg-meta"], [data-pre-plain-text], .message-in, .message-out, [data-testid="msg-container"]');
rows.forEach((row) => {{
  const pre = row.getAttribute('data-pre-plain-text') || '';
  const text = (row.innerText || '').trim();
  if (!text) return;
  const fromMe = row.className.includes('message-out') || /\\]\\s*You:/i.test(pre);
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
const rows = document.querySelectorAll('[data-testid="msg-container"], .message-in, .message-out');
rows.forEach((row) => {{
  const text = (row.innerText || '').trim();
  if (q && text.toLowerCase().includes(q)) {{
    hits.push({{text: text.slice(0, 400), from_me: row.className.includes('message-out')}});
  }}
}});
return JSON.stringify({{ok: true, messages: hits.slice(0, 30), complete_history: false, activated: false}});
"""


def compose_js(text: str) -> str:
    body = json.dumps(text)
    return f"""
const text = {body};
const box = document.querySelector('footer [contenteditable="true"]') || document.querySelector('[data-testid="conversation-compose-box-input"]');
if (!box) return JSON.stringify({{ok: false, error: 'compose_box_missing', sent: false, diagnosis: 'ui_changed'}});
box.focus();
document.execCommand('selectAll', false, null);
document.execCommand('insertText', false, text);
return JSON.stringify({{ok: true, composed: text, sent: false, activated: false, focus_theft: 0}});
"""


def send_js(text: str) -> str:
    body = json.dumps(text)
    return f"""
const text = {body};
const lastOut = () => {{
  const rows = Array.from(document.querySelectorAll('[data-testid="msg-container"], .message-out'));
  const last = rows[rows.length-1];
  return last ? (last.innerText || '') : '';
}};
const prior = lastOut();
if (prior && prior.indexOf(text) !== -1) {{
  return JSON.stringify({{ok: true, sent: true, duplicate_prevented: true, verified_in_thread: true, text_fragment: text.slice(0,80), activated: false}});
}}
const box = document.querySelector('footer [contenteditable="true"]') || document.querySelector('[data-testid="conversation-compose-box-input"]');
if (!box) return JSON.stringify({{ok: false, error: 'compose_box_missing', sent: false, diagnosis: 'ui_changed'}});
box.focus();
document.execCommand('selectAll', false, null);
document.execCommand('insertText', false, text);
const btn = document.querySelector('[data-testid="send"], [data-icon="send"], footer button[aria-label*="Send" i]');
if (!btn) return JSON.stringify({{ok: true, composed: text, sent: false, prepared: true, diagnosis: 'send_button_missing'}});
btn.click();
const appeared = lastOut().indexOf(text.slice(0, 40)) !== -1;
return JSON.stringify({{ok: true, sent: appeared, verified_in_thread: appeared, text_fragment: text.slice(0,80), activated: false, focus_theft: 0}});
"""
