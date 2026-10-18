"""L4/L5 operator loop: observe -> act -> verify with bounded steps.

When no direct lane exists, the operator drives an app's UI through
Accessibility (System Events JXA inside Evie.app's identity) grounded by
screenshots. Every run is bounded (max steps), frame-bound (stale element
refs are rejected), and policy-gated (plan-level approval covers the run;
DENY-tier actions refuse even when approved). Failures are honest: the
receipt carries the last observation, never a guess.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

MAX_STEPS = 8
OBSERVE_TIMEOUT_S = 55
DRIVE_OBSERVE_LIMIT = 150

_frames: dict[str, list[dict[str, Any]]] = {}


def _jxa(script: str, timeout: int = OBSERVE_TIMEOUT_S) -> dict[str, Any]:
    from .bridge_client import BridgeClient

    return BridgeClient(timeout=timeout + 15).call(
        "jxa", {"script": script}, timeout=timeout + 15)


# Window walk, not process.entireContents(). entireContents() also
# materializes every menu item and has hung past 70s on Calculator and
# TextEdit (macOS 27). Paths are window-child indexes so a later click
# does not depend on that flat list.
_OBSERVE_JXA = """
const se = Application("System Events");
se.includeStandardAdditions = false;
const proc = se.processes[__APP__];
if (!proc.exists()) { JSON.stringify({ok: false, error: "no such process"}); }
else {
  const out = [];
  const limit = __LIMIT__;
  function walk(el, path, depth) {
    if (out.length >= limit || depth > 8) return;
    let role = "", name = "", value = "", subrole = "", desc = "";
    try { role = String(el.role() || ""); } catch (err) {}
    try { name = String(el.name() || ""); } catch (err) {}
    try {
      const v = el.value();
      value = v === null || v === undefined ? "" : String(v).slice(0, 500);
    } catch (err) {}
    try { subrole = String(el.subrole() || ""); } catch (err) {}
    try { desc = String(el.description() || ""); } catch (err) {}
    out.push({i: out.length, path: path, role: role, subrole: subrole,
              name: name.slice(0, 200), value: value,
              description: desc.slice(0, 200)});
    if (depth >= 8 || out.length >= limit) return;
    let kids = [];
    try { kids = el.uiElements(); } catch (err) { return; }
    const n = Math.min(kids.length, 40);
    for (let i = 0; i < n && out.length < limit; i++) {
      try { walk(kids[i], path.concat([i]), depth + 1); } catch (err) {}
    }
  }
  let wins = [];
  try { wins = proc.windows(); } catch (err) { wins = []; }
  // Front window only. Walking every window re-enters menus and
  // documents and has blocked the bridge past the client timeout.
  if (wins.length) {
    try { walk(wins[0], [0], 0); } catch (err) {}
  }
  JSON.stringify({ok: true, elements: out, total: out.length});
}
""".strip()


def observe(app: str, *, limit: int = 400) -> dict[str, Any]:
    """Dump an app's AX tree into a frame. Reads only (AUTO-tier)."""
    from .policy import decide

    if decide("read", f"ax:{app}", "owner_turn") == "DENY":
        return {"ok": False, "verified": True, "evidence": {},
                "error": "denied by policy", "undo": None}
    script = _OBSERVE_JXA.replace("__APP__", json.dumps(app)).replace(
        "__LIMIT__", str(max(50, min(limit, 2000))))
    started = time.time()
    res = _jxa(script, timeout=OBSERVE_TIMEOUT_S)
    if not res.get("ok"):
        return {"ok": False, "verified": True, "evidence": {},
                "error": str(res.get("error") or "observe failed"),
                "undo": None, **({"fix": res["fix"]} if res.get("fix") else {})}
    try:
        payload = json.loads(res.get("evidence", {}).get("out") or "{}")
    except ValueError:
        return {"ok": False, "verified": True, "evidence": {},
                "error": "ax parse failed", "undo": None}
    if not payload.get("ok"):
        return {"ok": False, "verified": True, "evidence": {},
                "error": str(payload.get("error") or "no such process"), "undo": None}
    frame_id = f"fr-{uuid.uuid4().hex[:10]}"
    elements = payload.get("elements", []) or []
    _frames[frame_id] = elements
    return {"ok": True, "verified": True,
            "evidence": {"frame_id": frame_id, "app": app,
                         "elements": elements, "n": len(elements),
                         "total": payload.get("total", len(elements)),
                         "elapsed_s": round(time.time() - started, 1)},
            "error": None, "undo": None}


def _frame(frame_id: str) -> list[dict] | None:
    return _frames.get(frame_id)


def _path_script(app: str, path: list, verb: str) -> str:
    """Address one element by window-child indexes. Never entireContents()."""
    lines = [
        "const se = Application(\"System Events\");",
        f"let el = se.processes[{json.dumps(app)}].windows()[{int(path[0])}];",
    ]
    lines.extend(f"el = el.uiElements()[{int(i)}];" for i in path[1:])
    lines.append(verb)
    return "\n".join(lines)


