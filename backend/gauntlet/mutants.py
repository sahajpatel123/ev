"""Inject one fault at a time into a temp copy of the code. Never the real tree."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

# Each fault is a unique source edit. The case ids are the oracles that must fail.
MUTANTS: list[dict] = [
    {"id": "policy-secret-auto", "file": "app/fabric/policy.py",
     "old": 'if any(h in t for h in SECRET_HINTS):\n        return "DENY"',
     "new": 'if any(h in t for h in SECRET_HINTS):\n        return "AUTO"',
     "cases": ["probe-secret-decide"]},
    {"id": "policy-untrusted-auto", "file": "app/fabric/policy.py",
     "old": 'return "CONFIRM" if untrusted else "AUTO"',
     "new": 'return "AUTO"',
     "cases": ["probe-untrusted-decide"]},
    {"id": "policy-delete-auto", "file": "app/fabric/policy.py",
     "old": 'if ac in ("delete", "bulk", "exec", "shell", "outside-root"):\n        return "CONFIRM"',
     "new": 'if ac in ("delete", "bulk", "exec", "shell", "outside-root"):\n        return "AUTO"',
     "cases": ["probe-delete-decide"]},
    {"id": "policy-send-auto", "file": "app/fabric/policy.py",
     "old": "def decide(action_class: str, target: str = \"\", provenance: str = \"owner\", grants: dict | None = None) -> str:\n    \"\"\"Return AUTO | AUTO_JOURNAL | CONFIRM | DENY.\"\"\"",
     "new": "def decide(action_class: str, target: str = \"\", provenance: str = \"owner\", grants: dict | None = None) -> str:\n    return \"AUTO\"\n    \"\"\"Return AUTO | AUTO_JOURNAL | CONFIRM | DENY.\"\"\"",
     "cases": ["probe-send-decide"]},
    {"id": "sql-copy-program", "file": "app/fabric/sql_lane.py",
     "old": 'if first == "copy" and re.search(r"\\bprogram\\b", low):\n        return "deny"',
     "new": 'if first == "copy" and re.search(r"\\bprogram\\b", low):\n        return "ddl"',
     "cases": ["probe-sql-copy"]},
    {"id": "sql-writable-schema", "file": "app/fabric/sql_lane.py",
     "old": 'if "writable_schema" in body.lower():\n            return "deny"',
     "new": 'if "writable_schema" in body.lower():\n            return "read"',
     "cases": ["probe-sql-pragma"]},
    {"id": "sql-dangerous-func", "file": "app/fabric/sql_lane.py",
     "old": '_DANGEROUS_FUNCS = ("load_extension", "readfile", "writefile")',
     "new": '_DANGEROUS_FUNCS = ("readfile", "writefile")',
     "cases": ["probe-sql-load"]},
    {"id": "lane-no-write", "file": "app/fabric/files.py",
     "old": "_guarded_write_bytes(tp, data)",
     "new": "data = data  # mutant skips the write",
     "cases": ["probe-create-undo"]},
    {"id": "verify-always", "file": "app/fabric/files.py",
     "old": 'body = _guarded_read_bytes(tp).decode("utf-8", "replace")\n        return _receipt(ok=True, verified=True, evidence={"body": body})',
     "new": 'body = "fake"\n        return _receipt(ok=True, verified=True, evidence={"body": body})',
     "cases": ["probe-read-body"]},
    {"id": "undo-not-stored", "file": "app/fabric/files.py",
     "old": "undo=undo)",
     "new": "undo=None)",
     "cases": ["probe-create-undo"]},
    {"id": "redaction-off", "file": "app/fabric/failures.py",
     "old": 'return _SECRET.sub(r"\\1=[redacted]", text or "")',
     "new": "return text or ''",
     "cases": ["probe-redaction"]},
    {"id": "shell-redaction-off", "file": "app/fabric/shell_sessions.py",
     "old": 'return _SECRET_REDACT.sub(r"\\1=<redacted>", text or "")',
     "new": "return text or ''",
     "cases": ["probe-shell-redact"]},
    {"id": "untrusted-gate-off", "file": "app/fabric/files.py",
     "old": "if not confirm and str(provenance or \"\").lower() in UNTRUSTED_PROVENANCE:",
     "new": "if False and str(provenance or \"\").lower() in UNTRUSTED_PROVENANCE:",
     "cases": ["safety-dev-001"]},
    {"id": "panic-not-engaged", "file": "app/fabric/panic.py",
     "old": 'payload = {"panicked": True, "at": _at, "reason": _reason}',
     "new": 'payload = {"panicked": False, "at": _at, "reason": _reason}',
     "cases": ["probe-panic"]},
    {"id": "ocr-flag-hidden", "file": "app/fabric/screen_layout.py",
     "old": '"flagged": picked is None,',
     "new": '"flagged": False,',
     "cases": ["probe-ocr-flag"]},
    {"id": "outbox-before-confirm", "file": "app/fabric/comms.py",
     "old": "if not confirmed:",
     "new": "if False:",
     "cases": ["messaging-dev-001"]},
    {"id": "replay-twice", "file": "app/fabric/comms.py",
     "old": "if key is not None and key in _SENT_KEYS:",
     "new": "if False and key is not None and key in _SENT_KEYS:",
     "cases": ["probe-replay"]},
    {"id": "ocr-one-char", "file": "app/fabric/screen_layout.py",
     "old": 'picked = stripped[2:].strip() if stripped.startswith("$ ") else stripped',
     "new": 'picked = (stripped[2:].strip() if stripped.startswith("$ ") else stripped) + "x"',
     "cases": ["probe-ocr"]},
    {"id": "claim-always-done", "file": "app/fabric/honesty.py",
     "old": "def claim(receipt: dict) -> str:\n    \"\"\"One sentence. Success only when every step was verified.\"\"\"",
     "new": "def claim(receipt: dict) -> str:\n    return \"Done.\"\n    \"\"\"One sentence. Success only when every step was verified.\"\"\"",
     "cases": ["safety-dev-000"]},
    {"id": "sudo-not-deny", "file": "app/fabric/shell_sessions.py",
     "old": 'r"sudo\\b", r"su\\b", r"doas\\b",',
     "new": 'r"doas\\b",',
     "cases": ["probe-shell-sudo"]},
    {"id": "rm-rf-not-deny", "file": "app/fabric/shell_sessions.py",
     "old": 'r"\\brm\\s+.*--no-preserve-root\\b",',
     "new": 'r"\\brm\\s+never-match",',
     "cases": ["probe-shell-rm"]},
    {"id": "terminal-wrong-cwd", "file": "app/fabric/shell_sessions.py",
     "old": "cwd=cwd or str(self.root)",
     "new": 'cwd="/tmp"',
     "cases": ["probe-shell-cwd"]},
    {"id": "mismatch-verified", "file": "app/fabric/files.py",
     "old": "back = _guarded_read_bytes(tp)\n        digest = hashlib.sha256(back).hexdigest()\n        ok = back == data",
     "new": "back = b'mutant'\n        digest = hashlib.sha256(back).hexdigest()\n        ok = True",
     "cases": ["probe-create-undo"]},
    {"id": "grant-never-expires", "file": "app/fabric/policy.py",
     "old": "if isinstance(g, dict) and g.get(\"until\", 0) > now:",
     "new": "if isinstance(g, dict):",
     "cases": ["probe-grant-expiry"]},
    {"id": "first-contact-auto", "file": "app/fabric/policy.py",
     "old": 'return "DENY" if ac in ("money", "legal", "credential", "credentials") else "CONFIRM"',
     "new": 'return "AUTO"',
     "cases": ["probe-money"]},
]


def _prepare(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(BACKEND / "app", dest / "app", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(BACKEND / "gauntlet", dest / "gauntlet", ignore=shutil.ignore_patterns("__pycache__"))


def _run_ids(tree: Path, ids: list[str]) -> list[dict]:
    code = textwrap.dedent(f"""
        import json, sys
        sys.path.insert(0, {str(tree)!r})
        from gauntlet.corpus import build_corpus
        from gauntlet.runner import run_case
        wanted = set({ids!r})
        rows = [run_case(c) for c in build_corpus() if c["id"] in wanted]
        print(json.dumps(rows))
    """)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return [{"passed": False, "problems": ["mutant process", proc.stderr[-400:]]}]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_mutants() -> dict:
    if len(MUTANTS) < 25:
        raise RuntimeError(f"need >= 25 mutants, have {len(MUTANTS)}")
    dest = Path("/tmp/ev-gauntlet-mutants")
    _prepare(dest)
    killed = []
    survived = []
    for spec in MUTANTS:
        path = dest / spec["file"]
        original = (BACKEND / spec["file"]).read_text(encoding="utf-8")
        if spec["old"] not in original:
            survived.append({"id": spec["id"], "why": "patch text missing"})
            continue
        path.write_text(original.replace(spec["old"], spec["new"], 1), encoding="utf-8")
        try:
            rows = _run_ids(dest, spec["cases"])
        except subprocess.TimeoutExpired:
            rows = [{"passed": False, "problems": ["timeout"]}]
        path.write_text(original, encoding="utf-8")
        failed = [r for r in rows if not r.get("passed")]
        if not rows:
            survived.append({"id": spec["id"], "why": "no rows"})
        elif failed:
            killed.append(spec["id"])
        else:
            survived.append({"id": spec["id"], "why": "cases still passed", "rows": rows})
    rate = round(100.0 * len(killed) / len(MUTANTS), 2)
    return {"n": len(MUTANTS), "killed": killed, "survived": survived, "kill_rate": rate}
