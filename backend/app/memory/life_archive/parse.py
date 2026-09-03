"""Parse takeout formats into bounded, model-safe payloads.

Never returns blob bytes. Phone numbers and emails are omitted from `text`.
Quarantined files must not be passed here.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import Parser
from email.policy import default as email_policy
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_VCARD_FN = re.compile(r"^FN[;:](.+)$", re.IGNORECASE | re.MULTILINE)
_VCARD_BEGIN = re.compile(r"BEGIN:VCARD", re.IGNORECASE)
_ICS_BEGIN_EVENT = "BEGIN:VEVENT"
_ICS_BEGIN_TODO = "BEGIN:VTODO"


@dataclass(frozen=True)
class ParsedItem:
    event_type: str
    text: str
    occurred_at: datetime | None
    content: dict[str, Any]
    privacy_level: str
    item_key: str


def parse_record(root: Path, rel: str, adapter: str) -> list[ParsedItem]:
    path = root / rel
    if not path.is_file():
        return []
    if adapter == "contacts":
        return list(_parse_vcf(path, rel))
    if adapter == "calendar":
        return list(_parse_ics(path, rel))
    if adapter == "keep":
        return list(_parse_keep(path, rel))
    if adapter == "tasks":
        return list(_parse_tasks(path, rel))
    if adapter == "bookmarks":
        return list(_parse_bookmarks(path, rel))
    if adapter == "notebooklm":
        return list(_parse_notebooklm(path, rel))
    if adapter == "drive":
        return list(_parse_drive(path, rel))
    if adapter == "photos":
        return list(_parse_photo(path, rel))
    if adapter == "photos_meta":
        return list(_parse_photo_details(path, rel))
    if adapter == "photos_zip":
        return list(_parse_photo_zip(path, rel))
    if adapter == "whatsapp":
        return list(_parse_whatsapp(path, rel))
    if adapter == "mail":
        return list(_parse_mbox(path, rel))
    return []


def _parse_vcf(path: Path, rel: str) -> Iterator[ParsedItem]:
    raw = _read_text(path)
    if not raw:
        return
    unfolded = _unfold(raw)
    blocks = _VCARD_BEGIN.split(unfolded)
    for index, block in enumerate(blocks):
        if not block.strip():
            continue
        fn = _vcard_fn(block) or path.stem
        name = _clean_name(fn)
        if not name:
            continue
        key = f"contact:{rel}:{index}:{name.lower()}"
        yield ParsedItem(
            event_type="life.contact",
            text=f"Owner contact: {name}.",
            occurred_at=None,
            content={"kind": "contact", "name": name, "ref": rel},
            privacy_level="normal",
            item_key=key,
        )


def _vcard_fn(block: str) -> str | None:
    match = _VCARD_FN.search(block.replace("\r", ""))
    if not match:
        return None
    value = match.group(1).strip()
    if (
        (value.startswith("charset=") or value.lower().startswith("quoted-printable"))
        and ":" in value
    ):
        value = value.split(":", 1)[1]
    return value.strip().strip('"')


def _parse_ics(path: Path, rel: str) -> Iterator[ParsedItem]:
    raw = _unfold(_read_text(path) or "")
    if not raw:
        return
    current: list[str] = []
    kind = ""
    for line in raw.splitlines():
        upper = line.upper()
        if upper.startswith(_ICS_BEGIN_EVENT) or upper.startswith(_ICS_BEGIN_TODO):
            current = [line]
            kind = "event" if "VEVENT" in upper else "task"
            continue
        if current:
            current.append(line)
            if upper.startswith("END:VEVENT") or upper.startswith("END:VTODO"):
                fields = _ics_fields(current)
                summary = fields.get("SUMMARY") or "Untitled"
                when = _ics_datetime(fields.get("DTSTART") or fields.get("DUE") or "")
                uid = fields.get("UID") or summary
                prefix = "Calendar" if kind == "event" else "Reminder"
                text = f"{prefix}: {summary}."
                if when:
                    text = f"{prefix}: {summary} ({when.date().isoformat()})."
                yield ParsedItem(
                    event_type="life.calendar.event" if kind == "event" else "life.task",
                    text=text,
                    occurred_at=when,
                    content={
                        "kind": kind,
                        "summary": summary,
                        "uid": uid,
                        "ref": rel,
                    },
                    privacy_level="normal",
                    item_key=f"ics:{rel}:{uid}",
                )
                current = []
                kind = ""


def _ics_fields(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        name = key.split(";", 1)[0].upper()
        out[name] = value.strip()
    return out


def _ics_datetime(raw: str) -> datetime | None:
    token = (raw or "").strip()
    if not token:
        return None
    digits = re.sub(r"[^0-9]", "", token)
    try:
        if len(digits) >= 8:
            year, month, day = int(digits[0:4]), int(digits[4:6]), int(digits[6:8])
            hour = int(digits[8:10]) if len(digits) >= 10 else 0
            minute = int(digits[10:12]) if len(digits) >= 12 else 0
            return datetime(year, month, day, hour, minute, tzinfo=UTC)
    except ValueError:
        return None
    return None


def _parse_keep(path: Path, rel: str) -> Iterator[ParsedItem]:
    data = _read_json(path)
    if not isinstance(data, dict):
        return
    if data.get("isTrashed"):
        return
    title = str(data.get("title") or "").strip()
    body = str(data.get("textContent") or "").strip()
    text = body or title
    if not text:
        text = "Keep image note."
        if data.get("attachments"):
            text = "Keep image note (attachment)."
    occurred = _usec(data.get("userEditedTimestampUsec") or data.get("createdTimestampUsec"))
    yield ParsedItem(
        event_type="life.note",
        text=f"Note: {text}"[:4000],
        occurred_at=occurred,
        content={"kind": "keep", "title": title, "ref": rel},
        privacy_level="normal",
        item_key=f"keep:{rel}",
    )


def _parse_tasks(path: Path, rel: str) -> Iterator[ParsedItem]:
    data = _read_json(path)
    if not isinstance(data, dict):
        return
    for list_index, lst in enumerate(data.get("items") or []):
        if not isinstance(lst, dict):
            continue
        list_title = str(lst.get("title") or "Tasks").strip()
        for task_index, task in enumerate(lst.get("items") or []):
            if not isinstance(task, dict):
                continue
            title = str(task.get("title") or "").strip()
            if not title:
                continue
            status = str(task.get("status") or "needsAction")
            yield ParsedItem(
                event_type="life.task",
                text=f"Task ({list_title}): {title} [{status}].",
                occurred_at=None,
                content={
                    "kind": "task",
                    "title": title,
                    "list": list_title,
                    "status": status,
                    "ref": rel,
                },
                privacy_level="normal",
                item_key=f"task:{rel}:{list_index}:{task_index}:{title.lower()}",
            )


def _parse_bookmarks(path: Path, rel: str) -> Iterator[ParsedItem]:
    if path.suffix.lower() == ".html":
        yield from _parse_bookmark_html(path, rel)
        return
    if path.suffix.lower() == ".csv":
        yield from _parse_bookmark_csv(path, rel)
        return


def _parse_bookmark_html(path: Path, rel: str) -> Iterator[ParsedItem]:
    class _Links(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.links: list[tuple[str, str]] = []
            self._href = ""
            self._capture = False
            self._buf: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag.lower() != "a":
                return
            href = dict(attrs).get("href") or ""
            self._href = href
            self._capture = True
            self._buf = []

        def handle_data(self, data: str) -> None:
            if self._capture:
                self._buf.append(data)

        def handle_endtag(self, tag: str) -> None:
            if tag.lower() == "a" and self._capture:
                title = "".join(self._buf).strip() or self._href
                if self._href:
                    self.links.append((title, self._href))
                self._capture = False

    parser = _Links()
    parser.feed(_read_text(path) or "")
    for index, (title, href) in enumerate(parser.links[:400]):
        if href.startswith("javascript:"):
            continue
        yield ParsedItem(
            event_type="life.bookmark",
            text=f"Bookmark: {title}.",
            occurred_at=None,
            content={"kind": "bookmark", "title": title[:200], "url": href[:500], "ref": rel},
            privacy_level="normal",
            item_key=f"bookmark:{rel}:{index}:{href[:80]}",
        )


def _parse_bookmark_csv(path: Path, rel: str) -> Iterator[ParsedItem]:
    text = _read_text(path) or ""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return
    header = [part.strip().strip('"').lower() for part in lines[0].split(",")]
    url_i = next((i for i, name in enumerate(header) if "url" in name or name == "link"), None)
    title_i = next((i for i, name in enumerate(header) if "title" in name or "name" in name), 0)
    for index, line in enumerate(lines[1:400]):
        cols = [part.strip().strip('"') for part in line.split(",")]
        title = cols[title_i] if title_i < len(cols) else ""
        url = cols[url_i] if url_i is not None and url_i < len(cols) else ""
        if not title and not url:
            continue
        yield ParsedItem(
            event_type="life.bookmark",
            text=f"Bookmark: {title or url}.",
            occurred_at=None,
            content={"kind": "bookmark", "title": (title or url)[:200], "url": url[:500], "ref": rel},
            privacy_level="normal",
            item_key=f"bookmark:{rel}:{index}:{url[:80]}",
        )


def _parse_notebooklm(path: Path, rel: str) -> Iterator[ParsedItem]:
    if path.suffix.lower() in {".html", ".htm", ".md", ".txt"}:
        body = (_read_text(path) or "")
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()[:1500]
        title = path.stem
        text = f"NotebookLM: {title}."
        if body:
            text = f"NotebookLM {title}: {body}"
        yield ParsedItem(
            event_type="life.note",
            text=text[:4000],
            occurred_at=None,
            content={"kind": "notebooklm", "title": title, "ref": rel},
            privacy_level="normal",
            item_key=f"notebooklm:{rel}",
        )
        return
    data = _read_json(path)
    if not isinstance(data, dict):
        return
    title = str(data.get("title") or data.get("name") or path.parent.name).strip()
    if not title:
        return
    yield ParsedItem(
        event_type="life.note",
        text=f"NotebookLM: {title}.",
        occurred_at=None,
        content={"kind": "notebooklm", "title": title, "ref": rel},
        privacy_level="normal",
        item_key=f"notebooklm:{rel}",
    )


def _parse_drive(path: Path, rel: str) -> Iterator[ParsedItem]:
    suffix = path.suffix.lower()
    title = path.stem
    body = ""
    if suffix in {".md", ".txt", ".html", ".htm"}:
        body = (_read_text(path) or "")[:4000]
    elif suffix == ".json":
        body = (_read_text(path) or "")[:2000]
    elif suffix == ".ipynb":
        body = _ipynb_text(path)[:4000]
    text = f"Drive file: {title}."
    if body.strip():
        text = f"Drive file {title}: {body.strip()[:1500]}"
    yield ParsedItem(
        event_type="life.note",
        text=text[:4000],
        occurred_at=None,
        content={"kind": "drive", "title": title, "ref": rel, "suffix": suffix},
        privacy_level="normal",
        item_key=f"drive:{rel}",
    )


def _parse_photo(path: Path, rel: str) -> Iterator[ParsedItem]:
    sidecar = _photo_sidecar(path)
    taken = None
    album = path.parent.name
    title = path.stem
    if sidecar:
        taken_raw = (sidecar.get("photoTakenTime") or {}).get("timestamp")
        try:
            if taken_raw:
                taken = datetime.fromtimestamp(int(taken_raw), tz=UTC)
        except (TypeError, ValueError, OSError):
            taken = None
        title = str(sidecar.get("title") or title)
        # Never copy geo into text.
    year = taken.year if taken is not None else None
    text = f"Photo in archive: {title}."
    if year is not None:
        text = f"Photo in archive: {title} ({year})."
    yield ParsedItem(
        event_type="life.photo.index",
        text=text,
        occurred_at=taken,
        content={
            "kind": "photo",
            "title": title,
            "album": album,
            "ref": rel,
            "bytes": _size(path),
            "year": year,
        },
        privacy_level="normal",
        item_key=f"photo:{rel}:{_size(path)}",
    )


def _parse_photo_details(path: Path, rel: str) -> Iterator[ParsedItem]:
    """Dated Apple Photos catalog. One row per image; never the pixels."""
    try:
        handle = path.open(newline="", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name = str(row.get("imgName") or "").strip()
            if not name:
                continue
            if str(row.get("hidden") or "").strip().lower() == "yes":
                continue
            if str(row.get("deleted") or "").strip().lower() == "yes":
                continue
            taken = _apple_photo_date(str(row.get("originalCreationDate") or ""))
            year = taken.year if taken is not None else None
            favorite = str(row.get("favorite") or "").strip().lower() == "yes"
            disk = path.parent / name
            nbytes = _size(disk) if disk.is_file() else 0
            label = "Favorite photo" if favorite else "Photo"
            text = f"{label} in archive: {name}."
            if year is not None:
                text = f"{label} in archive: {name} ({year})."
            yield ParsedItem(
                event_type="life.photo.index",
                text=text,
                occurred_at=taken,
                content={
                    "kind": "photo",
                    "title": name,
                    "album": path.parent.name,
                    "ref": rel,
                    "bytes": nbytes,
                    "year": year,
                    "favorite": favorite,
                },
                privacy_level="normal",
                item_key=f"photo:details:{rel}:{name}",
            )


def _apple_photo_date(raw: str) -> datetime | None:
    blob = re.sub(r",(\d{4})", r", \1", (raw or "").strip())
    blob = re.sub(r"\s+", " ", blob)
    blob = re.sub(r"\s+GMT.*$", "", blob, flags=re.IGNORECASE).strip()
    for fmt in ("%A %B %d, %Y %I:%M %p", "%A %B %d, %Y %I:%M:%S %p"):
        try:
            return datetime.strptime(blob, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


_WA_LINE = re.compile(
    r"^[\u200e\u200f]*\[(\d{1,2}/\d{1,2}/\d{2,4}),\s+(\d{1,2}:\d{2}(?::\d{2})?)"
    r"[\u202f\s]*([AP]M)\]\s+([^:]+):\s*(.*)$",
    re.IGNORECASE,
)
_WA_SYSTEM = (
    "messages and calls are end-to-end encrypted",
    "this business uses a secure service",
    "this business is now using a secure service",
    "is a contact",
    "waiting for this message",
    "this message was deleted",
    "you deleted this message",
)
_WA_BRANDS = frozenset({"bookmyshow", "book my show"})
_WA_WEAK_TITLES = frozenset({"job", "home", "group", "family", "chat", "the"})
_WA_FAMILY = {
    "mummy": ("parent", ("mom", "mummy", "mommy", "mum", "mother", "maa")),
    "mom": ("parent", ("mom", "mummy", "mommy", "mum", "mother", "maa")),
    "mommy": ("parent", ("mom", "mummy", "mommy", "mum", "mother", "maa")),
    "mum": ("parent", ("mom", "mummy", "mommy", "mum", "mother", "maa")),
    "mother": ("parent", ("mom", "mummy", "mommy", "mum", "mother", "maa")),
    "maa": ("parent", ("mom", "mummy", "mommy", "mum", "mother", "maa")),
    "ammi": ("parent", ("mom", "mummy", "mommy", "mum", "mother", "maa", "ammi")),
    "papa": ("parent", ("dad", "daddy", "papa", "father")),
    "dad": ("parent", ("dad", "daddy", "papa", "father")),
    "daddy": ("parent", ("dad", "daddy", "papa", "father")),
    "father": ("parent", ("dad", "daddy", "papa", "father")),
    "baba": ("parent", ("dad", "daddy", "papa", "father", "baba")),
    "mama": ("family", ("mama",)),
    "suresh": ("family", ("suresh",)),
}
_OWNER_SENDERS = ("sahaj patel", "sahaj")
_PHOTO_ZIP_MEDIA = frozenset(
    {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".dng", ".raw", ".mp4", ".mov", ".m4v"}
)


def _parse_photo_zip(path: Path, rel: str) -> Iterator[ParsedItem]:
    from zipfile import ZipFile

    try:
        archive = ZipFile(path)
    except Exception:  # noqa: BLE001 - a stub zip must not abort ingest
        return
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            inner = info.filename.replace("\\", "/")
            if "recently deleted" in inner.lower():
                continue
            suffix = Path(inner).suffix.lower()
            if suffix not in _PHOTO_ZIP_MEDIA:
                continue
            title = Path(inner).stem
            album = Path(inner).parent.name or "Photos"
            yield ParsedItem(
                event_type="life.photo.index",
                text=f"Photo in archive: {title}.",
                occurred_at=None,
                content={
                    "kind": "photo",
                    "title": title,
                    "album": album,
                    "ref": f"{rel}:{inner}",
                    "bytes": int(info.file_size),
                },
                privacy_level="normal",
                item_key=f"photo:{rel}:{inner}:{info.file_size}",
            )


def _parse_whatsapp(path: Path, rel: str) -> Iterator[ParsedItem]:
    from zipfile import ZipFile

    try:
        with ZipFile(path) as archive:
            raw = archive.read("_chat.txt")
    except (KeyError, Exception):  # noqa: BLE001
        return
    text = raw.decode("utf-8", errors="replace")
    title = _whatsapp_title(rel)
    messages = _whatsapp_messages(text)
    if not messages:
        return
    owner_n = sum(1 for item in messages if item["owner"])
    other_n = len(messages) - owner_n
    others = [item["sender"] for item in messages if not item["owner"]]
    unique_others = list(dict.fromkeys(others))
    first_at = messages[0]["when"]
    last_at = messages[-1]["when"]
    span = _whatsapp_span(first_at, last_at)
    scripts = _whatsapp_scripts(" ".join(item["body"] for item in messages[:80]))
    is_group = (
        len(unique_others) >= 3
        or title.lower() in _WA_WEAK_TITLES
        or _whatsapp_group_title(title)
    )
    brand = title.lower().replace(" ", "") in {item.replace(" ", "") for item in _WA_BRANDS}
    counterpart = unique_others[0] if unique_others else title
    display = _clean_name(title) or _clean_name(counterpart) or title
    if display.startswith("."):
        display = display.lstrip("._ ")

    thread_text = (
        f"WhatsApp thread: {display}. {len(messages)} messages{span}."
        f"{' Group chat.' if is_group else ''}"
        f"{f' {scripts}.' if scripts else ''}"
    )
    yield ParsedItem(
        event_type="life.chat.thread",
        text=thread_text[:400],
        occurred_at=last_at,
        content={
            "kind": "whatsapp_thread",
            "title": display,
            "messages": len(messages),
            "owner_messages": owner_n,
            "peer_messages": other_n,
            "group": is_group,
            "ref": rel,
        },
        privacy_level="normal",
        item_key=f"whatsapp:thread:{rel}",
    )

    if not is_group and not brand and display.lower() not in _WA_WEAK_TITLES:
        relation, aliases = _whatsapp_relation(display)
        style = _whatsapp_owner_style(messages)
        card = (
            f"Person: {display} ({relation}). WhatsApp{span}, "
            f"{len(messages)} messages. {scripts or 'Mostly English.'} "
            f"{style}"
        )
        yield ParsedItem(
            event_type="life.person",
            text=card[:400],
            occurred_at=last_at,
            content={
                "kind": "person",
                "name": display,
                "relation": relation,
                "aliases": list(aliases),
                "title": display,
                "ref": rel,
            },
            privacy_level="normal",
            item_key=f"whatsapp:person:{rel}:{display.lower()}",
        )

    if owner_n:
        style = _whatsapp_owner_style(messages).rstrip(".")
        mixed = bool(scripts) and scripts.startswith("Mixes")
        voice = (
            f"Owner WhatsApp voice with {display}: {style.lower()}"
            f"{'; mixes English with Gujarati or Hindi' if mixed else ''}."
        )
        yield ParsedItem(
            event_type="life.owner.voice",
            text=voice[:400],
            occurred_at=last_at,
            content={"kind": "owner_voice", "title": display, "ref": rel},
            privacy_level="normal",
            item_key=f"whatsapp:voice:{rel}",
        )

    for excerpt in _whatsapp_excerpts(messages, display, owner=True, limit=3):
        yield excerpt
    if not is_group:
        for excerpt in _whatsapp_excerpts(messages, display, owner=False, limit=3):
            yield excerpt
        for excerpt in _whatsapp_recent_excerpts(messages, display, limit=4):
            yield excerpt


def _whatsapp_title(rel: str) -> str:
    stem = Path(rel).stem
    title = re.sub(r"^WhatsApp Chat\s*-\s*", "", stem, flags=re.IGNORECASE).strip()
    title = title.strip("._- !…👽")
    return title or stem


def _whatsapp_messages(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.replace("\u200e", "").replace("\u200f", "").strip()
        match = _WA_LINE.match(line)
        if match:
            if current and current["body"]:
                rows.append(current)
            sender = match.group(4).strip().lstrip(".")
            body = match.group(5).strip()
            current = {
                "sender": sender,
                "body": body,
                "owner": _is_owner_sender(sender),
                "when": _whatsapp_when(match.group(1), match.group(2), match.group(3)),
            }
            continue
        if current and line:
            current["body"] = f"{current['body']} {line}".strip()
    if current and current["body"]:
        rows.append(current)
    return [row for row in rows if _whatsapp_keep(row["body"])]


def _whatsapp_keep(body: str) -> bool:
    lowered = body.lower()
    if not body or len(body) < 2:
        return False
    if any(marker in lowered for marker in _WA_SYSTEM):
        return False
    if "<attached:" in lowered or "omitted>" in lowered:
        return False
    return not lowered.startswith("location:")


def _whatsapp_excerpts(
    messages: list[dict[str, Any]], title: str, *, owner: bool, limit: int
) -> Iterator[ParsedItem]:
    pool = [
        item
        for item in messages
        if bool(item["owner"]) == owner and _whatsapp_excerptable(str(item["body"] or ""))
    ]
    picked = _spread_pick(pool, limit)
    for kept, item in enumerate(picked):
        body = str(item["body"] or "").strip()
        who = "Owner" if owner else title
        text = f"WhatsApp with {title} — {who}: {body}"[:400]
        yield ParsedItem(
            event_type="life.chat.excerpt",
            text=text,
            occurred_at=item["when"],
            content={
                "kind": "whatsapp_excerpt",
                "title": title,
                "name": title,
                "owner": owner,
                "ref": title,
            },
            privacy_level="normal",
            item_key=f"whatsapp:excerpt:{title.lower()}:{kept}:{body[:40].lower()}",
        )


def _whatsapp_recent_excerpts(
    messages: list[dict[str, Any]], title: str, *, limit: int
) -> Iterator[ParsedItem]:
    """Keep the last speakable beats so 'last time' is not a random old sample."""

    pool = [
        item
        for item in messages
        if _whatsapp_excerptable(str(item["body"] or ""))
    ]
    for kept, item in enumerate(pool[-max(1, limit) :]):
        body = str(item["body"] or "").strip()
        who = "Owner" if item["owner"] else title
        text = f"WhatsApp with {title} — {who}: {body}"[:400]
        yield ParsedItem(
            event_type="life.chat.excerpt",
            text=text,
            occurred_at=item["when"],
            content={
                "kind": "whatsapp_excerpt",
                "title": title,
                "name": title,
                "owner": bool(item["owner"]),
                "recent": True,
                "ref": title,
            },
            privacy_level="normal",
            item_key=f"whatsapp:excerpt:recent:{title.lower()}:{kept}:{body[:40].lower()}",
        )


def _whatsapp_excerptable(body: str) -> bool:
    text = body.strip()
    if len(text) < 12 or len(text) > 180:
        return False
    if text.startswith("http"):
        return False
    lowered = text.lower()
    if lowered in {"ok", "okay", "haan", "haa", "hmm", "hmmm", "yes", "no", "yeah"}:
        return False
    return True


def _spread_pick(items: list[Any], limit: int) -> list[Any]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return items
    step = max(1, len(items) // limit)
    picked = [items[index] for index in range(0, len(items), step)][:limit]
    if items[-1] not in picked:
        picked[-1] = items[-1]
    return picked


def _whatsapp_owner_style(messages: list[dict[str, Any]]) -> str:
    bodies = [str(item["body"] or "") for item in messages if item.get("owner")]
    if not bodies:
        return "Owner rarely writes in this thread."
    avg = sum(len(body) for body in bodies) / len(bodies)
    if avg < 28:
        return "Owner is casual and brief with them."
    if avg < 90:
        return "Owner writes in short conversational messages with them."
    return "Owner writes longer messages with them."


def _whatsapp_group_title(title: str) -> bool:
    lowered = title.strip().lower()
    if any(marker in lowered for marker in ("baddies", "group", "gang", "family", "job")):
        return True
    words = [part for part in re.split(r"\s+", lowered) if part]
    return len(words) >= 4


def _whatsapp_relation(title: str) -> tuple[str, tuple[str, ...]]:
    key = title.strip().lower().lstrip(".")
    if key in _WA_FAMILY:
        return _WA_FAMILY[key]
    if key.startswith("prof") or "professor" in key:
        return "professional_contact", (title,)
    return "friend", (title,)


def _whatsapp_scripts(blob: str) -> str:
    gujarati = sum(1 for char in blob if "\u0a80" <= char <= "\u0aff")
    hindi = sum(1 for char in blob if "\u0900" <= char <= "\u097f")
    parts: list[str] = []
    if gujarati > 8:
        parts.append("Gujarati")
    if hindi > 8:
        parts.append("Hindi")
    if not parts:
        return "Mostly English"
    return "Mixes English with " + " and ".join(parts)


def _whatsapp_span(first: datetime | None, last: datetime | None) -> str:
    if first is None or last is None:
        return ""
    if first.year == last.year:
        return f" ({first.year})"
    return f" ({first.year}–{last.year})"


def _whatsapp_when(date_raw: str, time_raw: str, ampm: str) -> datetime | None:
    bits = date_raw.split("/")
    if len(bits) != 3:
        return None
    try:
        month, day, year = int(bits[0]), int(bits[1]), int(bits[2])
        if year < 100:
            year += 2000
        hour_s, minute_s, *rest = time_raw.split(":")
        hour, minute = int(hour_s), int(minute_s)
        second = int(rest[0]) if rest else 0
        if ampm.upper() == "PM" and hour < 12:
            hour += 12
        if ampm.upper() == "AM" and hour == 12:
            hour = 0
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:
        return None


def _is_owner_sender(sender: str) -> bool:
    lowered = sender.strip().lower()
    return any(token == lowered or lowered.startswith(token) for token in _OWNER_SENDERS)


def _parse_mbox(path: Path, rel: str) -> Iterator[ParsedItem]:
    parser = Parser(policy=email_policy)
    with path.open("rb") as handle:
        header_buf: list[bytes] = []
        in_headers = False
        index = 0
        for raw_line in handle:
            if raw_line.startswith(b"From "):
                if header_buf:
                    item = _mbox_headers(parser, header_buf, rel, index)
                    index += 1
                    if item is not None:
                        yield item
                header_buf = []
                in_headers = True
                continue
            if not in_headers:
                continue
            if raw_line in (b"\n", b"\r\n"):
                item = _mbox_headers(parser, header_buf, rel, index)
                index += 1
                header_buf = []
                in_headers = False
                if item is not None:
                    yield item
                continue
            if len(header_buf) < 80:
                header_buf.append(raw_line)
        if header_buf:
            item = _mbox_headers(parser, header_buf, rel, index)
            if item is not None:
                yield item


def _mbox_headers(parser: Parser, buf: list[bytes], rel: str, index: int) -> ParsedItem | None:
    try:
        message = parser.parsestr(b"".join(buf).decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - skip corrupt envelopes
        return None
    labels = str(message.get("X-Gmail-Labels") or "")
    labels_l = labels.lower()
    if "spam" in labels_l or "trash" in labels_l:
        return None
    subject = str(message.get("Subject") or "(no subject)").strip()[:200]
    date_raw = str(message.get("Date") or "")
    occurred = _email_date(date_raw)
    return ParsedItem(
        event_type="life.mail.envelope",
        text=f"Mail: {subject}.",
        occurred_at=occurred,
        content={"kind": "mail", "subject": subject, "ref": rel, "labels": labels[:200]},
        privacy_level="sensitive",
        item_key=f"mail:{rel}:{index}:{subject[:80]}",
    )


def _photo_sidecar(path: Path) -> dict[str, Any] | None:
    candidates = [Path(str(path) + ".json"), path.with_suffix(path.suffix + ".json")]
    for candidate in candidates:
        data = _read_json(candidate)
        if isinstance(data, dict):
            return data
    return None


def _ipynb_text(path: Path) -> str:
    data = _read_json(path)
    if not isinstance(data, dict):
        return ""
    chunks: list[str] = []
    for cell in data.get("cells") or []:
        if not isinstance(cell, dict):
            continue
        source = cell.get("source")
        if isinstance(source, list):
            chunks.append("".join(str(part) for part in source))
        elif isinstance(source, str):
            chunks.append(source)
    return "\n".join(chunks)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_json(path: Path) -> Any:
    raw = _read_text(path)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))


def _clean_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    cleaned = cleaned.replace("\\,", ",").replace("\\;", ";")
    return cleaned[:120]


def _usec(value: Any) -> datetime | None:
    try:
        usec = int(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(usec / 1_000_000, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _email_date(raw: str) -> datetime | None:
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0
