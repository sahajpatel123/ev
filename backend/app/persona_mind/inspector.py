"""MIND INSPECTOR: local read-only window into Evie's mind.

Usage: python -m app.persona_mind.inspector [--owner owner]
Serves one self-contained page on 127.0.0.1 with a random port and URL
token. All real-DB access is through a read-only SQLite URI; the lab tab
uses its own database file so it never pollutes the real mind.
"""

from __future__ import annotations

import secrets
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Evie Mind Inspector</title>
<style>
:root { --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --line: #e2e2e2;
  --card: #f7f7f5; --accent: #0b6e4f; --warn: #b3541e; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #141412; --fg: #e8e6e1; --muted: #a3a099; --line: #2e2e2b;
    --card: #1d1d1b; --accent: #4cc38a; --warn: #e0a060; }
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); font: 14px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif;
  margin: 0 auto; max-width: 980px; padding: 20px 16px 60px; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
nav { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 16px; }
nav button { background: var(--card); color: var(--fg); border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
nav button.on { border-color: var(--accent); color: var(--accent); font-weight: 600; }
section.tab { display: none; }
section.tab.on { display: block; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 12px; }
.card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--muted); margin: 0 0 8px; font-weight: 600; }
pre { font-family: var(--mono); font-size: 12px; white-space: pre-wrap; margin: 0; }
.mono { font-family: var(--mono); font-size: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
td, th { text-align: left; padding: 4px 8px 4px 0; border-bottom: 1px solid var(--line);
  vertical-align: top; }
.bar { height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; min-width: 90px; }
.bar > div { height: 100%; background: var(--accent); }
.warn { color: var(--warn); }
#lablog { max-height: 300px; overflow-y: auto; }
#lablog div { margin-bottom: 8px; }
#lablog .me { color: var(--muted); }
form.lab { display: flex; gap: 8px; margin-top: 8px; }
form.lab input { flex: 1; padding: 8px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--bg); color: var(--fg); font-size: 14px; }
form.lab button { padding: 8px 16px; border-radius: 8px; border: 1px solid var(--accent);
  background: var(--accent); color: #fff; cursor: pointer; }
code { font-family: var(--mono); font-size: 12px; }
</style>
</head>
<body>
<h1>Evie Mind Inspector</h1>
<div class="sub" id="status">connecting…</div>
<nav id="tabs"></nav>
<section class="tab" id="t-overview"></section>
<section class="tab" id="t-opinions"></section>
<section class="tab" id="t-threads"></section>
<section class="tab" id="t-timeline"></section>
<section class="tab" id="t-dreams"></section>
<section class="tab" id="t-lab"></section>
<section class="tab" id="t-perf"></section>
<script>
const TABS = [["t-overview","Overview"],["t-opinions","Opinions"],["t-threads","Threads & bits"],
  ["t-timeline","Timeline"],["t-dreams","Dreams"],["t-lab","Lab chat"],["t-perf","Latency"]];
let cur = "t-overview", S = null;
const nav = document.getElementById("tabs");
TABS.forEach(([id, label]) => {
  const b = document.createElement("button");
  b.textContent = label; b.dataset.tab = id;
  b.onclick = () => { cur = id; paint(); };
  nav.appendChild(b);
});
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function bar(v) { const p = Math.round(100 * Math.min(1, Math.max(0, v)));
  return `<div class="bar"><div style="width:${p}%"></div></div>`; }
function card(title, inner) { return `<div class="card"><h2>${esc(title)}</h2>${inner}</div>`; }
function rows(pairs) { return "<table>" + pairs.map(([k,v]) => `<tr><td><b>${esc(k)}</b></td><td>${v}</td></tr>`).join("") + "</table>"; }
async function tick() {
  try {
    const r = await fetch("api/state");
    S = await r.json();
    document.getElementById("status").textContent =
      `owner ${esc(S.owner)} · state v${S.state_version} · age ${S.state_age_s.toFixed(0)}s · updated ${new Date().toLocaleTimeString()}`;
    paint();
  } catch (e) { document.getElementById("status").textContent = "disconnected — retrying…"; }
}
function paint() {
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("on", b.dataset.tab === cur));
  document.querySelectorAll("section.tab").forEach(s => s.classList.toggle("on", s.id === cur));
  if (!S) return;
  const d = S.data || {}, room = S.room || {};
  document.getElementById("t-overview").innerHTML =
    card("Affect", "<table>" + Object.entries(d.affect || {}).map(([k,v]) =>
      `<tr><td style="width:110px">${esc(k)}</td><td style="width:130px">${bar(v)}</td><td class="mono">${Number(v).toFixed(2)}</td></tr>`).join("") + "</table>" +
      `<p>Mood: <b>${esc((d.mood_words||[]).map(w=>w.word||w).join(", ") || "—")}</b></p>`) +
    card("Room", rows([["pace", `${esc(room.pace||"—")} (${room.pace_conf??"—"})`],
      ["focus", `${esc(room.focus||"—")} (${room.focus_conf??"—"})`],
      ["banter", room.banter ?? "—"], ["line", esc(room.room_line||"—")]])) +
    card("Latest MIND block", `<pre>${esc(S.mind_block || "—")}</pre><p class="mono">tokens: ${S.mind_tokens ?? "—"} · budget 120</p>`);
  document.getElementById("t-opinions").innerHTML =
    card(`Opinions in play (${(d.opinions||[]).length})`, "<table>" + (d.opinions||[]).map(o =>
      `<tr><td><b>${esc(o.topic)}</b><br>${esc(o.stance)}<br><span style="color:var(--muted)">${esc(o.reason||"")}</span></td><td class="mono">${Number(o.confidence).toFixed(2)}</td></tr>`).join("") + "</table>") +
    card("Changed mind", (d.changed_mind||[]).map(c =>
      `<p>${esc(c.topic)}: <s>${esc(c.old)}</s> → <b>${esc(c.new)}</b><br><span style="color:var(--muted)">${esc(c.reason)}</span></p>`).join("") || "<p>—</p>");
  document.getElementById("t-threads").innerHTML =
    card("Attitudes", (d.attitudes||[]).map(a => `<p><b>${esc(a.topic)}</b> (${Number(a.strength).toFixed(2)}): ${esc(a.text)}</p>`).join("") || "<p>—</p>") +
    card("Intentions", (d.intentions||[]).map(i => `<p>${esc(i.text)} <span style="color:var(--muted)">— ${esc(i.why||"")}</span></p>`).join("") || "<p>—</p>") +
    card("Open threads", (d.threads||[]).map(t => `<p>${esc(t.text)}</p>`).join("") || "<p>—</p>") +
    card("Running bits", (d.bits||[]).map(b => `<p>“${esc(b.phrase)}” <span class="mono">×${b.positives}</span></p>`).join("") || "<p>—</p>") +
    card("Active lessons", (S.lessons||[]).map(l => `<p>${esc(l.text)} <span class="mono">+${l.reinforced||0}</span></p>`).join("") || "<p>—</p>");
  document.getElementById("t-timeline").innerHTML =
    card("Reflections", (S.reflections||[]).map(r =>
      `<p class="mono">${esc(r.ts)} · ${esc((r.turn_ids||[]).join(", "))} · ${esc(JSON.stringify(r.summary||{}))}</p>`).join("") || "<p>—</p>") +
    card("Validator rejections", (S.rejections||[]).map(r =>
      `<p><b class="warn">${esc(r.kind)}</b> <span class="mono">${esc(r.item)}</span><br>${esc(r.reason)}</p>`).join("") || "<p>—</p>");
  document.getElementById("t-dreams").innerHTML =
    card("Dream history", (S.dreams||[]).map(g => {
      const s = g.summary || {};
      return `<p><b>${esc(g.day)}</b> ${s.applied ? "applied v" + s.version : "discarded"} · moments ${(s.moments||[]).length} · wins ${(s.counterfactuals||{}).wins ?? 0} · canary ${((s.canary||{}).wins ?? 0) + "/" + ((s.canary||{}).scenarios ?? 0)} · calls ${s.calls_used ?? 0}${s.discarded ? "<br>reason: " + esc(s.discarded) : ""}</p>`; }).join("") || "<p>—</p>") +
    card("Config versions", (S.configs||[]).map(c =>
      `<p class="mono">${c.version === S.config_version ? "*" : "&nbsp;"} v${c.version} ${esc(c.created_at)} ${esc(c.diff)}</p>`).join("") +
      `<p>Rollback: <code>python -m app.persona_mind.rollback --to &lt;version&gt;</code></p>`);
  document.getElementById("t-lab").innerHTML =
    card("Lab chat (own namespace, tools disabled)", `<div id="lablog"></div>
      <form class="lab" id="labform"><input id="labin" autocomplete="off" placeholder="Talk to Evie (persona + mind)…"><button>Send</button></form>
      <p class="sub">Lab turns never touch the real mind. First reply may take a few seconds.</p>`);
  const form = document.getElementById("labform");
  if (form && !form.dataset.wired) { form.dataset.wired = "1";
    form.onsubmit = async (e) => {
      e.preventDefault();
      const input = document.getElementById("labin");
      const msg = input.value.trim();
      if (!msg) return;
      input.value = "";
      const log = document.getElementById("lablog");
      log.innerHTML += `<div class="me">you: ${esc(msg)}</div>`;
      try {
        const r = await fetch("api/lab", {method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({message: msg})});
        const j = await r.json();
        log.innerHTML += `<div>evie: ${esc(j.reply || j.error || "(no reply)")}</div>`;
      } catch (e) { log.innerHTML += `<div class="warn">error: ${esc(e)}</div>`; }
      log.scrollTop = log.scrollHeight;
    };
  }
  const L = S.latency || {};
  document.getElementById("t-perf").innerHTML =
    card("Reply-path latency", rows([
      ["snapshot p50/p99", `${L.snapshot_p50_ms ?? "—"} / ${L.snapshot_p99_ms ?? "—"} ms`],
      ["observe p50/p99 (temp DB)", `${L.observe_p50_ms ?? "—"} / ${L.observe_p99_ms ?? "—"} ms`],
      ["note", "snapshot measured live on this DB (read-only); observe on an ephemeral DB, same code path."]]));
}
tick();
setInterval(() => { if (cur !== "t-lab") tick(); }, 1000);
</script>
</body>
</html>
"""

_latency_cache: dict[str, Any] = {"ts": 0.0, "data": {}}


def build_state(owner_id: str) -> dict[str, Any]:
    """Full inspector snapshot from the active store scope (read-only safe)."""
    from app.persona_mind import store
    from app.persona_mind.fast import snapshot
    from app.persona_mind.room import read_room
    from app.persona_mind.state import apply_decay, coerce_ts
    from app.persona_mind.tune import active as active_config

    now = datetime.now().astimezone()
    row = store.get_state(owner_id)
    data = dict(row["data"]) if row else {}
    vector = apply_decay(data, now) if data else {}
    recent = store.recent_turns(owner_id, 1)
    last_query = str(recent[0].get("owner_msg") or "") if recent else ""
    try:
        active = active_config()
    except Exception:
        active = {"tunables": {}, "lessons": [], "exemplars": [], "version": 0}
    try:
        reading = read_room(
            owner_id, last_query, now=now,
            banter=float(data.get("banter_appetite", 0.5)),
            patterns=list(data.get("owner_patterns") or []),
        )
        room = {"pace": reading.pace, "pace_conf": reading.pace_conf,
                "focus": reading.focus, "focus_conf": reading.focus_conf,
                "banter": reading.banter, "room_line": reading.room_line,
                "features": reading.features}
    except Exception:
        room = {}
    try:
        block = snapshot(owner_id, "main", last_query, "text", 120)
    except Exception:
        block = None
    try:
        reflections = store.recent_reflections(30)
        rejections = store.recent_rejections(30)
        dreams = store.dream_history(10)
        configs = store.list_configs()
    except Exception:
        reflections, rejections, dreams, configs = [], [], [], []
    age = 0.0
    if row:
        try:
            age = max(0.0, (now - coerce_ts(row["updated_at"])).total_seconds())
        except (ValueError, TypeError):
            age = 0.0
    return {
        "owner": owner_id,
        "state_version": row["version"] if row else 0,
        "state_age_s": age,
        "data": {**data, "affect": vector},
        "room": room,
        "mind_block": block.text if block else None,
        "mind_tokens": block.token_estimate if block else None,
        "lessons": list(active.get("lessons") or []),
        "config_version": active.get("version", 0),
        "configs": configs,
        "reflections": reflections,
        "rejections": rejections,
        "dreams": dreams,
        "latency": measure_latency(owner_id),
    }


def measure_latency(owner_id: str) -> dict[str, Any]:
    """p50/p99 of snapshot (live, read-only) and observe (ephemeral DB)."""
    import time as _time

    if _time.time() - float(_latency_cache.get("ts", 0.0)) < 60.0:
        return dict(_latency_cache.get("data", {}))
    from app.persona_mind import store
    from app.persona_mind.fast import snapshot
    from app.persona_mind.paths import db_scope
    from app.persona_mind.types import TurnRecord

    out: dict[str, Any] = {}
    try:
        samples = []
        for i in range(100):
            start = _time.perf_counter()
            snapshot(owner_id, "main", f"probe {i % 5}", "text", 120)
            samples.append((_time.perf_counter() - start) * 1000.0)
        samples.sort()
        out["snapshot_p50_ms"] = round(samples[50], 3)
        out["snapshot_p99_ms"] = round(samples[99], 3)
    except Exception:
        pass
    try:
        with tempfile.TemporaryDirectory() as tmp, db_scope(
            str(Path(tmp) / "bench.sqlite")
        ):
            from app.persona_mind import observe

            now = datetime.now().astimezone()
            for i in range(20):
                observe(TurnRecord(
                    owner_id="bench", thread_id="t", turn_id=f"w{i}",
                    ts=now, owner_msg="m", evie_reply="r",
                    channel="text", audience="private", tone={},
                    reaction_to_prev=None, receipts=(), now_line=None,
                ))
            samples = []
            for i in range(200):
                record = TurnRecord(
                    owner_id="bench", thread_id="t", turn_id=f"b{i}",
                    ts=now, owner_msg="m", evie_reply="r",
                    channel="text", audience="private", tone={},
                    reaction_to_prev=None, receipts=(), now_line=None,
                )
                start = _time.perf_counter()
                observe(record)
                samples.append((_time.perf_counter() - start) * 1000.0)
            samples.sort()
            out["observe_p50_ms"] = round(samples[100], 3)
            out["observe_p99_ms"] = round(samples[198], 3)
    except Exception:
        pass
    finally:
        store.reset_connections()
    _latency_cache["ts"] = _time.time()
    _latency_cache["data"] = dict(out)
    return out


def _lab_db_path() -> Path:
    from app.persona_mind.paths import mind_db_path

    return mind_db_path().parent / "mind-lab.sqlite"


def lab_chat(message: str, owner_id: str = "lab") -> dict[str, Any]:
    """One lab turn: own DB file, full persona + mind block, tools disabled."""
    import asyncio
    from datetime import UTC

    from app.persona_mind import store
    from app.persona_mind.fast import invalidate, observe, snapshot
    from app.persona_mind.paths import db_scope
    from app.persona_mind.reflect import brain_available, ensure_state, reflect_batch
    from app.persona_mind.types import TurnRecord

    text = (message or "").strip()
    if not text:
        return {"error": "empty message"}
    if not brain_available():
        return {"error": "brain unavailable (set EV_CHAT_PROVIDER=muse + EV_ALLOW_REMOTE_CHAT=1)"}
    with db_scope(_lab_db_path()):
        try:
            from app.persona_mind import settings as mind_settings

            mind_settings.MIND_ENABLED = True
            now = datetime.now(UTC)
            record = TurnRecord(
                owner_id=owner_id, thread_id="lab", turn_id=f"lab-{int(now.timestamp() * 1000)}",
                ts=now, owner_msg=text, evie_reply="", channel="text",
                audience="private", tone={}, reaction_to_prev=None,
                receipts=(), now_line=None,
            )
            observe(record)
            queued = store.queued_turns(12)
            state = ensure_state(owner_id, now)

            def _sync(messages: list[dict[str, str]]) -> dict[str, Any] | None:
                from app.persona_mind.reflect import gateway_reflect

                try:
                    return asyncio.run(gateway_reflect(messages))
                except Exception:
                    return None

            result = reflect_batch(owner_id, queued, state, now, reflector=_sync)
            for kind, item, reason in result["rejected"]:
                store.log_rejection(kind, item, reason)
            store.put_state(owner_id, state)
            invalidate(owner_id)
            store.mark_done([str(t.get("turn_id")) for t in queued])
            mind = snapshot(owner_id, "lab", text, "text", 120)
            persona_text = ""
            try:
                from app.persona.compile import build_persona_block
                from app.persona.context import PersonaContext

                persona_text = build_persona_block(
                    PersonaContext(channel="text", owner_msg=text)
                ).text
            except Exception as exc:
                persona_text = f"(persona unavailable: {exc})"

            system = (
                f"{persona_text}\n\n{mind.text if mind else ''}\n\n"
                "Tools are disabled in this lab: answer from context only, "
                "never claim tool use, never ask for confirmation."
            )
            try:
                from uuid import uuid4

                from app.contracts import ChatMessage, RequestEnvelope
                from app.gateway.providers import get_chat_provider
                from app.gateway.service import ModelGateway

                gateway = ModelGateway(get_chat_provider())
                envelope = RequestEnvelope(
                    request_id=str(uuid4()), strategy={"mode": "mind_lab"},
                    metadata={"purpose": "persona-mind-lab", "actor": "persona_mind"},
                )
                call = asyncio.run(gateway.chat(
                    [ChatMessage(role="system", content=system),
                     ChatMessage(role="user", content=text)],
                    envelope=envelope, temperature=0.7,
                ))
            except Exception as exc:
                return {"error": f"lab reply failed: {exc}"}
            if getattr(call, "status", "") != "ok":
                return {"error": f"lab reply failed: {getattr(call, 'status', '')}"}
            reply = getattr(getattr(call, "result", None), "text", "") or ""
            return {"reply": reply, "mind_tokens": mind.token_estimate if mind else 0}
        finally:
            store.reset_connections()


def create_app(token: str, owner_id: str):  # noqa: ANN202 - FastAPI app
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="Evie Mind Inspector")
    prefix = f"/t/{token}"

    @app.middleware("http")
    async def _gate(request: Request, call_next):  # noqa: ANN001, ANN202
        if not request.url.path.startswith(prefix + "/") and request.url.path != prefix:
            return JSONResponse({"error": "not found"}, status_code=404)
        return await call_next(request)

    @app.get(prefix + "/", response_class=HTMLResponse)
    async def _page() -> str:
        return PAGE

    @app.get(prefix + "/api/state")
    async def _state() -> dict[str, Any]:
        try:
            return build_state(owner_id)
        except Exception as exc:
            return {"error": str(exc), "owner": owner_id}

    @app.post(prefix + "/api/lab")
    async def _lab(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return lab_chat(str(body.get("message", "")), owner_id="lab")
        except Exception as exc:
            return {"error": str(exc)}

    return app


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Serve the mind inspector.")
    parser.add_argument("--owner", default="owner")
    args = parser.parse_args(argv)

    import app.persona_mind.settings as mind_settings

    mind_settings.MIND_ENABLED = True
    token = secrets.token_urlsafe(16)
    app = create_app(token, args.owner)

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    print("Starting inspector on 127.0.0.1 (random port)...")

    import threading

    ready: list[str] = []
    original_startup = server.startup

    async def _startup(**kwargs: Any) -> None:
        await original_startup(**kwargs)
        for srv in server.servers:
            sockets = getattr(srv, "sockets", None) or [srv]
            for sock in sockets:
                getname = getattr(sock, "getsockname", None)
                if getname is None:
                    continue
                ready.append(f"http://127.0.0.1:{getname()[1]}/t/{token}/")

    server.startup = _startup  # type: ignore[method-assign,assignment]
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15.0
    while not ready and time.time() < deadline and thread.is_alive():
        time.sleep(0.1)
    if not ready:
        print("inspector failed to bind")
        return 1
    print(f"Mind inspector: {ready[0]}")
    print("Press Ctrl-C to stop.")
    try:
        while thread.is_alive():
            thread.join(timeout=1.0)
    except KeyboardInterrupt:
        server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
