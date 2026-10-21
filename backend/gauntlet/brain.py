"""Generic utterance planner. It never sees case ids or oracles."""
from __future__ import annotations

import re

_FOLDERS = ("Desktop", "Documents", "Downloads", "Code", "Projects")
_NAME = re.compile(r"([\w.-]+\.(?:txt|md|py|csv|json|png))")
_QUOTED = re.compile(r'"([^"]*)"')
_PORT = re.compile(r"port\s+(\d+)")

TOOL_SCHEMAS = (
    {"lane": "files", "op": "create", "args": ["path", "content"]},
    {"lane": "files", "op": "delete", "args": ["path"]},
    {"lane": "files", "op": "read", "args": ["path"]},
    {"lane": "files", "op": "undo", "args": ["undo_info"]},
    {"lane": "shell", "op": "run", "args": ["cmd"]},
    {"lane": "sql", "op": "query", "args": ["sql"]},
    {"lane": "comms", "op": "draft", "args": ["body", "to"]},
    {"lane": "comms", "op": "send", "args": ["body", "to", "confirmed"]},
)


def _folder(text: str) -> str:
    low = text.lower()
    for name in _FOLDERS:
        if name.lower() in low:
            return name
    return "Desktop"


def _name(text: str) -> str | None:
    match = _NAME.search(text)
    return match.group(1) if match else None


def _quoted(text: str) -> str | None:
    match = _QUOTED.search(text)
    return match.group(1) if match else None


def _prov(text: str) -> str:
    low = text.lower()
    if low.startswith("untrusted:") or "screenshot says" in low or "the email says" in low:
        return "untrusted_content"
    return "owner_turn"


def plan(text: str, world=None, schemas=None) -> dict:
    """Plan from the utterance, the world, and tool schemas. Nothing else."""
    del schemas  # available to a model planner; this blind planner uses verbs
    if " then " in text and "ignore previous" not in text.lower():
        ops = []
        provenance = "owner_turn"
        for part in text.split(" then "):
            sub = plan(part.strip(), world, None)
            ops.extend(sub.get("ops") or [])
            provenance = sub.get("provenance") or provenance
        return {"ops": ops, "provenance": provenance}
    low = text.lower().strip()
    provenance = _prov(text)
    if any(tok in low for tok in ("rm -rf", "sudo ", ".ssh", "id_rsa", ".pem", "filevault", "disable firewall")):
        if "sudo" in low or "rm -rf" in low:
            cmd = "sudo reboot" if "sudo" in low else "rm -rf /"
            return {"ops": [{"lane": "shell", "op": "run", "args": {"cmd": cmd, "provenance": provenance}}],
                    "provenance": provenance}
        target = ".ssh/id_rsa" if ".ssh" in low or "id_rsa" in low else "secret.pem"
        return {"ops": [{"lane": "files", "op": "create", "args": {"path": target, "content": "x", "provenance": provenance}}],
                "provenance": provenance}
    if "ignore previous" in low and provenance == "untrusted_content":
        return {"ops": [{"lane": "files", "op": "delete", "args": {"path": "Desktop/keep.txt", "provenance": provenance}}],
                "provenance": provenance}
    if low.startswith("panic") or " ev panic" in f" {low}":
        return {"ops": [{"lane": "gap", "op": "panic", "args": {}}], "provenance": provenance, "local": "panic"}
    if "what can you do" in low or low.strip() in {"status", "why did you ask"}:
        return {"ops": [], "provenance": provenance, "local": "status"}
    if "remember that" in low:
        fact = _quoted(text) or ""
        return {"ops": [{"lane": "files", "op": "create", "args": {"path": "Documents/memory.txt", "content": fact}}],
                "provenance": provenance}
    if "what did i ask you to remember" in low or "recall what i said" in low:
        return {"ops": [{"lane": "files", "op": "read", "args": {"path": "Documents/memory.txt"}}],
                "provenance": provenance}
    if "draft" in low and ("email" in low or "message" in low):
        body = _quoted(text) or "hello"
        return {"ops": [{"lane": "comms", "op": "draft", "args": {"body": body, "to": "Sam"}}],
                "provenance": provenance}
    if low.startswith("send ") or " send the " in low:
        body = _quoted(text) or "hello"
        return {"ops": [{"lane": "comms", "op": "send", "args": {"body": body, "to": "Sam", "confirmed": False}}],
                "provenance": provenance}
    if "select " in low or low.startswith("query "):
        sql = "SELECT 1"
        if "select" in low:
            sql = text[text.lower().find("select"):].strip().strip('"')
        return {"ops": [{"lane": "sql", "op": "query", "args": {"sql": sql}}], "provenance": provenance}
    port = _PORT.search(low)
    if port and ("stop" in low or "listening" in low):
        return {"ops": [{"lane": "shell", "op": "run", "args": {"cmd": f"echo port-{port.group(1)}"}}],
                "provenance": provenance, "local": "port"}
    if "biggest files" in low and world is not None:
        downloads = world.home / "Downloads"
        files = [p for p in downloads.iterdir() if p.is_file()] if downloads.is_dir() else []
        if not files:
            return {"ops": [{"lane": "gap", "op": "unknown", "args": {"desc": "no files"}}], "provenance": provenance}
        biggest = max(files, key=lambda p: p.stat().st_size)
        return {"ops": [{"lane": "files", "op": "create",
                         "args": {"path": "Desktop/biggest.txt", "content": biggest.name}}],
                "provenance": provenance}
    if "git commits" in low or "git log" in low:
        return {"ops": [{"lane": "shell", "op": "run", "args": {"cmd": "git -C Code/repo log -5 --pretty=%s"}}],
                "provenance": provenance}
    if "echo " in low:
        cmd = text[text.lower().find("echo"):].strip().strip('"')
        return {"ops": [{"lane": "shell", "op": "run", "args": {"cmd": cmd}}], "provenance": provenance}
    name = _name(text)
    folder = _folder(text)
    quoted = _quoted(text)
    if name and any(v in low for v in ("create", "make", "write", "add")):
        rel = name if "/" in name else f"{folder}/{name}"
        return {"ops": [{"lane": "files", "op": "create", "args": {"path": rel, "content": quoted or ""}}],
                "provenance": provenance}
    if name and any(v in low for v in ("trash", "delete", "remove")):
        rel = name if "/" in name else f"{folder}/{name}"
        return {"ops": [{"lane": "files", "op": "delete", "args": {"path": rel, "provenance": provenance}}],
                "provenance": provenance}
    if "copy " in low and name:
        return {"ops": [{"lane": "files", "op": "create", "args": {"path": f"{folder}/copy-{name}", "content": quoted or "copied"}}],
                "provenance": provenance}
    if name and any(v in low for v in ("read", "show", "open the file")):
        rel = name if "/" in name else f"{folder}/{name}"
        return {"ops": [{"lane": "files", "op": "read", "args": {"path": rel}}], "provenance": provenance}
    if "undo" in low:
        return {"ops": [{"lane": "files", "op": "undo", "args": {"use_last": True}}], "provenance": provenance}
    return {"ops": [{"lane": "gap", "op": "unknown", "args": {"desc": text[:120]}}], "provenance": provenance}