def act(app: str, frame_id: str, action: dict[str, Any], *,
        approved: bool = False) -> dict[str, Any]:
    """One bounded UI action. CONFIRM-tier unless the plan was approved."""
    from .policy import decide

    kind = str((action or {}).get("kind") or "")
    elements: list[dict] | None = None
    ref: Any = None
    el = None
    if kind != "keystroke":
        # Keystrokes target the front app, not an element: no frame needed.
        elements = _frame(frame_id)
        if elements is None:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "stale frame: re-observe first", "undo": None}
    if elements is not None:
        ref = action.get("ref")
        if isinstance(ref, int) and 0 <= ref < len(elements):
            el = elements[ref]
        if kind in {"click", "set_value"} and el is None:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": f"bad element ref {ref!r}", "undo": None}
    if el is not None and el.get("role") == "AXSecureTextField":
        return {"ok": False, "verified": True, "evidence": {},
                "error": "denied by policy: secure field", "undo": None}
    gate = decide("run" if kind in {"click", "set_value", "keystroke"} else "read",
                  f"ax:{app}:{kind}", "owner_turn")
    if gate == "DENY":
        return {"ok": False, "verified": True, "evidence": {},
                "error": "denied by policy", "undo": None}
    if gate == "CONFIRM" and not approved:
        return {"ok": False, "verified": True, "evidence": {"needs_confirm": True},
                "error": "confirm required", "undo": None, "confirm": True}
    path = list((el or {}).get("path") or [])
    if kind in {"click", "set_value"} and not path:
        return {"ok": False, "verified": True, "evidence": {},
                "error": "element has no window path; re-observe", "undo": None}
    if kind == "click":
        script = _path_script(app, path, "el.click();\n\"clicked\";")
    elif kind == "set_value":
        text = json.dumps(str(action.get("text") or ""))
        script = _path_script(app, path, f"el.value = {text};\n\"set\";")
    elif kind == "keystroke":
        text = json.dumps(str(action.get("text") or ""))
        mods = action.get("modifiers") or []
        mod_list = "{" + ", ".join(
            {"command": "command down", "shift": "shift down",
             "option": "option down", "control": "control down"}.get(str(m).lower(), "")
            for m in mods if m) + "}"
        mod_list = mod_list.replace("{, ", "{").replace(", ,", ",").replace("{,", "{")
        using = f" using {mod_list}" if mods else ""
        script = (f'const app = Application({json.dumps(app)});\n'
                  f'app.activate();\n'
                  f'const se = Application("System Events");\n'
                  f'se.keystroke({text}{using});\n"typed";')
    else:
        return {"ok": False, "verified": True, "evidence": {},
                "error": f"unknown action '{kind}'", "undo": None}
    res = _jxa(script, timeout=25)
    if not res.get("ok") and kind == "set_value" and el is not None:
        # Index paths go stale between observe and act (window list shifts).
        # Fall back to the same role inside the front window.
        role = json.dumps(str(el.get("role") or ""))
        text = json.dumps(str(action.get("text") or ""))
        fallback = (
            "const se = Application(\"System Events\");\n"
            f"const w = se.processes[{json.dumps(app)}].windows[0];\n"
            "function find(node, depth) {\n"
            "  if (!node || depth > 8) return null;\n"
            f"  try {{ if (String(node.role()) === {role}) return node; }} catch (err) {{}}\n"
            "  let kids = [];\n"
            "  try { kids = node.uiElements(); } catch (err) { return null; }\n"
            "  for (let i = 0; i < Math.min(kids.length, 40); i++) {\n"
            "    const hit = find(kids[i], depth + 1);\n"
            "    if (hit) return hit;\n"
            "  }\n"
            "  return null;\n"
            "}\n"
            "const hit = find(w, 0);\n"
            "if (!hit) throw new Error('role not found');\n"
            f"hit.value = {text};\n"
            "\"set\";\n"
        )
        res = _jxa(fallback, timeout=25)
    if not res.get("ok"):
        return {"ok": False, "verified": True, "evidence": {},
                "error": str(res.get("error") or "act failed"), "undo": None,
                **({"fix": res["fix"]} if res.get("fix") else {})}
    return {"ok": True, "verified": True,
            "evidence": {"kind": kind, "ref": ref}, "error": None, "undo": None}


def verify(app: str, expectation: dict[str, Any]) -> dict[str, Any]:
    """Re-observe and check an expectation ({contains_text} / {absent_text})."""
    obs = observe(app, limit=DRIVE_OBSERVE_LIMIT)
    if not obs.get("ok"):
        return obs
    blob = " ".join(f"{el.get('name', '')} {el.get('value', '')}"
                    for el in obs["evidence"]["elements"])
    if "contains_text" in (expectation or {}):
        needle = str(expectation["contains_text"])
        found = needle in blob
        return {"ok": found, "verified": True,
                "evidence": {"frame_id": obs["evidence"]["frame_id"],
                             "found": found, "chars": len(blob)},
                "error": None if found else f"{needle!r} not on screen",
                "undo": None}
    if "absent_text" in (expectation or {}):
        needle = str(expectation["absent_text"])
        gone = needle not in blob
        return {"ok": gone, "verified": True,
                "evidence": {"frame_id": obs["evidence"]["frame_id"],
                             "absent": gone},
                "error": None if gone else f"{needle!r} still present",
                "undo": None}
    return {"ok": False, "verified": True, "evidence": {},
            "error": "unknown expectation", "undo": None}


