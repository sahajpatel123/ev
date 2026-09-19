"""Unified explain: one bounded, purpose-first summary for anything on this Mac.

Evie used to route "tell me about X project" through the full coding loop
(Spark/Luna, up to 16 tool steps, minutes of remote calls) and answer with a
file dump. Generic asks ("explain this PDF", "what's in this folder",
"analyze this file") never had a home at all: they fell into the generic
Muse tool loop (search -> list -> read, one round per step) or hit
``laptop_files._read_file`` which refuses PDFs as "not a text file".

This module is the fix: a single synchronous dispatcher with no LLM calls.

- project -> ``code_literacy.project_card`` (purpose-first, cached)
- folder  -> bounded listing + README/OVERVIEW/package purpose, never a dump
- file    -> bounded read + deterministic summary (lines, language, gist)
- PDF     -> lazy text extraction (pypdf -> pdfminer -> pdftotext), then same
             summarizer; honest degraded when no engine is installed

All paths are bounded (no recursive walks beyond one listing, 256 KiB reads,
8000-char PDF slices) and return ``spoken`` <= 700 chars. No writes, no
network, no model calls — safe on the voice hot path.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

MAX_EXPLAIN_CHARS = 700
MAX_PDF_CHARS = 8000
MAX_FOLDER_NAMES = 12

_EXPLAIN_RE = re.compile(
    r"\b(?:"
    r"tell me about|talk (?:to me )?about|explain|describe|summariz(?:e|ing)|"
    r"give (?:me )?(?:some )?(?:the )?(?:info|information|details|an overview|a rundown)|"
    r"info(?:rmation)? (?:about|on|regarding)|details about|overview of|"
    r"what(?:'s| is)(?: in| inside)?|what is inside|what's inside|"
    r"walk me through|help me understand|"
    r"analy[sz]e|analysis of|review|break(?:ing)? down|give me the gist"
    r")\b",
    re.IGNORECASE,
)

_CAMERA_RE = re.compile(
    r"\b(?:"
    r"holding in my hand|in my hand|i'm holding|i am holding|"
    r"showing you|pointing at|what am i holding|"
    r"describe what you see|look at (?:the thing|this|that|what).{0,30}holding|"
    r"\bcamera\b|on (?:my )?screen"
    r")\b",
    re.IGNORECASE,
)

_NOT_EXPLAIN_RE = re.compile(
    r"\b(?:"
    r"write|create|make|build|implement|scaffold|refactor|patch|fix|edit|"
    r"run (?:it|that|the tests)|send|text|call|remind|open (?:the app|safari)"
    r")\b",
    re.IGNORECASE,
)

_QUERY_STOP = frozenset(
    {
        "the", "a", "an", "my", "our", "this", "that", "these", "those",
        "it", "its", "thing", "things", "item", "items", "stuff", "something",
        "more", "about", "tell", "give", "info", "information", "details",
        "hand", "holding", "hold", "look", "see", "want", "please", "me",
        "and", "for", "with", "from", "what", "which", "there",
    }
)

_STRIP_LEAD_RE = re.compile(
    r"^\s*(?:hey |ok(?:ay)? |evie[,.]? |please )?"
    r"(?:can you |could you |would you |please )?"
    r"(?:tell me about|talk (?:to me )?about|explain|describe|summariz(?:e|ing)|"
    r"give (?:me )?(?:some )?(?:the )?(?:info|information|details|an overview|a rundown)(?: about| on)?|"
    r"info(?:rmation)? (?:about|on|regarding)|details about|overview of|"
    r"what(?:'s| is)(?: in| inside)?|walk me through|help me understand|"
    r"analy[sz]e|analysis of|review|break(?:ing)? down|give me the gist)"
    r"\s*(?:about|on|of|for)?\s*",
    re.IGNORECASE,
)

_STRIP_TAIL_RE = re.compile(
    r"\s*(?:for me|please|thanks|thank you)[\s.?!]*$",
    re.IGNORECASE,
)

_KIND_TAIL_RE = re.compile(
    r"\s+(?:project|repo|repository|workspace|codebase|folder|directory|file|pdf|document)s?\s*$",
    re.IGNORECASE,
)

_LANGUAGE_BY_SUFFIX = {
    ".py": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".swift": "Swift",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".java": "Java",
    ".php": "PHP",
    ".kt": "Kotlin",
    ".c": "C",
    ".h": "C header",
    ".cpp": "C++",
    ".hpp": "C++ header",
    ".sh": "shell script",
    ".md": "Markdown note",
    ".markdown": "Markdown note",
    ".txt": "text note",
    ".json": "JSON data",
    ".csv": "CSV data",
    ".yml": "YAML config",
    ".yaml": "YAML config",
    ".toml": "TOML config",
    ".html": "HTML page",
    ".css": "stylesheet",
    ".xml": "XML data",
    ".log": "log",
}


def looks_like_explain_ask(text: str | None) -> bool:
    """True when the owner asked to understand something, not to change it."""
    raw = (text or "").strip()
    if not raw or len(raw) > 2000:
        return False
    # A visible thing in the owner's hand belongs to the camera, not the
    # file index. "Tell me about this item I'm holding" must not match a
    # source file that happens to contain "item".
    if _CAMERA_RE.search(raw):
        return False
    if not _EXPLAIN_RE.search(raw):
        return False
    # "explain X then fix it" is work, not a read-only explain.
    return not re.search(
        r"\b(?:then|and then|after that)\b.{0,40}\b(?:fix|edit|patch|write|run)\b",
        raw.lower(),
    )


def explain_target_from_text(text: str | None) -> str:
    """Best-effort target phrase with the explain verbs stripped."""
    raw = (text or "").strip()
    raw = _STRIP_LEAD_RE.sub("", raw)
    raw = _STRIP_TAIL_RE.sub("", raw)
    raw = re.sub(r"^(?:the |my |our |this |that |a |an )+", "", raw, flags=re.IGNORECASE)
    raw = raw.strip(" .,'\"?!")
    return raw[:120]


def explain_anything(text: str | None, *, roots: list[Path] | None = None) -> dict[str, Any]:
    """Synchronous purpose-first summary for a project, folder, file, or PDF.

    Never raises, never writes, never calls a model. ``brain`` is always
    ``"explain"`` so callers can label the receipt honestly.
    """
    raw = (text or "").strip()
    if _CAMERA_RE.search(raw):
        return {
            "ok": False,
            "spoken": "",
            "kind": "miss",
            "target": "",
            "brain": "explain",
            "degraded": True,
            "files_changed": [],
            "runs": [],
            "error": "camera_turn",
        }
    target = explain_target_from_text(raw)
    try:
        project_hit = _explain_project(raw, target)
        if project_hit is not None:
            return project_hit
    except Exception:
        pass
    try:
        path_hit = _explain_path(raw, target, roots=roots)
        if path_hit is not None:
            return path_hit
    except Exception:
        pass
    label = target or "that"
    return {
        "ok": False,
        "spoken": f"I couldn't find {label} on this Mac. What's the exact name?",
        "kind": "miss",
        "target": target,
        "brain": "explain",
        "degraded": True,
        "files_changed": [],
        "runs": [],
        "error": "unknown_target",
    }


def _explain_project(raw: str, target: str) -> dict[str, Any] | None:
    """A named code project -> purpose-first card. None when not a project."""
    try:
        from app.ev.code_literacy import project_card, spoken_is_file_dump
        from app.ev.code_locate import (
            name_is_rejected,
            preferred_catalog_projects,
            resolve_code_target,
        )
        from app.ev.code_runtime import is_sandbox_workspace
    except Exception:
        return None

    candidates: list[str] = []
    try:
        for name in preferred_catalog_projects(raw):
            if name not in candidates:
                candidates.append(name)
    except Exception:
        pass
    try:
        from app.ev.code_literacy import project_name_for_alias

        alias = project_name_for_alias(raw)
        if alias and alias not in candidates:
            try:
                if not name_is_rejected(raw, alias):
                    candidates.append(alias)
            except Exception:
                candidates.append(alias)
    except Exception:
        pass
    located = None
    try:
        located = resolve_code_target(raw)
    except Exception:
        located = None
    if located is not None and getattr(located, "kind", "") not in {
        "ambiguous",
        "desk",
        "miss",
    }:
        project = str(getattr(located, "project", "") or "").strip()
        if project and project not in candidates:
            candidates.append(project)
    if target and target not in candidates and len(target) >= 2:
        # Let the card lookup below decide; a bare folder name (e.g. the
        # sticky project) still deserves the purpose card when it resolves.
        candidates.append(target)

    try:
        from app.ev.code_runtime import list_projects
    except Exception:
        list_projects = None  # type: ignore[assignment]

    catalog: dict[str, Path] = {}
    if list_projects is not None:
        try:
            for item in list_projects() or []:
                name = str(item.get("name") or "").strip()
                path = str(item.get("path") or "").strip()
                if name and path:
                    catalog[name] = Path(path)
                    if name.lower() not in catalog:
                        catalog[name.lower()] = Path(path)
        except Exception:
            catalog = {}

    for name in candidates:
        key = (name or "").strip()
        if not key:
            continue
        root = catalog.get(key) or catalog.get(key.lower())
        if root is None and located is not None:
            try:
                loc_root = getattr(located, "root", None)
                loc_project = str(getattr(located, "project", "") or "")
                if loc_root is not None and (
                    _norm(key) == _norm(loc_project)
                    or _norm(key) == _norm(str(getattr(located, "name", "") or ""))
                ):
                    root = Path(str(loc_root))
            except Exception:
                root = None
        if root is None or not root.exists() or not root.is_dir():
            continue
        try:
            if is_sandbox_workspace(root):
                continue
        except Exception:
            pass
        try:
            card = project_card(root=root)
        except Exception:
            continue
        spoken = str(card.get("spoken") or "").strip()
        if not spoken:
            continue
        try:
            if spoken_is_file_dump(spoken):
                fallback = str(card.get("one_liner") or "").strip()
                if fallback and not spoken_is_file_dump(fallback):
                    spoken = fallback
                else:
                    continue
        except Exception:
            pass
        out = dict(card)
        out["spoken"] = spoken[:MAX_EXPLAIN_CHARS]
        out["kind"] = "project"
        out["target"] = key
        out["brain"] = "explain"
        out.setdefault("files_changed", [])
        out.setdefault("runs", [])
        return out
    return None


def _explain_path(
    raw: str, target: str, *, roots: list[Path] | None = None
) -> dict[str, Any] | None:
    path = _resolve_owner_path(target, roots=roots)
    if path is None and raw:
        # The target phrase may include kind tails ("X folder", "Y pdf")
        # that the index handles better stripped.
        stripped = _KIND_TAIL_RE.sub("", target or "").strip()
        if stripped and stripped != target:
            path = _resolve_owner_path(stripped, roots=roots)
            if path is not None:
                target = stripped
    if path is None:
        folder = _folder_alias_path(raw, target)
        if folder is not None:
            return explain_folder(folder)
        return None
    if path.is_dir():
        return explain_folder(path)
    if path.is_file():
        return explain_file(path)
    return None


def _query_tokens(query: str) -> list[str]:
    parts = re.findall(r"[a-z0-9]+", (query or "").lower())
    return [p for p in parts if len(p) >= 3 and p not in _QUERY_STOP][:8]


def _hit_matches_query(hit: Path, query: str) -> bool:
    """A fuzzy index hit only counts when the owner actually named the file."""
    raw = (query or "").strip()
    if not raw:
        return False
    name = hit.name.lower()
    stem = hit.stem.lower()
    lowered = raw.lower()
    if "." in name:
        if name in lowered:
            return True
    elif re.search(r"\b" + re.escape(name) + r"\b", lowered):
        if name not in _QUERY_STOP and len(name) >= 4:
            return True
        if lowered.strip() == name:
            return True
    for token in _query_tokens(raw):
        compact_name = re.sub(r"[\s._-]+", "", name)
        compact_stem = re.sub(r"[\s._-]+", "", stem)
        if token in compact_name or token in compact_stem:
            return True
    return False


def _resolve_owner_path(query: str, *, roots: list[Path] | None = None) -> Path | None:
    token = (query or "").strip().strip("\"'").strip()
    if not token or len(token) < 2:
        return None
    # Direct path the owner pasted.
    candidate = Path(token).expanduser()
    if len(candidate.parts) > 1 or "/" in token:
        try:
            if candidate.exists() and _allowed(candidate):
                return candidate
        except OSError:
            pass
    search_roots = list(roots or []) or _allowed_roots()
    # Exact filename match first ("Q3 report.pdf").
    try:
        from app.ev import laptop_files

        deny = laptop_files.path_denied
    except Exception:
        deny = None  # type: ignore[assignment]
    needles = [token, _KIND_TAIL_RE.sub("", token).strip()]
    for needle in dict.fromkeys(n for n in needles if n):
        try:
            from app.ev.file_index import scored_search

            hits = scored_search(needle, roots=search_roots or None, kind="")
        except Exception:
            hits = []
        for hit in hits or []:
            try:
                if deny is not None and deny(hit):
                    continue
                if not hit.exists():
                    continue
                if _hit_matches_query(hit, needle):
                    return hit
            except OSError:
                continue
        # Spotlight/content fallback for renamed/aliased files.
        try:
            from app.ev.file_retrieve import collect_retrieve_hits

            extra = collect_retrieve_hits(needle)
        except Exception:
            extra = []
        for hit in extra or []:
            try:
                if deny is not None and deny(hit):
                    continue
                if not hit.exists():
                    continue
                if _hit_matches_query(hit, needle):
                    return hit
            except OSError:
                continue
    return None


def _folder_alias_path(raw: str, target: str) -> Path | None:
    lowered = f"{raw} {target}".lower()
    try:
        from app.ev import laptop_files
    except Exception:
        return None
    for alias, folder in (laptop_files.FOLDER_ALIASES or {}).items():
        if alias and alias.lower() in lowered:
            candidate = Path.home() / folder
            try:
                if candidate.is_dir() and _allowed(candidate):
                    return candidate
            except OSError:
                continue
    return None


def explain_folder(path: Path) -> dict[str, Any]:
    """Bounded folder survey: purpose first, a few names, never a dump."""
    try:
        name = path.name or str(path)
        children = sorted(
            (c for c in path.iterdir() if not c.name.startswith(".")),
            key=lambda c: c.name.lower(),
        )
    except OSError:
        return {
            "ok": False,
            "spoken": f"I couldn't open {path.name}.",
            "kind": "folder",
            "target": path.name,
            "path": str(path),
            "brain": "explain",
            "degraded": True,
            "files_changed": [],
            "runs": [],
            "error": "unreadable",
        }
    total = len(children)
    shown = [c.name + ("/" if c.is_dir() else "") for c in children[:MAX_FOLDER_NAMES]]
    purpose = _folder_purpose(path)
    counts = _folder_counts(children)
    if purpose:
        lead = f"{name} is {purpose.rstrip('.')}."
    elif total == 0:
        lead = f"{name} is an empty folder."
    else:
        lead = f"{name} is a folder with {total} items."
    detail = ""
    if shown and total:
        sample = ", ".join(shown[:6])
        detail = (
            f" It holds {counts}; including {sample}."
            if counts
            else f" Including {sample}."
        )
        if total > len(shown):
            detail = detail.rstrip(".") + f", plus {total - len(shown)} more."
    spoken = (lead + detail).strip()
    return {
        "ok": True,
        "spoken": spoken[:MAX_EXPLAIN_CHARS],
        "kind": "folder",
        "target": name,
        "path": str(path),
        "total": total,
        "sample": shown,
        "brain": "explain",
        "degraded": False,
        "files_changed": [],
        "runs": [],
    }


def explain_file(path: Path) -> dict[str, Any]:
    """Bounded file summary for text and PDFs."""
    suffix = path.suffix.lower()
    name = path.name
    if suffix == ".pdf":
        return explain_pdf(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    text_exts: set[str] = set()
    try:
        from app.ev import laptop_files

        text_exts = set(laptop_files.TEXT_EXTENSIONS or ())
    except Exception:
        text_exts = set(_LANGUAGE_BY_SUFFIX)
    if suffix and suffix not in text_exts:
        return {
            "ok": True,
            "spoken": (
                f"{name} is a {suffix.lstrip('.').upper()} file"
                + (f" ({size // 1024} KB)" if size >= 1024 else "")
                + ". I can't summarize that format — I can open it or tell you where it lives."
            )[:MAX_EXPLAIN_CHARS],
            "kind": "file",
            "target": name,
            "path": str(path),
            "brain": "explain",
            "degraded": True,
            "files_changed": [],
            "runs": [],
        }
    try:
        data = path.read_bytes()[: 256 * 1024]
    except OSError:
        return {
            "ok": False,
            "spoken": f"I couldn't read {name}.",
            "kind": "file",
            "target": name,
            "path": str(path),
            "brain": "explain",
            "degraded": True,
            "files_changed": [],
            "runs": [],
            "error": "unreadable",
        }
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    gist = _summarize_text(name, text)
    language = _LANGUAGE_BY_SUFFIX.get(suffix, "text file" if suffix else "note")
    count = len([ln for ln in lines if ln.strip()])
    if gist:
        spoken = f"{name} is {article(language)} {language} with {count} lines. {gist}"
    else:
        spoken = f"{name} is {article(language)} {language} with {count} lines."
        extra = _code_symbols(text)
        if extra:
            spoken += f" It defines {extra}."
    return {
        "ok": True,
        "spoken": spoken[:MAX_EXPLAIN_CHARS],
        "kind": "file",
        "target": name,
        "path": str(path),
        "brain": "explain",
        "degraded": False,
        "files_changed": [],
        "runs": [],
    }


def explain_pdf(path: Path) -> dict[str, Any]:
    """PDF summary via a lazy engine; honest degraded when none is installed."""
    name = path.name
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    text, pages, engine = extract_pdf_text(path)
    if not text.strip():
        size_bit = f" ({size // 1024} KB)" if size >= 1024 else ""
        reason = (
            "I don't have a PDF reader installed"
            if engine == "none"
            else "I couldn't pull text from it (it may be scanned images)"
        )
        return {
            "ok": True,
            "spoken": (
                f"{name} is a PDF{size_bit}"
                + (f" with {pages} pages" if pages else "")
                + f". {reason} — I can open it or tell you where it lives."
            )[:MAX_EXPLAIN_CHARS],
            "kind": "pdf",
            "target": name,
            "path": str(path),
            "pages": pages,
            "engine": engine,
            "brain": "explain",
            "degraded": True,
            "files_changed": [],
            "runs": [],
        }
    gist = _summarize_text(name, text)
    page_bit = f", {pages} pages" if pages else ""
    if gist:
        spoken = f"{name} is a PDF{page_bit}. It covers: {gist}"
    else:
        spoken = f"{name} is a PDF{page_bit} with readable text."
    return {
        "ok": True,
        "spoken": spoken[:MAX_EXPLAIN_CHARS],
        "kind": "pdf",
        "target": name,
        "path": str(path),
        "pages": pages,
        "engine": engine,
        "brain": "explain",
        "degraded": False,
        "files_changed": [],
        "runs": [],
    }


def extract_pdf_text(path: Path, *, max_chars: int = MAX_PDF_CHARS) -> tuple[str, int, str]:
    """Best-effort PDF text. Lazy engines only; ("", 0, "none") when absent."""
    raw = Path(str(path))
    text = ""
    pages = 0
    # 1) pypdf — pure Python, most common.
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = PdfReader(str(raw))
        pages = len(getattr(reader, "pages", []) or [])
        parts: list[str] = []
        for page in (getattr(reader, "pages", []) or [])[:20]:
            try:
                chunk = page.extract_text() or ""
            except Exception:
                chunk = ""
            if chunk.strip():
                parts.append(chunk)
            if sum(len(p) for p in parts) >= max_chars:
                break
        text = "\n".join(parts)[:max_chars]
        if text.strip():
            return text, pages, "pypdf"
    except Exception:
        pass
    # 2) pdfminer.six — better layout, heavier.
    try:
        from pdfminer.high_level import extract_text  # type: ignore[import-not-found]

        text = (extract_text(str(raw), maxpages=10) or "")[:max_chars]
        if text.strip():
            return text, pages, "pdfminer"
    except Exception:
        pass
    # 3) poppler pdftotext when the binary exists.
    try:
        proc = subprocess.run(
            ["pdftotext", "-l", "10", "-layout", str(raw), "-"],
            capture_output=True,
            timeout=8,
            check=False,
        )
        if proc.returncode == 0:
            text = proc.stdout.decode("utf-8", errors="replace")[:max_chars]
            if text.strip():
                return text, pages, "pdftotext"
    except Exception:
        pass
    return "", pages, "none"


def _summarize_text(name: str, text: str, *, budget: int = 420) -> str:
    blob = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")
    blob = re.sub(r"#{1,6}\s*", "", blob)
    blob = re.sub(r"\s+", " ", blob).strip()
    if not blob:
        return ""
    # Prefer real sentences over import noise / front-matter.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", blob) if s.strip()]
    picked: list[str] = []
    for sentence in sentences:
        low = sentence.lower()
        if len(sentence) < 24:
            continue
        if re.match(r"^(import|from|package|//|#|const|let|var|func|def |class )", sentence):
            continue
        if low.startswith(("copyright", "license", "all rights reserved")):
            continue
        picked.append(sentence.rstrip("."))
        if len(". ".join(picked)) >= budget or len(picked) >= 3:
            break
    if not picked:
        lines = [ln.strip(" #*->") for ln in (text or "").splitlines()]
        lines = [ln for ln in lines if len(ln) >= 24][:3]
        picked = [ln.rstrip(".") for ln in lines]
    out = ". ".join(picked).strip()
    if out and not out.endswith("."):
        out += "."
    return out[:budget]


def _code_symbols(text: str, *, limit: int = 5) -> str:
    names: list[str] = []
    for pattern in (
        r"^\s*(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*(?:const|let|var|function)\s+([A-Za-z_][A-Za-z0-9_]*)",
    ):
        for match in re.finditer(pattern, text or "", re.MULTILINE):
            token = match.group(1)
            if token not in names and token not in {"main", "test", "init"}:
                names.append(token)
            if len(names) >= limit:
                break
        if len(names) >= limit:
            break
    return ", ".join(names)


def _folder_purpose(path: Path) -> str:
    for candidate in ("OVERVIEW.md", "README.md", "AGENTS.md", "package.json", "pyproject.toml"):
        doc = path / candidate
        try:
            if not doc.is_file() or doc.stat().st_size > 64 * 1024:
                continue
            raw = doc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if candidate.endswith(".json"):
            desc = _package_desc(raw)
            if desc:
                return desc.rstrip(".")
            continue
        if candidate.endswith(".toml"):
            _name, desc = _pyproject_desc(raw)
            if desc:
                return desc.rstrip(".")
            continue
        claim = _first_claim(raw)
        if claim:
            return claim.rstrip(".")
    return ""


def _first_claim(text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line or "").strip(" #*>-")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) < 24 or len(cleaned) > 220:
            continue
        low = cleaned.lower()
        if low.startswith(("copyright", "license", "npm ", "pip install", "uv run")):
            continue
        return cleaned.rstrip(".")
    return ""


def _package_desc(text: str) -> str:
    try:
        import json

        data = json.loads(text or "{}")
    except Exception:
        return ""
    if isinstance(data, dict):
        desc = str(data.get("description") or "").strip()
        return re.sub(r"\s+", " ", desc)
    return ""


def _pyproject_desc(text: str) -> tuple[str, str]:
    name = ""
    desc = ""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("name") and "=" in stripped and not name:
            name = stripped.split("=", 1)[1].strip().strip("\"' ")
        if stripped.startswith("description") and "=" in stripped and not desc:
            desc = stripped.split("=", 1)[1].strip().strip("\"' ")
    return name, desc


def _folder_counts(children: list[Path]) -> str:
    folders = sum(1 for c in children if c.is_dir())
    files = len(children) - folders
    kinds: dict[str, int] = {}
    for child in children:
        if child.is_dir():
            continue
        suffix = child.suffix.lower().lstrip(".") or "files"
        kinds[suffix] = kinds.get(suffix, 0) + 1
    bits: list[str] = []
    if folders:
        bits.append(f"{folders} folders")
    if files:
        top = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
        if top and files >= 3:
            bits.append("/".join(f"{n} {ext}" for ext, n in top))
        else:
            bits.append(f"{files} files")
    return ", ".join(bits)


def _allowed(path: Path) -> bool:
    try:
        from app.ev import laptop_files

        return laptop_files.path_denied(path) is None
    except Exception:
        return True


def _allowed_roots() -> list[Path]:
    try:
        from app.ev import laptop_files

        return list(laptop_files.allowed_roots())
    except Exception:
        return [Path.home()]


def _norm(value: str) -> str:
    return re.sub(r"[\s._-]+", "", (value or "").lower())


def article(word: str) -> str:
    return "an" if (word or "")[:1].lower() in "aeiou" else "a"
