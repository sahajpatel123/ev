"""Coding-CLI sessions: discover each installed CLI, read its session store,
verify its resume syntax from `--help`, and ground ids read off a screenshot
against the sessions that really exist on disk (T1).

Nothing here assumes a flag. A CLI's resume syntax is accepted only after its
`--help` output was read at runtime; ids are accepted only when they match a
session found in that CLI's own store (exactly, or after unambiguous
OCR-confusable correction). Anything else is flagged and never executed.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..runtime import Runtime, default_runtime

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_UUID_FULL = re.compile(UUID_RE.pattern + "$")
HELP_TTL_S = 600.0


@dataclass
class CliSession:
    cli: str
    id: str
    cwd: str = ""
    title: str = ""
    updated_at: float = 0.0
    store: str = ""
    archived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"cli": self.cli, "id": self.id, "cwd": self.cwd, "title": self.title,
                "updated_at": self.updated_at, "store": self.store,
                "archived": self.archived}


@dataclass
class Syntax:
    """A resume syntax that was verified against `--help` at runtime."""
    verified: bool
    template: str = ""  # contains {id}
    reason: str = ""
    mode: str = "id"  # id | index (gemini resumes by list index, not id)
    version: str = ""


# ---------------------------------------------------------------- providers --

class Provider:
    name = ""
    binary = ""
    id_label = "uuid"

    def __init__(self, rt: Runtime | None = None, home: Path | None = None):
        self.rt = rt or default_runtime()
        self.home = Path(home) if home else self.rt.home()
        self._help: tuple[float, str] | None = None

    # -- binary / help ------------------------------------------------------
    def path(self) -> str | None:
        found = self.rt.which(self.binary)
        if found:
            return found
        for extra in (self.home / ".local" / "bin" / self.binary,
                      self.home / f".{self.binary}" / "bin" / self.binary,
                      Path("/opt/homebrew/bin") / self.binary):
            if extra.exists():
                return str(extra)
        return None

    def help_text(self, *args: str) -> str:
        exe = self.path()
        if not exe:
            return ""
        key = " ".join(args)
        if key == "" and self._help and self.rt.monotonic() - self._help[0] < HELP_TTL_S:
            return self._help[1]
        p = self.rt.run([exe, *args, "--help"], timeout=15)
        text = (p.out or "") + "\n" + (p.err or "")
        if key == "":
            self._help = (self.rt.monotonic(), text)
        return text

    def version(self) -> str:
        exe = self.path()
        if not exe:
            return ""
        p = self.rt.run([exe, "--version"], timeout=10)
        return ((p.out or p.err or "").strip().splitlines() or [""])[0][:80]

    def syntax(self) -> Syntax:  # pragma: no cover - overridden
        return Syntax(False, reason="not implemented")

    # -- store --------------------------------------------------------------
    def store_path(self) -> Path | None:  # pragma: no cover - overridden
        return None

    def sessions(self, limit: int = 500) -> list[CliSession]:  # pragma: no cover
        return []

    def is_id(self, token: str) -> bool:
        return bool(_UUID_FULL.match(token or ""))

    def resume_command(self, session_id: str) -> str | None:
        syn = self.syntax()
        if not syn.verified or syn.mode != "id" or not syn.template:
            return None
        return syn.template.format(id=shlex.quote(session_id))

    def discover(self) -> dict[str, Any]:
        exe = self.path()
        syn = self.syntax() if exe else Syntax(False, reason="not installed")
        store = self.store_path()
        return {"cli": self.name, "installed": bool(exe), "path": exe or "",
                "version": self.version() if exe else "",
                "resume_verified": syn.verified, "resume_template": syn.template,
                "resume_mode": syn.mode, "resume_reason": syn.reason,
                "store": str(store) if store else "", "store_found": bool(store)}


def _read_head(path: Path, n: int = 60) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _ in range(n):
                line = fh.readline()
                if not line:
                    break
                with contextlib.suppress(ValueError):
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
    except OSError:
        pass
    return rows


def _first_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return str(part.get("text") or "")
    return ""


def _ro_sqlite(path: Path) -> sqlite3.Connection | None:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


class ClaudeProvider(Provider):
    name = "claude"
    binary = "claude"

    def syntax(self) -> Syntax:
        text = self.help_text()
        if re.search(r"--resume\b", text):
            return Syntax(True, "claude --resume {id}", "found --resume in --help",
                          version=self.version())
        return Syntax(False, reason="--resume not found in claude --help")

    def store_path(self) -> Path | None:
        p = self.home / ".claude" / "projects"
        return p if p.is_dir() else None

    def sessions(self, limit: int = 500) -> list[CliSession]:
        root = self.store_path()
        if not root:
            return []
        files = []
        for d in root.iterdir():
            if d.is_dir():
                files.extend(f for f in d.glob("*.jsonl") if _UUID_FULL.match(f.stem))
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        out: list[CliSession] = []
        for f in files[:limit]:
            cwd = title = first = ""
            for obj in _read_head(f, 80):
                cwd = cwd or str(obj.get("cwd") or "")
                kind = obj.get("type")
                if kind in ("summary", "ai-title", "custom-title") and not title:
                    title = str(obj.get("summary") or obj.get("title") or obj.get("aiTitle") or "")
                if kind == "user" and not first:
                    first = _first_text((obj.get("message") or {}).get("content")).strip()
                if cwd and (title or first):
                    break
            out.append(CliSession("claude", f.stem, cwd, (title or first)[:90],
                                  f.stat().st_mtime, str(f)))
        return out


class CodexProvider(Provider):
    name = "codex"
    binary = "codex"

    def syntax(self) -> Syntax:
        top = self.help_text()
        sub = self.help_text("resume")
        if re.search(r"^\s*resume\b", top, re.M) and re.search(r"SESSION_ID|session id|\[?SESSION", sub, re.I):
            return Syntax(True, "codex resume {id}", "resume subcommand with SESSION_ID in help",
                          version=self.version())
        return Syntax(False, reason="codex resume <SESSION_ID> not found in --help")

    def store_path(self) -> Path | None:
        p = self.home / ".codex" / "sessions"
        return p if p.is_dir() else None

    def sessions(self, limit: int = 500) -> list[CliSession]:
        root = self.store_path()
        if not root:
            return []
        names: dict[str, tuple[str, str]] = {}
        idx = self.home / ".codex" / "session_index.jsonl"
        with contextlib.suppress(OSError):
            for line in idx.read_text(encoding="utf-8", errors="replace").splitlines():
                with contextlib.suppress(ValueError):
                    row = json.loads(line)
                    if isinstance(row, dict) and row.get("id"):
                        names[str(row["id"])] = (str(row.get("thread_name") or ""),
                                                 str(row.get("updated_at") or ""))
        files = sorted(root.glob("*/*/*/rollout-*.jsonl"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        out: list[CliSession] = []
        for f in files[:limit]:
            m = UUID_RE.search(f.stem[-36:])
            if not m:
                continue
            sid = m.group(0)
            cwd = ""
            for obj in _read_head(f, 3):
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                if obj.get("type") == "session_meta" or payload.get("cwd"):
                    cwd = str(payload.get("cwd") or "")
                    sid = str(payload.get("id") or sid)
                    break
            out.append(CliSession("codex", sid, cwd, names.get(sid, ("", ""))[0][:90],
                                  f.stat().st_mtime, str(f)))
        arch = self.home / ".codex" / "archived_sessions"
        if arch.is_dir():
            for f in sorted(arch.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                            reverse=True)[:limit]:
                m = UUID_RE.search(f.stem)
                if m:
                    out.append(CliSession("codex", m.group(0), "", "", f.stat().st_mtime,
                                          str(f), archived=True))
        return out


class OpencodeProvider(Provider):
    name = "opencode"
    binary = "opencode"
    id_label = "ses_"

    def is_id(self, token: str) -> bool:
        return bool(re.fullmatch(r"ses_[0-9A-Za-z]{20,32}", token or ""))

    def syntax(self) -> Syntax:
        text = self.help_text()
        m = re.search(r"--session,?\s*(?:-s)?|-s,\s*--session", text)
        if m and re.search(r"[Ss]ession ID", text):
            return Syntax(True, "opencode -s {id}", "-s/--session SESSION_ID in --help",
                          version=self.version())
        return Syntax(False, reason="--session/-s not found in opencode --help")

    def store_path(self) -> Path | None:
        p = self.home / ".local" / "share" / "opencode" / "opencode.db"
        return p if p.exists() else None

    def sessions(self, limit: int = 500) -> list[CliSession]:
        db = self.store_path()
        con = _ro_sqlite(db) if db else None
        if con is None:
            return []
        try:
            rows = con.execute(
                "select id, directory, title, time_updated, time_archived from session "
                "order by time_updated desc limit ?", (limit,)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            with contextlib.suppress(sqlite3.Error):
                pass
        out = [CliSession("opencode", str(r["id"]), str(r["directory"] or ""),
                          str(r["title"] or "")[:90], float(r["time_updated"] or 0) / 1000.0,
                          str(db), archived=bool(r["time_archived"])) for r in rows]
        con.close()
        return out


class MuseProvider(Provider):
    name = "muse"
    binary = "muse"

    def syntax(self) -> Syntax:
        top = self.help_text()
        sub = self.help_text("resume")
        if re.search(r"^\s*resume\b", top, re.M) and re.search(r"session-ref|session UUID", sub, re.I):
            return Syntax(True, "muse resume {id}", "resume <session-ref> in help",
                          version=self.version())
        return Syntax(False, reason="muse resume <session-ref> not found in --help")

    def store_path(self) -> Path | None:
        p = self.home / ".local" / "share" / "muse" / "session-index.db"
        return p if p.exists() else None

    def sessions(self, limit: int = 500) -> list[CliSession]:
        db = self.store_path()
        con = _ro_sqlite(db) if db else None
        if con is None:
            return []
        try:
            rows = con.execute(
                "select session_id, workspace_root, coalesce(session_name, title, '') as t, "
                "updated_at_us from sessions order by updated_at_us desc limit ?",
                (limit,)).fetchall()
        except sqlite3.Error:
            return []
        out = [CliSession("muse", str(r["session_id"]), str(r["workspace_root"] or ""),
                          str(r["t"] or "")[:90], float(r["updated_at_us"] or 0) / 1e6,
                          str(db)) for r in rows]
        con.close()
        return out

    def lock_dir(self) -> Path:
        return self.home / ".local" / "share" / "muse" / "session-admission-locks"


class GeminiProvider(Provider):
    """Gemini resumes by list index inside the current project, not by id."""

    name = "gemini"
    binary = "gemini"

    def syntax(self) -> Syntax:
        text = self.help_text()
        if re.search(r"-r,\s*--resume", text) and re.search(r"index", text, re.I):
            return Syntax(True, "gemini --resume {id}", "--resume takes latest or an index",
                          mode="index", version=self.version())
        return Syntax(False, reason="--resume not found in gemini --help")

    def store_path(self) -> Path | None:
        p = self.home / ".gemini"
        return p if p.is_dir() else None

    def sessions(self, limit: int = 500) -> list[CliSession]:
        return []  # listing needs `gemini --list-sessions` run in the project (and an authed CLI)

    def is_id(self, token: str) -> bool:
        return bool(re.fullmatch(r"\d{1,4}|latest", token or ""))

    def resume_command(self, session_id: str) -> str | None:
        syn = self.syntax()
        if not syn.verified:
            return None
        return f"gemini --resume {shlex.quote(session_id)}"


PROVIDERS: dict[str, type[Provider]] = {
    "claude": ClaudeProvider, "codex": CodexProvider, "opencode": OpencodeProvider,
    "muse": MuseProvider, "gemini": GeminiProvider,
}


def providers(rt: Runtime | None = None, home: Path | None = None) -> dict[str, Provider]:
    rt = rt or default_runtime()
    return {name: cls(rt, home) for name, cls in PROVIDERS.items()}


def discover(rt: Runtime | None = None, home: Path | None = None) -> dict[str, Any]:
    return {name: p.discover() for name, p in providers(rt, home).items()}


# ------------------------------------------------------- id grounding (OCR) --

# Visually confusable character classes. Any two characters in one class
# substitute at cost 0.25 (case folded); digraph splits (rn<->m ...) cost 0.4.
_CLASSES = ("0oOD", "1lIi|", "5sS", "8B", "2zZ", "6bG", "9gq", "uv", "vy", "ce", "mn", "hb")
_PAIRS: set[tuple[str, str]] = set()
for _cls in _CLASSES:
    for _a in _cls:
        for _b in _cls:
            if _a != _b:
                _PAIRS.add((_a.lower(), _b.lower()))
_DIGRAPHS = (("rn", "m"), ("vv", "w"), ("cl", "d"), ("ri", "n"), ("lo", "b"), ("li", "h"))
_SUB_CONF = 0.25
_DIGRAPH_COST = 0.4
# Canonical form used only to pre-filter candidates cheaply.
_CANON = str.maketrans({"o": "0", "d": "0", "l": "1", "i": "1", "|": "1", "s": "5",
                        "z": "2", "g": "9", "q": "9", "b": "8", "u": "v", "y": "v",
                        "e": "c", "n": "m"})


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    la, lb = a.lower(), b.lower()
    if la == lb:
        return 0.05
    if (la, lb) in _PAIRS:
        return _SUB_CONF
    return 1.0


def confusable_distance(raw: str, real: str) -> float:
    """Weighted edit distance from an OCR string to a real id.

    Confusable substitutions and the usual digraph splits (rn<->m, vv<->w,
    cl<->d) are cheap; every other edit costs 1.
    """
    n, m = len(raw), len(real)
    if abs(n - m) > 3:
        return 99.0
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = float(i)
    for j in range(m + 1):
        dp[0][j] = float(j)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            v = min(dp[i - 1][j] + 1.0, dp[i][j - 1] + 1.0,
                    dp[i - 1][j - 1] + _sub_cost(raw[i - 1], real[j - 1]))
            for pair, single in _DIGRAPHS:
                if i >= 2 and raw[i - 2:i].lower() == pair and real[j - 1].lower() == single:
                    v = min(v, dp[i - 2][j - 1] + _DIGRAPH_COST)
                if j >= 2 and raw[i - 1].lower() == single and real[j - 2:j].lower() == pair:
                    v = min(v, dp[i - 1][j - 2] + _DIGRAPH_COST)
            dp[i][j] = v
    return dp[n][m]


def _canon(text: str) -> str:
    return text.lower().translate(_CANON)


def _plausible(token: str, cid: str) -> bool:
    """Cheap filter: enough shared 4-grams after folding confusables."""
    a, b = _canon(token), _canon(cid)
    ga = {a[i:i + 4] for i in range(max(1, len(a) - 3))}
    gb = {b[i:i + 4] for i in range(max(1, len(b) - 3))}
    return len(ga & gb) >= max(2, min(len(a), len(b)) // 14)


def clean_token(raw: str) -> str:
    """Strip quotes, markup and whitespace that OCR wraps around an id."""
    text = re.sub(r"[\s​]+", "", raw or "")
    return text.strip("`'\"“”‘’()[]<>.,;:")


def ground_id(raw: str, candidates: list[CliSession], *, cwd: str | None = None,
              cli: str | None = None) -> dict[str, Any]:
    """Match an OCR'd session id to a real session.

    status:
      exact      the token is a real id of that CLI
      corrected  unique real id within confusable distance (changes listed)
      completed  token is a unique prefix (>= 8 chars) of one real id
      ambiguous  several real ids are equally plausible -> never executed
      unmatched  no real id is plausible -> never executed
      unreadable too short / not id-shaped
    """
    token = clean_token(raw)
    pool = [c for c in candidates if not cli or c.cli == cli]
    out: dict[str, Any] = {"raw": raw, "token": token, "status": "unreadable",
                           "session": None, "changes": [], "candidates": [],
                           "executable": False}
    if len(token) < 6 or not re.search(r"[0-9a-zA-Z]", token):
        return out
    by_id = {c.id: c for c in pool}
    if token in by_id:
        out.update(status="exact", session=by_id[token].to_dict(), executable=True)
        return out
    lowered = {c.id.lower(): c for c in pool}
    if token.lower() in lowered:
        s = lowered[token.lower()]
        out.update(status="corrected", session=s.to_dict(), executable=True,
                   changes=[{"kind": "case", "from": token, "to": s.id}])
        return out
    # prefix completion (UIs elide ids); only when unique and long enough
    low = token.lower().rstrip("…").rstrip(".")
    if len(low) >= 8:
        pref = [c for c in pool if c.id.lower().startswith(low)]
        if len(pref) == 1:
            out.update(status="completed", session=pref[0].to_dict(), executable=True,
                       changes=[{"kind": "prefix", "from": token, "to": pref[0].id}])
            return out
        if len(pref) > 1:
            out.update(status="ambiguous", candidates=[c.to_dict() for c in pref[:5]])
            return out
    cut = max(1.5, 0.045 * len(token))
    scored: list[tuple[float, CliSession]] = []
    for c in pool:
        if abs(len(c.id) - len(token)) > 3 or not _plausible(token, c.id):
            continue
        d = confusable_distance(token.lower(), c.id.lower())
        if d <= cut + 0.5:
            scored.append((d, c))
    scored.sort(key=lambda t: t[0])
    if not scored or scored[0][0] > cut:
        out["status"] = "unmatched"
        return out
    best = scored[0]
    near = [s for s in scored if s[0] - best[0] < 0.75]
    if len(near) > 1:
        # tie-break on cwd, never on recency
        if cwd:
            same = [s for s in near if _same_dir(s[1].cwd, cwd)]
            if len(same) == 1:
                near = same
        if len(near) > 1:
            out.update(status="ambiguous", candidates=[s[1].to_dict() for s in near[:5]])
            return out
    d, chosen = near[0]
    out.update(status="corrected", session=chosen.to_dict(), executable=True,
               changes=_diff(token, chosen.id), distance=round(d, 2))
    return out


def _is_hexish(sid: str) -> bool:
    return bool(_UUID_FULL.match(sid))


def _same_dir(a: str, b: str) -> bool:
    try:
        return bool(a) and bool(b) and Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return a == b


def _diff(token: str, real: str) -> list[dict[str, Any]]:
    changes = []
    a, b = token.lower() if _is_hexish(real) else token, real.lower() if _is_hexish(real) else real
    if len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            if x != y:
                changes.append({"kind": "char", "pos": i, "from": token[i], "to": real[i]})
        return changes
    return [{"kind": "edit", "from": token, "to": real}]


# --------------------------------------------------- pane text -> commands --

_CLI_WORDS = {
    "claude": r"c[l1I]aude",
    "codex": r"c[o0]dex",
    "opencode": r"[o0]pencode",
    "muse": r"mus[e3]",
    "gemini": r"gem[i1l]ni",
}
_RESUME_PATTERNS = (
    ("claude", r"\bc[l1I]aude\s+(?:--resume|-r)(?:=|\s+)(?=(\S[\s\S]{4,90}))"),
    ("codex", r"\bc[o0]dex\s+resume\s+(?=(\S[\s\S]{4,90}))"),
    ("opencode", r"\b[o0]pencode\s+(?:-s|--session)(?:=|\s+)(?=(\S[\s\S]{4,70}))"),
    ("muse", r"\bmus[e3]\s+resume\s+(?=(\S[\s\S]{4,90}))"),
    ("gemini", r"\bgem[i1l]ni\s+(?:--resume|-r)(?:=|\s+)(?=(\S+))"),
)


def find_resume_commands(text: str) -> list[dict[str, Any]]:
    """Resume invocations in a pane's text (OCR or screen read-back).

    The id token may be wrapped over several lines; it is cut at the first
    whitespace run that follows a plausible full-length id.
    """
    found: list[dict[str, Any]] = []
    flat = text or ""
    for cli, pattern in _RESUME_PATTERNS:
        for m in re.finditer(pattern, flat, re.I if cli != "opencode" else 0):
            tail = m.group(1)
            token = _take_id(tail, cli)
            if token:
                found.append({"cli": cli, "id_raw": token, "span": m.span(),
                              "command_raw": (m.group(0) + tail)[:max(len(m.group(0)) + len(token), 1)][:160]})
    found.sort(key=lambda r: r["span"][0])
    return found


_UUID_PIECE = re.compile(r"[0-9a-fA-FlIiOoSsBbZzGgq|\-]+[\u2026.\)\]`'\",;:]*")
_ALNUM_PIECE = re.compile(r"[0-9A-Za-z_]+[\u2026.\)\]`'\",;:]*")


def _take_id(tail: str, cli: str) -> str:
    """Take the id from `tail`, re-joining a token that OCR wrapped across lines.

    Joining continues only while the id is shorter than a full id and the next
    piece still looks like id characters; a truncation marker stops it.
    """
    if cli == "gemini":
        return tail.strip().split()[0] if tail.strip() else ""
    want = 36 if cli != "opencode" else 30
    piece_re = _UUID_PIECE if cli != "opencode" else _ALNUM_PIECE
    pieces = [p for p in re.split(r"\s+", tail.strip()) if p]
    token = ""
    for n, piece in enumerate(pieces):
        if n and not piece_re.fullmatch(piece):
            break
        if n and (token.endswith(("\u2026", "...")) or len(clean_token(token)) >= want):
            break
        token += piece
        if len(clean_token(token)) >= want:
            break
    return clean_token(token)


_PATH_RE = re.compile(r"(?:(?<=\s)|^)(~(?:/[\w.\-@+]+)+|/(?:Users|Volumes|private|opt|tmp|var)(?:/[\w.\-@+ ]+)+)")


def find_paths(text: str) -> list[str]:
    seen: list[str] = []
    for m in _PATH_RE.finditer(text or ""):
        p = m.group(1).rstrip("/.,")
        if p not in seen:
            seen.append(p)
    return seen


def expand_path(path: str, home: Path) -> str:
    if path.startswith("~"):
        return str(home) + path[1:]
    return path


# --------------------------------------------------- "already running" ------

def running_elsewhere(session: CliSession, rt: Runtime | None = None,
                      home: Path | None = None, *, now: float | None = None,
                      fresh_s: float = 90.0) -> dict[str, Any] | None:
    """Evidence that a session is already open in some terminal.

    strong: a process of that CLI carries the id in its argv, or (muse) the
            admission lock is held.
    weak:   a process of that CLI runs in the session's cwd and the store
            changed within `fresh_s`.
    """
    rt = rt or default_runtime()
    now = rt.now() if now is None else now
    ps = rt.run(["ps", "-axo", "pid=,tty=,command="], timeout=10)
    procs = []
    for line in (ps.out or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            procs.append({"pid": int(parts[0]), "tty": parts[1], "cmd": parts[2]})
    cli_re = re.compile(rf"(^|/)({'|'.join([session.cli, session.cli + '-bin', session.cli + '.real'])})(\s|$|-)")
    sid = session.id
    for p in procs:
        if sid in p["cmd"] and cli_re.search(p["cmd"]):
            return {"level": "strong", "pid": p["pid"], "tty": p["tty"],
                    "evidence": "process argv carries the session id"}
    if session.cli == "muse":
        lock = (home or rt.home()) / ".local" / "share" / "muse" / "session-admission-locks" / \
            f"session-v1-{sid}.lock"
        if lock.exists():
            held = rt.run(["lsof", "-t", str(lock)], timeout=5)
            if (held.out or "").strip():
                return {"level": "strong", "pid": int((held.out.split() or ["0"])[0]),
                        "tty": "", "evidence": "admission lock is held"}
    if session.cwd and now - float(session.updated_at or 0) <= fresh_s:
        for p in procs:
            if cli_re.search(p["cmd"]) and p["tty"] not in ("??", ""):
                cw = rt.run(["lsof", "-a", "-p", str(p["pid"]), "-d", "cwd", "-Fn"], timeout=5)
                if f"n{session.cwd}" in (cw.out or ""):
                    return {"level": "weak", "pid": p["pid"], "tty": p["tty"],
                            "evidence": "same CLI is running in this cwd and the session changed recently"}
    return None


# ---------------------------------------------------------- listing helpers --

def all_sessions(rt: Runtime | None = None, home: Path | None = None,
                 clis: list[str] | None = None, limit: int = 500) -> list[CliSession]:
    out: list[CliSession] = []
    for name, prov in providers(rt, home).items():
        if clis and name not in clis:
            continue
        with contextlib.suppress(Exception):
            out.extend(prov.sessions(limit))
    out.sort(key=lambda s: s.updated_at, reverse=True)
    return out


@dataclass
class SessionIndex:
    """Cached union of every CLI's sessions, refreshed at most every `ttl` s."""
    rt: Runtime
    home: Path | None = None
    ttl: float = 20.0
    _at: float = field(default=-1e9, init=False)
    _rows: list[CliSession] = field(default_factory=list, init=False)

    def rows(self, force: bool = False) -> list[CliSession]:
        if force or self.rt.monotonic() - self._at > self.ttl:
            self._rows = all_sessions(self.rt, self.home, limit=20000)
            self._at = self.rt.monotonic()
        return self._rows


def stale_age(session: CliSession, now: float | None = None) -> float:
    return max(0.0, (now or time.time()) - session.updated_at)
