"""Unified file/folder index: fast, typo-tolerant retrieval for every file channel.

Live voice stalled on "locating the folder" because every retrieval request
did its own blocking work: ``os.walk`` over $HOME (9 roots including $HOME
itself), plus up to 6 sequential ``mdfind`` calls at 8s timeout each in
``file_retrieve.collect_retrieve_hits``. Multi-word queries also missed:
``laptop_files`` matched the whole needle as one substring, so
"quarterly report" never matched "Quarterly_Report_Final.pdf" in Spotlight
(phrase with space vs underscore) and truncated walk order decided hits.

This module is the shared fix. One incremental in-memory index per scope:

- single scan reused across tokens/channels (no per-token walk/subprocess),
- inverted token postings with IDF-weighted field scoring (name > parent >
  path), prefix + edit-distance + trigram-Dice fuzzy expansion,
  exact-phrase bonus,
- recency / location / depth / noise adjustments for sorting,
- folders indexed alongside files (``want_folder`` filters at query time),
- incremental refresh with subtree-max pruning: steady-state cost is one
  scandir per changed dir chain, not a full walk, so newly created files are
  visible on the very next query.

Stdlib only, offline, deterministic. Callers keep their slow path as a
fallback when the index returns nothing (content search, unindexed edge).
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

logger = logging.getLogger("ev.file_index")

_TTL_S = 30.0
_MAX_ENTRIES = 20000
_MAX_CANDIDATES = 3000
_MAX_VOCAB_EXPANSION = 24

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_QUERY_STOP = frozenset(
    {
        "a", "an", "the", "my", "me", "mine", "our",
        "please", "just", "some", "any", "that", "this", "those", "these",
        "one", "ones", "it", "of", "for", "on", "in", "from", "to",
        "and", "or", "called", "named", "titled", "copy", "copies",
        "thing", "things", "stuff", "here", "there", "now", "also", "too",
        "laptop", "mac", "macbook", "computer", "machine", "home", "local",
        "locally", "find", "search", "locate", "look", "show", "open",
        "read", "get", "give", "file", "files", "folder", "folders",
        "document", "documents", "where", "is",
    }
)
_NOISE_TOKENS = ("flagship", "draft", "old", "copy", "backup", "template", "sample", "placeholder")
_INDEXES: dict[str, dict[str, Any]] = {}
_INDEX_SCOPE = ""
_INDEX_AT = 0.0
_INDEX_CACHE_CAP = 4


def _trigrams(value: str) -> set[str]:
    token = f"  {value} "
    if len(token) < 5:
        return set()
    return {token[index : index + 3] for index in range(len(token) - 2)}


def _dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return (2.0 * len(left & right)) / (len(left) + len(right))


def reset_file_index() -> None:
    """Drop the cached index. Tests call this after changing roots."""

    global _INDEXES, _INDEX_SCOPE, _INDEX_AT
    _INDEXES = {}
    _INDEX_SCOPE = ""
    _INDEX_AT = 0.0


def invalidate() -> None:
    """Mark the index stale after a local write/rename/delete."""

    reset_file_index()


def query_tokens(query: str, *, extra_stop: frozenset[str] = frozenset()) -> list[str]:
    """Split owner phrasing into searchable tokens (deduped, capped)."""

    raw = _normalize(query or "")
    out: list[str] = []
    for part in _TOKEN_RE.findall(raw):
        if len(part) < 2 or part in _QUERY_STOP or part in extra_stop:
            continue
        if part not in out:
            out.append(part)
        stemmed = _stem(part)
        if stemmed != part and len(stemmed) >= 2 and stemmed not in _QUERY_STOP and stemmed not in out:
            out.append(stemmed)
        if len(out) >= 10:
            break
    return out[:10]


def scored_search(
    query: str,
    *,
    roots: list[Path] | None = None,
    want_folder: bool = False,
    kind: str = "",
    limit: int = 80,
    cls_tokens: tuple[str, ...] = (),
) -> list[Path]:
    """Ranked file/folder hits from the shared index. Empty on miss."""

    tokens = query_tokens(query)
    for extra in cls_tokens:
        for part in _TOKEN_RE.findall(_normalize(extra)):
            if len(part) >= 2 and part not in tokens:
                tokens.append(part)
    tokens = tokens[:10]
    if not tokens:
        return []
    index = _get_index(roots)
    if not index or not index.get("entries"):
        return []
    entries: list[dict[str, Any]] = index["entries"]
    postings: dict[str, list[int]] = index["postings"]
    doc_freq: dict[str, int] = index["doc_freq"]
    total = max(1, int(index.get("total") or len(entries)))

    candidates = _candidates(tokens, postings, doc_freq)
    if not candidates:
        return []
    raw_norm = _normalize(query)
    now = time.time()
    weights = {token: _idf(token, total, doc_freq) for token in tokens}
    tris = {token: _trigrams(token) for token in tokens if len(token) >= 5}
    scored: list[tuple[float, int]] = []
    for idx in candidates:
        entry = entries[idx]
        if want_folder and not entry["is_dir"]:
            continue
        # Deny filtering happens at scan time (_cheap_denied mirrors the full
        # policy); per-hit resolve() here cost a syscall per candidate.
        score = _score_entry(entry, tokens, raw_norm, total, doc_freq, now=now, weights=weights, tris=tris)
        if score <= 0:
            continue
        if kind and not _kind_ok(entry, kind):
            continue
        scored.append((score, idx))
    scored.sort(key=lambda item: (item[0], -len(entries[item[1]]["name"])), reverse=True)
    out: list[Path] = []
    for _, idx in scored[: max(1, min(int(limit or 80), 400))]:
        try:
            out.append(Path(entries[idx]["path"]))
        except OSError:
            continue
    return out


def rank_score_for_path(path: Path, query: str) -> float:
    """Score one path without the index (fallback hits, unit checks)."""

    tokens = query_tokens(query)
    if not tokens:
        return 0.0
    try:
        mtime = float(path.stat().st_mtime)
    except OSError:
        mtime = 0.0
    entry = _entry_from_path(path, mtime)
    return _score_entry(entry, tokens, _normalize(query), 1000, {})


def _get_index(roots: list[Path] | None) -> dict[str, Any] | None:
    global _INDEX_SCOPE, _INDEX_AT
    scope = _scope_key(roots)
    now = time.time()
    slot = _INDEXES.get(scope)
    if slot is not None and (now - float(slot.get("at") or 0.0)) < _TTL_S:
        index = slot["index"]
        try:
            _refresh_index(index, roots)
        except OSError:
            pass
        except Exception:
            logger.debug("file_index.refresh_failed", exc_info=True)
        slot["at"] = now
        _INDEX_SCOPE = scope
        _INDEX_AT = now
        return index
    try:
        built = _build_index(roots, scope)
    except OSError:
        if slot is not None:
            return slot["index"]
        return None
    while len(_INDEXES) >= _INDEX_CACHE_CAP:
        oldest = min(_INDEXES, key=lambda key: float(_INDEXES[key].get("at") or 0.0))
        _INDEXES.pop(oldest, None)
    _INDEXES[scope] = {"index": built, "at": now}
    _INDEX_SCOPE = scope
    _INDEX_AT = now
    return built


def _refresh_index(index: dict[str, Any], roots: list[Path] | None) -> None:
    """Incremental update with subtree-max pruning.

    Each tracked dir stores ``sub`` = max mtime of itself + all tracked
    descendants. A file add/remove/rename always bumps its direct parent dir
    mtime, so a subtree with ``sub <= built_at`` cannot have changed and is
    pruned without listing. Steady-state cost is one scandir per changed dir
    chain instead of a full walk. Content-only edits don't affect recall.
    """

    from app.ev.laptop_files import _search_depth

    if roots is None:
        from app.ev.laptop_files import allowed_roots as _allowed

        roots = _allowed()
    built_at = float(index.get("built_at") or 0.0)
    entries: list[dict[str, Any]] = index.get("entries") or []
    dirs: dict[str, dict[str, Any]] = index.get("dirs") or {}
    index["dirs"] = dirs
    by_path: dict[str, int] = {entry["path"]: pos for pos, entry in enumerate(entries)}
    changed = False
    visited: list[str] = []
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        rkey = str(resolved)
        if not resolved.is_dir():
            if _purge_prefix(entries, by_path, dirs, rkey):
                changed = True
            continue
        max_depth = _search_depth(resolved)
        stack: list[tuple[str, int]] = [(rkey, 0)]
        while stack:
            dkey, depth = stack.pop()
            stored = dirs.get(dkey)
            if stored is not None and dkey != rkey and float(stored.get("sub") or 0.0) <= built_at:
                continue
            try:
                names, subdirs = _list_dir(dkey)
            except OSError:
                if _purge_prefix(entries, by_path, dirs, dkey):
                    changed = True
                continue
            if (stored is None or set(names) != set(stored.get("children") or ())) and _reconcile_dir(
                entries, by_path, dirs, dkey, names, set(subdirs), depth, max_depth
            ):
                changed = True
            with contextlib.suppress(OSError):
                dirs.setdefault(dkey, {}).update(_dir_meta(dkey, names, tuple(subdirs)))
            visited.append(dkey)
            if depth + 1 < max_depth:
                for kid_path in subdirs:
                    stack.append((kid_path, depth + 1))
    for dkey in sorted(visited, key=lambda p: p.count(os.sep), reverse=True):
        meta = dirs.get(dkey)
        if not meta:
            continue
        sub = float(meta.get("mtime") or 0.0)
        for child in meta.get("subdirs") or ():
            kid = dirs.get(child)
            if kid:
                sub = max(sub, float(kid.get("sub") or 0.0))
        meta["sub"] = sub
    if changed:
        _rebuild_postings(index)
    index["built_at"] = time.time()
    index["total"] = len(entries)


def _dir_meta(dkey: str, names: tuple[str, ...], subdirs: tuple[str, ...]) -> dict[str, Any]:
    try:
        mtime = Path(dkey).stat().st_mtime
    except OSError:
        mtime = 0.0
    return {"mtime": float(mtime), "children": names, "subdirs": subdirs, "sub": float(mtime)}


def _list_dir(dkey: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from app.ev.laptop_files import _SKIP_DIR_NAMES

    names: list[str] = []
    subdirs: list[str] = []
    with os.scandir(dkey) as handle:
        for child in handle:
            name = child.name
            if not name or name.startswith(".") or name in _SKIP_DIR_NAMES:
                continue
            try:
                if child.is_symlink():
                    continue
            except OSError:
                continue
            full = str(Path(dkey) / name)
            if _cheap_denied(Path(full)):
                continue
            names.append(name)
            try:
                if child.is_dir(follow_symlinks=False):
                    subdirs.append(full)
            except OSError:
                continue
    names.sort()
    subdirs.sort()
    return tuple(names), tuple(subdirs)


def _reconcile_dir(
    entries: list[dict[str, Any]],
    by_path: dict[str, int],
    dirs: dict[str, dict[str, Any]],
    dkey: str,
    names: tuple[str, ...],
    sub_set: set[str],
    depth: int,
    max_depth: int,
) -> bool:
    stored = dirs.get(dkey) or {}
    previous = set(stored.get("children") or ())
    live = set(names)
    if previous == live and stored:
        return False
    changed = False
    for name in sorted(previous - live):
        full = str(Path(dkey) / name)
        pos = by_path.get(full)
        if pos is not None:
            _drop_entry(entries, by_path, pos)
            changed = True
        if full in dirs and _purge_prefix(entries, by_path, dirs, full):
            changed = True
    for name in sorted(live - previous):
        if len(entries) >= _MAX_ENTRIES:
            break
        full = str(Path(dkey) / name)
        child_depth = depth + 1
        if full in sub_set:
            try:
                mtime = Path(full).stat().st_mtime
            except OSError:
                continue
            parent = Path(full).parent.name
            entries.append(_make_entry(full, name, "", parent, full, True, float(mtime), child_depth))
            by_path[full] = len(entries) - 1
            changed = True
            if child_depth >= max_depth:
                try:
                    kid_names, kid_subs = _list_dir(full)
                except OSError:
                    kid_names, kid_subs = (), ()
                dirs[full] = _dir_meta(full, kid_names, kid_subs)
        else:
            try:
                candidate = Path(full)
                mtime = candidate.stat().st_mtime
                if candidate.is_dir():
                    continue
            except OSError:
                continue
            parent = Path(dkey).name
            entries.append(
                _make_entry(full, name, Path(name).stem, parent, full, False, float(mtime), child_depth)
            )
            by_path[full] = len(entries) - 1
            changed = True
    return changed


def _drop_entry(entries: list[dict[str, Any]], by_path: dict[str, int], pos: int) -> None:
    last = len(entries) - 1
    if pos != last:
        entries[pos] = entries[last]
        by_path[entries[pos]["path"]] = pos
    by_path.pop(entries[last]["path"], None)
    entries.pop()


def _purge_prefix(
    entries: list[dict[str, Any]],
    by_path: dict[str, int],
    dirs: dict[str, dict[str, Any]],
    prefix: str,
) -> bool:
    changed = False
    for key in [key for key in by_path if key == prefix or key.startswith(prefix + os.sep)]:
        pos = by_path.get(key)
        if pos is not None and 0 <= pos < len(entries) and entries[pos]["path"] == key:
            _drop_entry(entries, by_path, pos)
            changed = True
        else:
            by_path.pop(key, None)
            changed = True
    for key in [key for key in dirs if key == prefix or key.startswith(prefix + os.sep)]:
        dirs.pop(key, None)
        changed = True
    return changed


def _rebuild_postings(index: dict[str, Any]) -> None:
    entries: list[dict[str, Any]] = index.get("entries") or []
    postings: dict[str, list[int]] = {}
    for idx, entry in enumerate(entries):
        for token in entry["tokens"]:
            bucket = postings.setdefault(token, [])
            if not bucket or bucket[-1] != idx:
                bucket.append(idx)
    index["postings"] = postings
    index["doc_freq"] = {token: len(bucket) for token, bucket in postings.items()}
    index["total"] = len(entries)


def _scope_key(roots: list[Path] | None) -> str:
    """One canonical key per root set, however the caller spelled it."""

    try:
        from app.config import settings as _settings

        env = str(getattr(_settings, "environment", "") or "").strip().lower()
    except Exception:
        env = ""
    if roots is None:
        try:
            from app.ev.laptop_files import allowed_roots as _allowed

            roots = _allowed()
        except Exception:
            return f"auto:{env}"
    try:
        parts = sorted(str(Path(str(item)).expanduser()) for item in roots)
    except OSError:
        return f"roots:?|{env}"
    return "roots:" + "|".join(parts) + f"|{env}"


def _build_index(roots: list[Path] | None, scope: str) -> dict[str, Any]:
    if roots is None:
        from app.ev.laptop_files import allowed_roots as _allowed

        roots = _allowed()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    dirs: dict[str, dict[str, Any]] = {}
    for root in roots:
        sub_entries, sub_dirs = _scan_root(root)
        for entry in sub_entries:
            key = entry["path"]
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
            if len(entries) >= _MAX_ENTRIES:
                break
        for key, meta in sub_dirs.items():
            if key not in dirs:
                dirs[key] = meta
        if len(entries) >= _MAX_ENTRIES:
            break
    for dkey in sorted(dirs, key=lambda p: p.count(os.sep), reverse=True):
        meta = dirs[dkey]
        sub = float(meta.get("mtime") or 0.0)
        for child in meta.get("subdirs") or ():
            kid = dirs.get(child)
            if kid:
                sub = max(sub, float(kid.get("sub") or 0.0))
        meta["sub"] = sub
    postings: dict[str, list[int]] = {}
    doc_freq: dict[str, int] = {}
    for idx, entry in enumerate(entries):
        for token in entry["tokens"]:
            bucket = postings.setdefault(token, [])
            if not bucket or bucket[-1] != idx:
                bucket.append(idx)
    for token, bucket in postings.items():
        doc_freq[token] = len(bucket)
    logger.debug("file_index.built scope=%s entries=%d", scope[:80], len(entries))
    return {
        "entries": entries,
        "postings": postings,
        "doc_freq": doc_freq,
        "total": len(entries),
        "built_at": time.time(),
        "dirs": dirs,
    }


def _scan_root(root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    from app.ev.laptop_files import _SKIP_DIR_NAMES, _search_depth

    out: list[dict[str, Any]] = []
    found_dirs: dict[str, dict[str, Any]] = {}
    try:
        resolved = root.expanduser().resolve()
    except OSError:
        return out, found_dirs
    if not resolved.is_dir():
        return out, found_dirs
    max_depth = _search_depth(resolved)
    prefix_len = len(resolved.parts)
    try:
        for dirpath, dirnames, filenames in os.walk(resolved):
            current = Path(dirpath)
            try:
                depth = len(current.parts) - prefix_len
            except Exception:
                continue
            dirnames[:] = [n for n in dirnames if n not in _SKIP_DIR_NAMES and not n.startswith(".")]
            kept_dirs = [name for name in dirnames if not (current / name).is_symlink()]
            kept_files = [
                name
                for name in filenames
                if name
                and not name.startswith(".")
                and name not in _SKIP_DIR_NAMES
                and not (current / name).is_symlink()
            ]
            dkey = str(current)
            try:
                dir_mtime = current.stat().st_mtime
            except OSError:
                dir_mtime = 0.0
            subs = tuple(sorted(str(current / name) for name in kept_dirs))
            kids = tuple(sorted([*kept_dirs, *kept_files]))
            if dkey not in found_dirs:
                found_dirs[dkey] = {
                    "mtime": float(dir_mtime),
                    "children": kids,
                    "subdirs": subs,
                    "sub": float(dir_mtime),
                }
            if depth >= max_depth:
                dirnames.clear()
            if (
                depth > 0
                and current.name
                and not current.name.startswith(".")
                and not _cheap_denied(current)
            ):
                out.append(
                    _make_entry(
                        dkey, current.name, "", current.parent.name, dkey, True, float(dir_mtime), depth
                    )
                )
            if depth >= max_depth:
                continue
            for name in kept_files:
                child = current / name
                if _cheap_denied(child):
                    continue
                try:
                    mtime = child.stat().st_mtime
                except OSError:
                    continue
                out.append(
                    _make_entry(
                        str(child),
                        name,
                        Path(name).stem,
                        current.name,
                        str(child),
                        False,
                        float(mtime),
                        depth + 1,
                    )
                )
                if len(out) >= _MAX_ENTRIES:
                    return out, found_dirs
    except OSError:
        return out, found_dirs
    return out, found_dirs


def _make_entry(
    path: str,
    name: str,
    stem: str,
    parent: str,
    full: str,
    is_dir: bool,
    mtime: float,
    depth: int,
) -> dict[str, Any]:
    name_l = name.lower()
    stem_l = (stem or Path(name).stem).lower()
    parent_l = (parent or "").lower()
    toks: set[str] = set()
    for blob in (name_l, stem_l, parent_l):
        for part in _TOKEN_RE.findall(_normalize(blob)):
            if len(part) >= 2:
                toks.add(part)
                stemmed = _stem(part)
                if len(stemmed) >= 2:
                    toks.add(stemmed)
    # Extension without dot so "pdf" queries hit PDFs.
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix and len(suffix) >= 2:
        toks.add(suffix)
    return {
        "path": path,
        "name": name_l,
        "stem": stem_l,
        "parent": parent_l,
        "full": full.lower(),
        "suffix": suffix,
        "is_dir": is_dir,
        "mtime": float(mtime or 0.0),
        "depth": int(depth),
        "tokens": sorted(toks),
        "words": tuple(_TOKEN_RE.findall(_normalize(name_l))),
        "norm": _normalize(name_l),
        "compact": re.sub(r"[\s_\-.]+", "", name_l),
    }


def _entry_from_path(path: Path, mtime: float) -> dict[str, Any]:
    name = path.name
    try:
        is_dir = path.is_dir()
    except OSError:
        is_dir = False
    return _make_entry(str(path), name, Path(name).stem, path.parent.name, str(path), is_dir, mtime, 3)


def _candidates(
    tokens: list[str], postings: dict[str, list[int]], doc_freq: dict[str, int]
) -> set[int]:
    out: set[int] = set()

    vocab_sorted = sorted(postings)
    missed: list[str] = []
    for token in tokens:
        before = len(out)
        bucket = postings.get(token)
        if bucket:
            out.update(bucket)
        stemmed = _stem(token)
        if stemmed != token and stemmed in postings:
            out.update(postings[stemmed])
        if len(token) >= 3:
            for word in _prefixed_words(vocab_sorted, token):
                out.update(postings[word])
                if len(out) >= _MAX_CANDIDATES:
                    break
        if len(out) == before:
            missed.append(token)
        if len(out) >= _MAX_CANDIDATES:
            break
    # Fuzzy expansion only for tokens with zero cheap hits: typo queries stay
    # recall-complete without taxing every exact query with a vocab scan.
    for token in missed:
        if len(token) >= 4:
            matched = 0
            for word in vocab_sorted:
                if word == token or not word.startswith(token[:2]):
                    continue
                if _edits_at_most_one(token, word) or (
                    len(token) >= 7 and _edits_at_most_two(token, word)
                ):
                    out.update(postings[word])
                    matched += 1
                    if matched >= _MAX_VOCAB_EXPANSION or len(out) >= _MAX_CANDIDATES:
                        break
        if len(token) >= 5:
            try:
                query_tri = _trigrams(token)
                if query_tri:
                    picked = 0
                    for word in vocab_sorted:
                        if len(word) < 5 or abs(len(word) - len(token)) > 3:
                            continue
                        if word.startswith(token[:2]) and _dice(query_tri, _trigrams(word)) >= 0.6:
                            out.update(postings[word])
                            picked += 1
                            if picked >= _MAX_VOCAB_EXPANSION or len(out) >= _MAX_CANDIDATES:
                                break
            except Exception:
                pass
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


def _prefixed_words(vocab_sorted: list[str], token: str) -> list[str]:
    import bisect

    found: list[str] = []
    start = bisect.bisect_left(vocab_sorted, token)
    for word in vocab_sorted[start:]:
        if not word.startswith(token):
            break
        if word != token:
            found.append(word)
            if len(found) >= _MAX_VOCAB_EXPANSION:
                break
    return found


def _score_entry(
    entry: dict[str, Any],
    tokens: list[str],
    raw_norm: str,
    total: int,
    doc_freq: dict[str, int],
    *,
    now: float | None = None,
    weights: dict[str, float] | None = None,
    tris: dict[str, set[str]] | None = None,
) -> float:
    name = entry["name"]
    stem = entry["stem"]
    parent = entry["parent"]
    full = entry["full"]
    name_words = entry.get("words") or ()
    compact_name = entry.get("compact") or ""
    norm_name = entry.get("norm") or name
    if weights is None:
        weights = {token: _idf(token, total, doc_freq) for token in tokens}
    if tris is None:
        tris = {token: _trigrams(token) for token in tokens if len(token) >= 5}
    score = 0.0
    matched_all = True
    for token in tokens:
        tier = _token_tier(token, entry, name_words, compact_name, tris.get(token))
        if tier <= 0:
            matched_all = False
            continue
        weight = weights.get(token, 1.0)
        if tier <= 30:
            weight *= 0.6  # fuzzy matches are uncertain: discount, don't boost
        score += tier * weight
    if not score:
        # Parent/path-only recall (e.g. Aadhaar.pdf inside IDs for "government id"):
        # at least one token matched weakly handled above; nothing matched -> miss.
        return 0.0
    if matched_all and len(tokens) > 1:
        score += 25.0
    if raw_norm and raw_norm in norm_name:
        score += 30.0
    # Location boosts mirror the existing file ranking so Desktop wins ties.
    if parent == "desktop":
        score += 18.0
    elif parent in {"documents", "docs"}:
        score += 10.0
    elif parent == "downloads":
        score += 2.0
    elif "icloud" in parent or "icloud" in full:
        score += 5.0
    # Recency: recently touched owner files usually win ties.
    if now is None:
        now = time.time()
    age = now - float(entry.get("mtime") or 0.0)
    if age >= 0:
        if age < 7 * 86400:
            score += 8.0
        elif age < 30 * 86400:
            score += 5.0
        elif age < 90 * 86400:
            score += 2.0
    score -= min(len(entry.get("stem") or ""), 10)
    score -= min(int(entry.get("depth") or 0), 6)
    lowered = f"{name} {stem}"
    for noise in _NOISE_TOKENS:
        if noise in lowered and noise not in tokens:
            score -= 12.0 if noise == "flagship" else 6.0
    if entry.get("is_dir"):
        score -= 2.0  # files win ties unless a folder was requested
    return score


def _token_tier(
    token: str,
    entry: dict[str, Any],
    name_words: tuple[str, ...] | list[str],
    compact_name: str,
    query_tri: set[str] | None,
) -> float:
    name = entry["name"]
    stem = entry["stem"]
    parent = entry["parent"]
    full = entry["full"]
    compact = re.sub(r"[\s_\-.]+", "", token)
    if stem == token or name == token:
        return 100.0
    if token in name_words:
        return 80.0
    if name.startswith(token):
        return 70.0
    if token in name or token in stem:
        return 50.0
    if compact and compact in compact_name:
        return 45.0
    if len(token) >= 3 and any(word.startswith(token) for word in name_words):
        return 40.0
    if len(token) >= 4:
        for word in name_words:
            if _edits_at_most_one(token, word):
                return 30.0
        if len(token) >= 7:
            for word in name_words:
                if _edits_at_most_two(token, word):
                    return 20.0
    if len(token) >= 5 and query_tri:
        try:
            for word in name_words:
                if len(word) < 5 or abs(len(word) - len(token)) > 3:
                    continue
                if _dice(query_tri, _trigrams(word)) >= 0.6:
                    return 25.0
        except Exception:
            pass
    if token in parent.split() or token in parent:
        return 15.0
    if token in full:
        return 10.0
    return 0.0


def _idf(token: str, total: int, doc_freq: dict[str, int]) -> float:
    df = doc_freq.get(token, 0) if doc_freq else 0
    if total <= 1 or df <= 0:
        return 1.0
    try:
        return 1.0 + min(1.0, max(0.0, math.log((total + 1) / (df + 1)) / 2.0))
    except (ValueError, OverflowError):
        return 1.0


def _kind_ok(entry: dict[str, Any], kind: str) -> bool:
    try:
        from app.ev.laptop_files import _matches_kind as _match
    except Exception:
        return True
    try:
        return bool(_match(Path(entry["path"]), kind))
    except OSError:
        return False


def _denied(path: str) -> bool:
    try:
        from app.ev.laptop_files import path_denied as _denied_fn
    except Exception:
        return False
    try:
        return _denied_fn(Path(path)) is not None
    except OSError:
        return True


def _cheap_denied(path: Path) -> bool:
    try:
        lowered = str(path).replace("\\", "/").lower()
    except Exception:
        return True
    for part in path.parts:
        low = part.lower()
        if low in {".ssh", ".aws", ".gnupg", ".gnupg2", ".kube", "keychains", "keychain"}:
            return True
        if low in {
            ".env",
            ".env.local",
            ".env.production",
            "id_rsa",
            "id_ed25519",
            "id_ecdsa",
            "credentials.json",
            "credentials.csv",
            "master.key",
            "api.env",
        }:
            return True
    if lowered.endswith((".pem", ".p12", ".pfx", ".key", ".kdbx")):
        return True
    return (
        "/library/application support/ev" in lowered
        or "/.ev/secrets" in lowered
        or "/.git/" in lowered
    )


def _normalize(text: str) -> str:
    raw = text or ""
    if raw.isascii():
        folded = raw
    else:
        try:
            folded = unicodedata.normalize("NFKD", raw)
            folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
        except Exception:
            folded = raw
    return re.sub(r"\s+", " ", folded.lower()).strip()


def _stem(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
        return token[:-1]
    return token


def _edits_at_most_one(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) == 1
    if len(left) > len(right):
        left, right = right, left
    i = j = diffs = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        diffs += 1
        if diffs > 1:
            return False
        j += 1
    return True


def _edits_at_most_two(left: str, right: str) -> bool:
    if _edits_at_most_one(left, right):
        return True
    if abs(len(left) - len(right)) > 2:
        return False
    # Bounded DP for short filename tokens only.
    if len(left) > 24 or len(right) > 24:
        return False
    prev = list(range(len(right) + 1))
    for i, left_ch in enumerate(left, 1):
        cur = [i]
        row_min = i
        for j, right_ch in enumerate(right, 1):
            cost = 0 if left_ch == right_ch else 1
            best = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(best)
            row_min = min(row_min, best)
        if row_min > 2:
            return False
        prev = cur
    return prev[-1] <= 2
