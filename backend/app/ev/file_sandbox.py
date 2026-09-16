"""Evie file sandbox: one jail, full DDL/DML, every origin.

Unified contract over the owner's laptop files so the brain (Muse Spark
1.3 Contributor), the Mac app, and the iPhone all run the SAME verbs
against the SAME jail and get the SAME receipts.

Verbs
  discover  -- list allowed roots + survey ~/Desktop/Documents/Downloads
  index     -- status/rebuild of the typo-tolerant file index
  search    -- ranked hits (index first, walk/Spotlight fallback inside jail)
  read/list -- bounded reads, folder listings
  write     -- create file with content (DDL)
  edit      -- replace whole file body (DML)
  append    -- append content with newline join (DML)
  mkdir     -- create folder inside jail (DDL)
  delete    -- trash-safe delete of one file (DML, reversible via undo)
  copy/move/rename -- file relocation inside jail (DML, reversible)
  run       -- execute one allowlisted script (.py/.sh/.js), no shell (DML)
  undo      -- restore the last mutating op from the trash journal
  execute   -- generic {op, args} entry point used by the API + brain runner

Safety (boring on purpose, mirrors laptop_files + MacControlService):
  - Jail is laptop_files.allowed_roots() (EV_LAPTOP_FILES_ROOT in tests).
  - Deny list is laptop_files.path_denied() (.ssh, *.pem/.key, secrets, .git).
  - No shell, no absolute escape, no folder delete, 256 KiB read/write cap.
  - Mutating ops support dry_run (validate, change nothing) and confirm
    gating: unless EV_FILE_SANDBOX_AUTONOMY=auto, write/edit/append/mkdir/
    copy/move/rename need confirm=True, delete/run always need confirm=True.
  - Every mutation is journaled to storage/sandbox/file-sandbox-journal.jsonl
    with a trash copy, so undo is real. Index is invalidated after mutation.

Origins: mac | iphone | brain | api. The origin is stamped on every receipt
so Mac-originated and iPhone-originated runs can be compared — same executor.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("ev.file_sandbox")

MAX_FILE_BYTES = 256 * 1024
MAX_LIST = 40
_JOURNAL_MAX = 200

DDL_OPS = frozenset({"write", "mkdir"})
DML_OPS = frozenset({"edit", "append", "delete", "copy", "move", "rename", "run"})
READ_OPS = frozenset({"discover", "index", "search", "read", "list", "status"})
MUTATING_OPS = DDL_OPS | DML_OPS
CONFIRM_ALWAYS = frozenset({"delete", "run"})


def autonomy() -> str:
    """confirm (default) or auto. Auto skips the confirm gate (owner's call)."""
    try:
        from app.config import settings

        raw = str(getattr(settings, "file_sandbox_autonomy", "") or "").strip().lower()
        if raw:
            return raw
    except Exception:
        pass
    return (os.getenv("EV_FILE_SANDBOX_AUTONOMY") or "confirm").strip().lower() or "confirm"


def _roots() -> list[Path]:
    from app.ev import laptop_files

    try:
        return list(laptop_files.allowed_roots())
    except Exception:
        return []


def _read_allowed() -> bool:
    from app.ev import laptop_files

    try:
        if laptop_files.laptop_files_allowed():
            return True
        return bool(laptop_files.laptop_search_allowed())
    except Exception:
        return False


def _write_allowed() -> bool:
    from app.ev import laptop_files

    try:
        return bool(laptop_files.laptop_files_allowed())
    except Exception:
        return False


def _deny(path: Path) -> str | None:
    from app.ev import laptop_files

    try:
        return laptop_files.path_denied(path)
    except Exception:
        return "path_denied"


def _trash_dir() -> Path:
    try:
        from app.config import settings

        base = Path(str(getattr(settings, "storage_root", "./storage") or "./storage"))
    except Exception:
        base = Path("./storage")
    trash = base / "sandbox" / "file-trash"
    trash.mkdir(parents=True, exist_ok=True)
    return trash


def _journal_path() -> Path:
    try:
        from app.config import settings

        base = Path(str(getattr(settings, "storage_root", "./storage") or "./storage"))
    except Exception:
        base = Path("./storage")
    base = base / "sandbox"
    base.mkdir(parents=True, exist_ok=True)
    return base / "file-sandbox-journal.jsonl"


def _journal_append(entry: dict[str, Any]) -> None:
    try:
        path = _journal_path()
        entry = dict(entry)
        entry.setdefault("at", time.time())
        lines: list[str] = []
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
        lines.append(json.dumps(entry, default=str))
        path.write_text("\n".join(lines[-_JOURNAL_MAX:]) + "\n", encoding="utf-8")
    except Exception as exc:  # journal must never break the op
        logger.debug("file_sandbox.journal_failed: %s", exc)


def _journal_pop() -> dict[str, Any] | None:
    try:
        path = _journal_path()
        if not path.exists():
            return None
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return None
        last = json.loads(lines[-1])
        path.write_text(("\n".join(lines[:-1]) + "\n") if len(lines) > 1 else "", encoding="utf-8")
        return last if isinstance(last, dict) else None
    except Exception as exc:
        logger.debug("file_sandbox.journal_pop_failed: %s", exc)
        return None


def _bump_index() -> None:
    try:
        from app.ev import file_index

        file_index.invalidate()
    except Exception:
        pass


def _file_index():
    """Shared typo-tolerant index, or None when the neighbor module is busy."""
    try:
        import importlib

        # import_module (not `from app.ev import file_index`): the from-form
        # short-circuits through the package attribute and misses a poisoned
        # sys.modules entry, so a missing/broken neighbor would look present.
        file_index = importlib.import_module("app.ev.file_index")
    except Exception:
        return None
    return file_index if file_index is not None else None

_WALK_CAP = 4000


def _walk_entries(roots: list[Path]) -> list[Path]:
    """Bounded jail walk used when the shared index is unavailable."""
    out: list[Path] = []
    for root in roots:
        try:
            stack = [root]
            while stack and len(out) < _WALK_CAP:
                current = stack.pop()
                try:
                    children = sorted(current.iterdir())
                except OSError:
                    continue
                for child in children:
                    name = child.name
                    if name.startswith(".") or name == "__pycache__":
                        continue
                    out.append(child)
                    if child.is_dir() and len(out) < _WALK_CAP:
                        stack.append(child)
                    if len(out) >= _WALK_CAP:
                        break
        except OSError:
            continue
    return out


def _walk_search(needle: str, *, kind: str = "", limit: int = 40) -> list[Path]:
    token = (needle or "").strip().lower()
    if not token:
        return []
    words = [part for part in token.replace("_", " ").replace("-", " ").split() if part]
    scored: list[tuple[int, Path]] = []
    for path in _walk_entries(_roots()):
        name = path.name.lower()
        stem = path.stem.lower()
        if kind and kind.strip().lower().lstrip(".") not in name:
            continue
        if token in name or token in str(path).lower():
            scored.append((100, path))
            continue
        hits = sum(1 for word in words if word and (word in name or word in stem))
        if hits:
            scored.append((hits, path))
    scored.sort(key=lambda item: (item[0], -len(item[1].name)), reverse=True)
    return [path for _, path in scored[: max(1, min(int(limit or 40), MAX_LIST))]]


def _receipt(
    op: str,
    *,
    ok: bool,
    origin: str,
    path: str = "",
    dry_run: bool = False,
    needs_confirm: bool = False,
    degraded: bool = False,
    error: str | None = None,
    spoken: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": ok,
        "op": op,
        "origin": origin,
        "path": path,
        "dry_run": dry_run,
        "needs_confirm": needs_confirm,
        "degraded": degraded,
        "verified": bool(ok and not dry_run and not needs_confirm),
    }
    if error:
        out["error"] = error
    if spoken:
        out["spoken"] = spoken
    if extra:
        out.update(extra)
    return out


def _needs_confirm(op: str, *, confirm: bool) -> bool:
    if confirm:
        return False
    if autonomy() == "auto":
        return False
    if op in CONFIRM_ALWAYS:
        return True
    return op in MUTATING_OPS


def discover(*, origin: str = "api") -> dict[str, Any]:
    """List the jail: allowed roots + surveyed owner folders. Read-only."""
    if not _read_allowed():
        return _receipt(
            "discover", ok=False, origin=origin, error="laptop_files_disabled",
            spoken="Local file access is not enabled on this API.",
        )
    roots = [str(r) for r in _roots()]
    survey: dict[str, Any] = {}
    try:
        from app.ev import laptop_files

        survey = laptop_files._survey_home_roots()  # noqa: SLF001 - same jail survey
        if not isinstance(survey, dict):
            survey = {}
    except Exception as exc:
        survey = {"ok": False, "error": str(exc)[:200]}
    return _receipt(
        "discover", ok=True, origin=origin,
        spoken=f"Jail covers {len(roots)} roots: {', '.join(Path(r).name for r in roots) or 'none'}.",
        extra={"roots": roots, "survey": survey},
    )


def index_status(*, rebuild: bool = False, origin: str = "api") -> dict[str, Any]:
    """Report (and optionally rebuild) the shared typo-tolerant file index."""
    if not _read_allowed():
        return _receipt(
            "index", ok=False, origin=origin, error="laptop_files_disabled",
            spoken="Local file access is not enabled on this API.",
        )
    try:
        from app.ev import laptop_files

        roots = laptop_files.allowed_roots()
    except Exception as exc:
        return _receipt("index", ok=False, origin=origin, error="index_failed", spoken=str(exc)[:300])
    module = _file_index()
    if module is not None:
        try:
            if rebuild:
                module.reset_file_index()
            idx = module._get_index(roots)  # noqa: SLF001 - introspect shared cache
            total = int((idx or {}).get("total") or 0)
            scope = str(getattr(module, "_INDEX_SCOPE", ""))
            return _receipt(
                "index", ok=True, origin=origin,
                spoken=f"Index holds {total} entries." if total else "Index is empty.",
                extra={"entries": total, "scope": scope, "roots": [str(r) for r in roots], "rebuilt": bool(rebuild), "source": "index"},
            )
        except Exception as exc:
            logger.debug("file_sandbox.index_fallback: %s", exc)
    entries = _walk_entries(roots)
    return _receipt(
        "index", ok=True, origin=origin, degraded=True,
        spoken=f"Index holds {len(entries)} entries.",
        extra={"entries": len(entries), "scope": "walk", "roots": [str(r) for r in roots], "rebuilt": bool(rebuild), "source": "walk"},
    )


def search(query: str, *, kind: str = "", limit: int = 40, origin: str = "api") -> dict[str, Any]:
    """Ranked file/folder hits from the shared index. Empty on miss."""
    needle = (query or "").strip()
    if not needle:
        return _receipt("search", ok=False, origin=origin, error="empty_query", spoken="Tell me what to search for.")
    if not _read_allowed():
        return _receipt(
            "search", ok=False, origin=origin, error="laptop_files_disabled",
            spoken="Local file access is not enabled on this API.",
        )
    module = _file_index()
    hits: list[Path] = []
    source = "walk"
    if module is not None:
        try:
            hits = list(module.scored_search(needle, kind=(kind or "").strip(), limit=max(1, min(int(limit or 40), 200))))
            source = "index"
        except Exception as exc:
            logger.debug("file_sandbox.search_fallback: %s", exc)
            hits = []
    if source == "walk":
        try:
            hits = _walk_search(needle, kind=kind, limit=limit)
        except Exception as exc:
            return _receipt("search", ok=False, origin=origin, error="search_failed", spoken=str(exc)[:300])
    paths = [str(p) for p in hits[:MAX_LIST]]
    if paths:
        head = Path(paths[0]).name
        spoken = f"I found {head}." + (f" {len(paths)} matches." if len(paths) > 1 else "")
    else:
        spoken = f"I couldn't find {needle}."
    return _receipt("search", ok=True, origin=origin, degraded=(source == "walk"), spoken=spoken, extra={"query": needle, "hits": paths, "count": len(paths), "source": source})


def _perform(action: str, args: dict[str, Any]) -> dict[str, Any]:
    from app.ev import laptop_files

    return laptop_files.perform_local({"action": action, **args})


def _backup_for_undo(path: Path) -> str | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        trash = _trash_dir()
        backup = trash / f"{int(time.time() * 1000)}-{path.name}"
        shutil.copy2(path, backup)
        return str(backup)
    except Exception:
        return None


def execute_op(
    op: str,
    args: dict[str, Any] | None = None,
    *,
    origin: str = "api",
    confirm: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one sandbox verb. Dry-run validates without mutating."""
    name = (op or "").strip().lower()
    params = dict(args or {})
    if name in {"discover"}:
        return discover(origin=origin)
    if name in {"index", "status"}:
        return index_status(rebuild=bool(params.get("rebuild")), origin=origin)
    if name == "search":
        return search(str(params.get("query") or params.get("needle") or ""), kind=str(params.get("kind") or ""), limit=int(params.get("limit") or 40), origin=origin)
    if name in {"read", "list", "open"}:
        if not _read_allowed():
            return _receipt(name, ok=False, origin=origin, error="laptop_files_disabled", spoken="Local file access is not enabled on this API.")
        result = _perform(name, params)
        ok = bool(result.get("ok"))
        return _receipt(
            name, ok=ok, origin=origin, path=str(result.get("path") or params.get("path") or ""),
            error=None if ok else str(result.get("error") or "failed"),
            spoken=result.get("spoken"), extra={k: v for k, v in result.items() if k not in {"ok", "spoken"}},
        )
    if name == "mkdir":
        return _mkdir(str(params.get("path") or ""), origin=origin, confirm=confirm, dry_run=dry_run)
    if name in MUTATING_OPS:
        if not _write_allowed():
            return _receipt(name, ok=False, origin=origin, error="laptop_files_disabled", spoken="Local file access is not enabled on this API.")
        if dry_run:
            return _dry_run(name, params, origin=origin)
        if _needs_confirm(name, confirm=confirm):
            target = str(params.get("path") or params.get("dest") or "")
            return _receipt(
                name, ok=False, origin=origin, path=target, needs_confirm=True,
                error="confirmation_required",
                spoken=f"Confirm {name} on {Path(target).name or 'that file'} and I'll do it.",
            )
        return _mutate(name, params, origin=origin)
    return _receipt(name or "unknown", ok=False, origin=origin, error="unknown_op",
                    spoken="I can discover, index, search, read, write, edit, append, mkdir, delete, copy, move, rename, run, or undo files.")


def _resolve_target(path_hint: str) -> tuple[Path | None, str | None]:
    from app.ev import laptop_files

    hint = (path_hint or "").strip()
    if not hint:
        return None, "empty_path"
    try:
        roots = laptop_files.allowed_roots()
    except Exception:
        return None, "laptop_files_disabled"
    candidate = Path(hint).expanduser()
    if not candidate.is_absolute():
        candidate = (roots[0] / candidate) if roots else (Path.home() / candidate)
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate.absolute()
    allowed = False
    for root in roots:
        try:
            if resolved == root.resolve() or resolved.is_relative_to(root.resolve()):
                allowed = True
                break
        except Exception:
            continue
    if not allowed:
        return None, "path_outside_allowed"
    denied = _deny(resolved)
    if denied:
        return None, denied
    return resolved, None


def _dry_run(op: str, params: dict[str, Any], *, origin: str) -> dict[str, Any]:
    target_hint = str(params.get("path") or params.get("dest") or "")
    if op == "write":
        content = str(params.get("content") or "")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return _receipt(op, ok=False, origin=origin, path=target_hint, dry_run=True, error="too_large", spoken="That file is larger than I will write.")
        target, error = _resolve_target(target_hint or str(params.get("query") or "evie-note.txt"))
        if error or target is None:
            return _receipt(op, ok=False, origin=origin, path=target_hint, dry_run=True, error=error or "bad_path", spoken="I won't touch that path.")
        exists = target.exists()
        return _receipt(op, ok=True, origin=origin, path=str(target), dry_run=True, needs_confirm=True,
                        spoken=f"Ready to {'overwrite' if exists else 'create'} {target.name} ({len(content.encode('utf-8'))} bytes).",
                        extra={"would": "overwrite" if exists else "create", "bytes": len(content.encode("utf-8"))})
    if op == "mkdir":
        target, error = _resolve_target(target_hint)
        if error or target is None:
            return _receipt(op, ok=False, origin=origin, path=target_hint, dry_run=True, error=error or "bad_path", spoken="I won't touch that path.")
        return _receipt(op, ok=True, origin=origin, path=str(target), dry_run=True, needs_confirm=True,
                        spoken=f"Ready to create folder {target.name}.", extra={"would": "mkdir", "exists": target.exists()})
    if op in {"edit", "append", "read", "delete", "run", "copy", "move", "rename"}:
        from app.ev import laptop_files

        target, matches, error = laptop_files.resolve_existing(target_hint, str(params.get("query") or ""))
        if error == "ambiguous":
            names = ", ".join(item.name for item in matches[:8])
            return _receipt(op, ok=False, origin=origin, path=target_hint, dry_run=True, error="ambiguous", spoken=f"Which file do you mean? I found {names}.")
        if target is None:
            return _receipt(op, ok=False, origin=origin, path=target_hint, dry_run=True, error=error or "not_found", spoken="I couldn't find that file.")
        if op in {"copy", "move", "rename"} and not str(params.get("dest") or "").strip():
            return _receipt(op, ok=False, origin=origin, path=str(target), dry_run=True, error="missing_dest", spoken="Tell me the destination name.")
        if op == "run" and target.suffix.lower() not in {".py", ".sh", ".js"}:
            return _receipt(op, ok=False, origin=origin, path=str(target), dry_run=True, error="not_runnable", spoken=f"{target.name} isn't a script I will run.")
        return _receipt(op, ok=True, origin=origin, path=str(target), dry_run=True, needs_confirm=True,
                        spoken=f"Ready to {op} {target.name}.", extra={"would": op})
    return _receipt(op, ok=False, origin=origin, path=target_hint, dry_run=True, error="unknown_op", spoken="Unknown file operation.")


def _mkdir(path_hint: str, *, origin: str, confirm: bool, dry_run: bool) -> dict[str, Any]:
    if not _write_allowed():
        return _receipt("mkdir", ok=False, origin=origin, error="laptop_files_disabled", spoken="Local file access is not enabled on this API.")
    if dry_run:
        return _dry_run("mkdir", {"path": path_hint}, origin=origin)
    if _needs_confirm("mkdir", confirm=confirm):
        return _receipt("mkdir", ok=False, origin=origin, path=path_hint, needs_confirm=True, error="confirmation_required",
                        spoken=f"Confirm creating folder {Path(path_hint).name or 'that folder'} and I'll do it.")
    target, error = _resolve_target(path_hint)
    if error or target is None:
        return _receipt("mkdir", ok=False, origin=origin, path=path_hint, error=error or "bad_path", spoken="I won't touch that path.")
    try:
        target.mkdir(parents=True, exist_ok=True)
        _bump_index()
        return _receipt("mkdir", ok=True, origin=origin, path=str(target), spoken=f"Created folder {target.name}.", extra={"created": True})
    except Exception as exc:
        return _receipt("mkdir", ok=False, origin=origin, path=str(target), error="mkdir_failed", spoken=str(exc)[:300])


def _mutate(op: str, params: dict[str, Any], *, origin: str) -> dict[str, Any]:
    from app.ev import laptop_files

    # Snapshot for undo before the mutation.
    hint = str(params.get("path") or "")
    undo_backup: str | None = None
    undo_path: str | None = None
    undo_kind = op
    try:
        existing, _, _ = laptop_files.resolve_existing(hint, str(params.get("query") or ""))
        if existing is not None and existing.is_file() and op in {"write", "edit", "append", "delete", "move", "rename"}:
            undo_backup = _backup_for_undo(existing)
            undo_path = str(existing)
        elif existing is not None and op == "write" and not existing.exists():
            undo_kind = "write_new"
            undo_path = str(existing)
    except Exception:
        pass

    result = _perform(op, params)
    ok = bool(result.get("ok"))
    if ok:
        _bump_index()
        journal_path = str(result.get("path") or undo_path or hint)
        _journal_append({"op": undo_kind, "path": journal_path, "backup": undo_backup, "dest": str(params.get("dest") or ""), "origin": origin})
    return _receipt(
        op, ok=ok, origin=origin, path=str(result.get("path") or undo_path or hint),
        error=None if ok else str(result.get("error") or "failed"),
        spoken=result.get("spoken"), extra={k: v for k, v in result.items() if k not in {"ok", "spoken"}},
    )


def undo(*, origin: str = "api") -> dict[str, Any]:
    """Restore the last mutating op from its trash copy. Honest when empty."""
    if not _write_allowed():
        return _receipt("undo", ok=False, origin=origin, error="laptop_files_disabled", spoken="Local file access is not enabled on this API.")
    entry = _journal_pop()
    if not entry:
        return _receipt("undo", ok=False, origin=origin, error="nothing_to_undo", spoken="There's nothing to undo.")
    op = str(entry.get("op") or "")
    path = Path(str(entry.get("path") or ""))
    backup = str(entry.get("backup") or "")
    denied = _deny(path)
    if denied:
        return _receipt("undo", ok=False, origin=origin, path=str(path), error=denied, spoken="I won't touch that path.")
    try:
        if op == "write_new":
            if path.exists() and path.is_file():
                path.unlink()
            _bump_index()
            return _receipt("undo", ok=True, origin=origin, path=str(path), spoken=f"Undid creating {path.name}.", extra={"restored": "deleted_new"})
        if backup and Path(backup).exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, path)
            _bump_index()
            return _receipt("undo", ok=True, origin=origin, path=str(path), spoken=f"Restored {path.name}.", extra={"restored": True})
        # Move/rename without backup: best effort — dest hint holds the moved file.
        dest_hint = str(entry.get("dest") or "")
        if op in {"move", "rename", "copy"} and dest_hint:
            moved = Path(dest_hint)
            if moved.exists() and moved.is_file() and op in {"move", "rename"}:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(moved), str(path))
                _bump_index()
                return _receipt("undo", ok=True, origin=origin, path=str(path), spoken=f"Moved {path.name} back.", extra={"restored": True})
            if moved.exists() and op == "copy":
                moved.unlink()
                return _receipt("undo", ok=True, origin=origin, path=str(moved), spoken=f"Removed the copy {moved.name}.", extra={"restored": "removed_copy"})
        return _receipt("undo", ok=False, origin=origin, path=str(path), error="backup_missing", spoken="The backup for that change is gone.")
    except Exception as exc:
        return _receipt("undo", ok=False, origin=origin, path=str(path), error="undo_failed", spoken=str(exc)[:300])
