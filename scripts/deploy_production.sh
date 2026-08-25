#!/bin/zsh
# P0 CLOSURE — SINGLE PRODUCTION DEPLOY AUTHORITY (PART 1).
# Thin launcher: all logic (flock, parity checks, kickstart, health wait,
# audit log) lives in embedded Python so the kernel lock spans the deploy.
exec python3 - "$@" <<'PY'
import fcntl, json, os, subprocess, sys, time, urllib.request
from datetime import datetime
from pathlib import Path

EVIE = "/Users/sahajpatel/code/ev"
ACTOR = os.environ.get("DEPLOY_ACTOR", "unspecified")
LOCK = Path.home() / "Library/Application Support/EV/deploy.lock"

def fail(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)

lock_file = open(LOCK, "a+")
try:
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    fail(409, "DEPLOY_REFUSED: another production deployment is in progress.")

def git(*a): return subprocess.run(["git", "-C", EVIE, *a], capture_output=True, text=True)

local = git("rev-parse", "HEAD").stdout.strip()
dirty = git("status", "--porcelain").stdout.strip()
subprocess.run(["git", "-C", EVIE, "fetch", "origin", "main"], capture_output=True)
remote = git("rev-parse", "origin/main").stdout.strip()

if local != remote: fail(400, f"DEPLOY_REFUSED: local HEAD {local[:10]} != origin/main {remote[:10]} (push first).")
if dirty: fail(400, "DEPLOY_REFUSED: working tree dirty.")

subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/ev.api"], capture_output=True)

expected = local[:10]; health_sha = None; got = False
for _ in range(30):
    time.sleep(5)
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/v1/health", timeout=6) as resp:
            got = True
            health_sha = json.load(resp).get("git", {}).get("sha", "")[:10]
        if health_sha == expected: break
    except Exception:
        continue

LOG = Path.home() / "Library/Application Support/EV/incidents/deploy-log.txt"
LOG.parent.mkdir(parents=True, exist_ok=True)
with LOG.open("a") as f:
    f.write(f"{datetime.now():%F %T %Z} actor={ACTOR} sha={local} health={health_sha or 'none'} up={'yes' if got else 'no'}\n")

print(f"DEPLOYED sha={local} health={health_sha or 'none'}")
PY
