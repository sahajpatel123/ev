"""AST symbol index for repo-scale coding: outline a file, find a symbol.

Python files parse with :mod:`ast` (precise); other languages fall back to
conservative definition regexes. The index caches per file with mtime/size
validation, so repeat queries over a big tree stay cheap. Read-only: all
paths resolve inside the coding workspace jail.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from app.ev.code_runtime import (
    MAX_SEARCH_FILES,
    SECRET_NAME_RE,
    SKIP_DIR_NAMES,
    CodeJailError,
    resolve_workspace_path,
    workspace_root,
)

MAX_INDEX_FILES = 2000
MAX_SYMBOLS_PER_FILE = 500
MAX_FIND_HITS = 40

_CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".swift",
        ".go",
        ".rs",
        ".rb",
        ".java",
        ".php",
        ".pl",
        ".lua",
        ".kt",
        ".scala",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".sh",
        ".sql",
        ".r",
        ".m",
        ".mm",
        ".vue",
        ".svelte",
    }
)

_FALLBACK_DEF_RES = (
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?fn\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|enum|struct)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*def\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("),
)

_INDEX_CACHE: dict[str, dict[str, Any]] = {}


def invalidate_code_map(root: Path | None = None) -> None:
    """Drop cached symbol indexes (bulk edit, project switch, tests)."""

    if root is None:
        _INDEX_CACHE.clear()
        return
    try:
        _INDEX_CACHE.pop(str(root.expanduser().resolve()), None)
    except OSError:
        _INDEX_CACHE.clear()


def _iter_code_files(root: Path) -> list[Path]:
    import os

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES and not name.startswith(".")
        ]
        current = Path(dirpath)
        for filename in filenames:
            if filename.startswith("."):
                continue
            if Path(filename).suffix.lower() not in _CODE_SUFFIXES:
                continue
            if SECRET_NAME_RE.search(filename):
                continue
            path = current / filename
            try:
                probe = path.read_bytes()[:1024]
            except OSError:
                continue
            if b"\0" in probe:
                continue
            found.append(path)
            if len(found) >= min(MAX_INDEX_FILES, MAX_SEARCH_FILES):
                return found
    return found


def _python_symbols(text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    symbols: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({"name": node.name, "kind": "function", "line": node.lineno})
        elif isinstance(node, ast.ClassDef):
            symbols.append({"name": node.name, "kind": "class", "line": node.lineno})
        if len(symbols) >= MAX_SYMBOLS_PER_FILE:
            break
    symbols.sort(key=lambda item: item["line"])
    return symbols


def _fallback_symbols(text: str) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern in _FALLBACK_DEF_RES:
            match = pattern.search(line)
            if match:
                symbols.append({"name": match.group(1), "kind": "def", "line": lineno})
                break
        if len(symbols) >= MAX_SYMBOLS_PER_FILE:
            break
    return symbols


def _index_file(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
        if stat.st_size > 256_000:
            return {"size": stat.st_size, "skipped": "too_large", "symbols": []}
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    symbols = _python_symbols(text) if path.suffix.lower() == ".py" else _fallback_symbols(text)
    return {"mtime": stat.st_mtime, "size": stat.st_size, "symbols": symbols}


def refresh_index(root: Path | None = None) -> dict[str, Any]:
    """Rebuild/refresh the cached index for one project root."""

    base = (root or workspace_root()).expanduser().resolve()
    key = str(base)
    cached = _INDEX_CACHE.get(key) or {"files": {}, "root": key}
    files: dict[str, Any] = dict(cached.get("files") or {})
    seen: set[str] = set()
    for path in _iter_code_files(base):
        rel = str(path.relative_to(base))
        seen.add(rel)
        try:
            stat = path.stat()
        except OSError:
            files.pop(rel, None)
            continue
        entry = files.get(rel)
        if (
            isinstance(entry, dict)
            and entry.get("mtime") == stat.st_mtime
            and entry.get("size") == stat.st_size
        ):
            continue
        indexed = _index_file(path)
        if indexed is None:
            files.pop(rel, None)
        else:
            files[rel] = indexed
    for rel in [rel for rel in files if rel not in seen]:
        files.pop(rel, None)
    cached = {"root": key, "files": files}
    _INDEX_CACHE[key] = cached
    return cached


def file_outline(rel: str) -> dict[str, Any]:
    """Symbols defined in one project-relative file."""

    target = resolve_workspace_path(rel or "", create=False)
    if not target.is_file():
        raise CodeJailError("not a file inside the coding workspace")
    if SECRET_NAME_RE.search(target.name):
        raise CodeJailError("refusing to index a secret-looking filename")
    base = workspace_root()
    index = refresh_index(base)
    entry = (index.get("files") or {}).get(str(target.relative_to(base)))
    symbols = list((entry or {}).get("symbols") or []) if entry else []
    return {
        "ok": True,
        "path": str(target.relative_to(base)),
        "symbols": symbols[:MAX_SYMBOLS_PER_FILE],
        "count": len(symbols),
    }


def find_symbol(name: str) -> dict[str, Any]:
    """Project-wide definition hits for one symbol name (exact match)."""

    needle = (name or "").strip()
    if not needle or len(needle) > 128:
        raise CodeJailError("symbol name must be 1-128 characters")
    base = workspace_root()
    index = refresh_index(base)
    hits: list[dict[str, Any]] = []
    for rel, entry in (index.get("files") or {}).items():
        if not isinstance(entry, dict):
            continue
        for symbol in entry.get("symbols") or []:
            if str(symbol.get("name") or "") == needle:
                hits.append(
                    {
                        "path": rel,
                        "kind": symbol.get("kind"),
                        "line": symbol.get("line"),
                    }
                )
                if len(hits) >= MAX_FIND_HITS:
                    return {"ok": True, "symbol": needle, "hits": hits, "truncated": True}
    return {"ok": True, "symbol": needle, "hits": hits, "truncated": False}