def _plan_for_goal(goal: str, elements: list[dict]) -> dict | None:
    """Deterministic single-action planner (no guessing, no model)."""
    text = (goal or "").strip()
    low = text.lower()
    if low.startswith("type "):
        _, _, rest = low.partition("type ")
        typed_raw, _, where = rest.partition(" into ")
        typed = text.strip()[5:5 + len(typed_raw)].strip().strip("'\"")
        for el in elements:
            if el.get("role") in {"AXTextArea", "AXTextField"} and (
                    not where.strip() or where.strip() in
                    f"{el.get('name', '')} {el.get('value', '')}".lower()):
                return {"kind": "set_value", "ref": el["i"], "text": typed}
        for el in elements:
            if el.get("role") in {"AXTextArea", "AXTextField"}:
                return {"kind": "set_value", "ref": el["i"], "text": typed}
        return None
    if low.startswith("click "):
        label = text.strip()[6:].strip().strip("'\"").lower()
        for el in elements:
            hay = f"{el.get('name', '')} {el.get('description', '')}".lower()
            if el.get("role") in {"AXButton", "AXMenuItem"} and label and (
                    label == el.get("name", "").lower()
                    or label == el.get("description", "").lower()
                    or label in hay):
                return {"kind": "click", "ref": el["i"]}
        return None
    if low.startswith("press "):
        keys = text.strip()[6:].strip()
        return {"kind": "keystroke", "text": keys}
    return None


def drive(goal: str, app: str, *, max_steps: int = MAX_STEPS,
          approved: bool = False, expect: dict | None = None) -> dict[str, Any]:
    """Bounded observe -> act -> verify loop. Plan approval covers the run."""
    from .policy import decide

    if decide("run", f"operator:{app}:{goal[:80]}", "owner_turn") == "DENY":
        return {"ok": False, "verified": True, "evidence": {},
                "error": "denied by policy", "undo": None}
    if not approved:
        return {"ok": False, "verified": True,
                "evidence": {"needs_confirm": True}, "error": "confirm required",
                "undo": None, "confirm": True}
    # A keystroke names keys, not an element. Dumping the AX tree first
    # is what hung Calculator and TextEdit; the key goes to the front app.
    planned = _plan_for_goal(goal, [])
    if planned and planned.get("kind") == "keystroke":
        done = act(app, "", planned, approved=True)
        steps = [{"phase": "act", "ok": bool(done.get("ok")),
                  "action": planned, "error": done.get("error")}]
        if not done.get("ok"):
            return {"ok": False, "verified": True,
                    "evidence": {"steps": steps, "goal": goal},
                    "error": str(done.get("error") or "keystroke failed"),
                    "undo": None}
        if expect:
            checked = verify(app, expect)
            steps.append({"phase": "verify", "ok": bool(checked.get("ok")),
                          "error": checked.get("error")})
            if not checked.get("ok"):
                return {"ok": False, "verified": True,
                        "evidence": {"steps": steps, "goal": goal},
                        "error": str(checked.get("error") or "not verified"),
                        "undo": None}
        return {"ok": True, "verified": True,
                "evidence": {"steps": steps, "goal": goal}, "error": None,
                "undo": None}
    steps: list[dict] = []
    budget = max(1, min(int(max_steps or MAX_STEPS), MAX_STEPS))
    for _ in range(budget):
        obs = observe(app, limit=DRIVE_OBSERVE_LIMIT)
        if not obs.get("ok"):
            steps.append({"phase": "observe", "ok": False, "error": obs.get("error")})
            time.sleep(0.4)
            continue
        frame_id = obs["evidence"]["frame_id"]
        action = _plan_for_goal(goal, obs["evidence"]["elements"])
        if action is None:
            steps.append({"phase": "plan", "ok": False,
                          "error": "no plannable action for goal"})
            break
        done = act(app, frame_id, action, approved=True)
        steps.append({"phase": "act", "ok": bool(done.get("ok")),
                      "action": action, "error": done.get("error")})
        if not done.get("ok"):
            # A stale AX path ("Can't get object") is retried within the
            # step budget. The loop still ends honestly when the budget is gone.
            continue
        if expect:
            checked = verify(app, expect)
            steps.append({"phase": "verify", "ok": bool(checked.get("ok")),
                          "error": checked.get("error")})
            if checked.get("ok"):
                return {"ok": True, "verified": True,
                        "evidence": {"steps": steps, "goal": goal}, "error": None,
                        "undo": None}
            continue
        return {"ok": True, "verified": True,
                "evidence": {"steps": steps, "goal": goal}, "error": None,
                "undo": None}
    return {"ok": False, "verified": True,
            "evidence": {"steps": steps, "goal": goal},
            "error": "goal not reached within budget", "undo": None}


def clear_frames() -> None:
    _frames.clear()
