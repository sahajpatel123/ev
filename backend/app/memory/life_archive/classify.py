"""Path-only classification of a personal-data archive.

Disposition is decided from relative path, size, and suffix. Quarantined
files are never opened. Large media is never hashed here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

Disposition = Literal["ingest", "index", "skip", "quarantine"]
Origin = Literal["apple", "google", "other"]

DEFAULT_ARCHIVE_ROOT = Path.home() / "personal-data-for-training"

_SKIP_NAMES = frozenset({".ds_store", "archive_browser.html", ".localized"})
_SKIP_SUFFIXES = frozenset({".apk", ".bin", ".dmg", ".iso"})
_MEDIA_SUFFIXES = frozenset({
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".png",
    ".gif",
    ".dng",
    ".raw",
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
})
_NOTE_SUFFIXES = frozenset({".md", ".txt", ".json", ".html", ".htm", ".ipynb", ".csv"})
_DOC_SUFFIXES = frozenset({".pdf", ".docx", ".doc", ".rtf"})
_QUARANTINE_DIR_MARKERS = (
    "trustwallet",
    "google pay",
    "google wallet",
    "addresses and more",
)
_SKIP_DIR_MARKERS = (
    "recently deleted",
    "access log activity",
    "app install and push notification",
    "apple media services",
    "gemini apps",
    "my ad center",
    "android device configuration",
    "google product surveys",
    "workspace studio",
    "google shopping",
    "google store",
    "play movies",
    "voice match",
    "/node_modules/",
    "/venv/",
    "/.venv/",
    "/site-packages/",
    "/__pycache__/",
    "/.git/",
    "cc statement",
    "bank statement",
)
_SKIP_WALK_DIR_NAMES = frozenset(
    {
        "node_modules",
        "venv",
        ".venv",
        "__pycache__",
        ".git",
        "dist",
        "build",
        "pods",
        ".expo",
        "site-packages",
    }
)
SKIP_WALK_DIR_NAMES = _SKIP_WALK_DIR_NAMES
_SKIP_ACTIVITY_MARKERS = (
    "my activity/search",
    "my activity/youtube",
    "my activity/gmail",
    "my activity/maps",
    "my activity/discover",
    "my activity/image search",
    "my activity/video search",
    "my activity/help",
    "my activity/takeout",
    "my activity/developers",
    "my activity/ai mode",
    "my activity/google analytics",
    "my activity/google lens",
    "my activity/google pay",
    "my activity/google play",
    "my activity/shopping",
)
_GOV_ID_MARKERS = ("aadhaar", "aadhar", "passport", "pancard", "pan_card", "eaadhaar")


@dataclass(frozen=True)
class CatalogRecord:
    rel: str
    origin: Origin
    adapter: str
    disposition: Disposition
    reason: str
    size: int
    suffix: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_path(path: Path, *, root: Path) -> CatalogRecord:
    """Classify one file. Directories are not records."""
    rel = _rel(path, root)
    origin = _origin(rel)
    size = _size(path)
    suffix = path.suffix.lower()
    name = path.name.lower()
    rel_l = rel.lower().replace("\\", "/")

    if name in _SKIP_NAMES or name.startswith("._"):
        return _record(rel, origin, "junk", "skip", "os_junk", size, suffix)

    if _quarantine(rel_l, name, suffix):
        return _record(rel, origin, "secret", "quarantine", "secret_or_id", size, suffix)

    if _in_markers(rel_l, _SKIP_DIR_MARKERS) or _in_markers(rel_l, _SKIP_ACTIVITY_MARKERS):
        return _record(rel, origin, "noise", "skip", "unnecessary_tree", size, suffix)

    if suffix in _SKIP_SUFFIXES:
        return _record(rel, origin, "noise", "skip", "binary_installable", size, suffix)

    if suffix == ".zip" and (
        "icloud photos part" in name or "photos part" in name
    ):
        unpacked = path.parent / path.stem
        if unpacked.is_dir():
            return _record(rel, origin, "photos", "skip", "unpacked_archive", size, suffix)
        return _record(rel, origin, "photos_zip", "index", "photos_zip", size, suffix)

    if suffix == ".zip" and "whatsapp" in rel_l:
        return _record(rel, origin, "whatsapp", "ingest", "whatsapp_export", size, suffix)

    if suffix == ".zip" and size < 32:
        return _record(rel, origin, "junk", "skip", "empty_zip", size, suffix)

    if "/documents/id/" in f"/{rel_l}/":
        return _record(rel, origin, "secret", "quarantine", "id_folder", size, suffix)

    if "icloud contacts" in rel_l and suffix == ".vcf":
        return _record(rel, origin, "contacts", "ingest", "apple_vcard", size, suffix)
    if "/contacts/" in f"/{rel_l}/" and suffix == ".vcf":
        return _record(rel, origin, "contacts", "ingest", "google_vcard", size, suffix)

    if suffix == ".ics" and ("calendar" in rel_l or "reminder" in rel_l):
        return _record(rel, origin, "calendar", "ingest", "ics", size, suffix)

    if "/keep/" in f"/{rel_l}/" and suffix == ".json":
        return _record(rel, origin, "keep", "ingest", "keep_note", size, suffix)
    if "/keep/" in f"/{rel_l}/":
        return _record(rel, origin, "keep", "skip", "keep_attachment", size, suffix)

    if rel_l.endswith("/tasks/tasks.json") or (suffix == ".json" and "/tasks/" in f"/{rel_l}/"):
        return _record(rel, origin, "tasks", "ingest", "google_tasks", size, suffix)

    if "bookmarks" in rel_l and suffix in {".csv", ".html", ".json"}:
        if "recently deleted" in rel_l:
            return _record(rel, origin, "bookmarks", "skip", "deleted_bookmarks", size, suffix)
        return _record(rel, origin, "bookmarks", "ingest", "bookmarks", size, suffix)

    if "/notebooklm/" in f"/{rel_l}/" and name.endswith("metadata.json"):
        return _record(rel, origin, "notebooklm", "ingest", "notebook_meta", size, suffix)
    if "/notebooklm/" in f"/{rel_l}/" and suffix in {".html", ".htm", ".md", ".txt", ".pdf"}:
        return _record(rel, origin, "notebooklm", "ingest", "notebook_source", size, suffix)

    if "/saved/" in f"/{rel_l}/" and suffix == ".csv":
        return _record(rel, origin, "bookmarks", "ingest", "saved_list", size, suffix)

    if "/chrome/" in f"/{rel_l}/":
        if name in {"bookmarks.html", "reading list.html"}:
            return _record(rel, origin, "bookmarks", "ingest", "chrome_bookmarks", size, suffix)
        return _record(rel, origin, "chrome", "skip", "chrome_noise", size, suffix)

    if "/mail/" in f"/{rel_l}/" and suffix == ".mbox":
        return _record(rel, origin, "mail", "index", "mail_envelopes", size, suffix)

    if _is_photo_sidecar(path, rel_l, suffix):
        return _record(rel, origin, "photos", "skip", "photo_sidecar", size, suffix)
    if (
        suffix == ".csv"
        and "icloud photos" in rel_l
        and name.startswith("photo details")
    ):
        return _record(rel, origin, "photos_meta", "index", "photo_details_csv", size, suffix)
    if suffix in {".csv", ".json"} and (
        "/albums/" in f"/{rel_l}/" or "/memories/" in f"/{rel_l}/" or "icloud photos" in rel_l
    ):
        return _record(rel, origin, "photos", "skip", "photo_list", size, suffix)

    if ("photos" in rel_l or "icloud photos" in rel_l) and suffix in _MEDIA_SUFFIXES:
        if "icloud photos" in rel_l and _photos_csv_covers(path.parent):
            return _record(rel, origin, "photos", "skip", "csv_covers_photos", size, suffix)
        return _record(rel, origin, "photos", "index", "media_pointer", size, suffix)

    if "/drive/" in f"/{rel_l}/":
        if suffix == ".csv" or name.startswith("drive details"):
            return _record(rel, origin, "drive", "skip", "drive_index_csv", size, suffix)
        if suffix in _NOTE_SUFFIXES or suffix in _DOC_SUFFIXES:
            return _record(rel, origin, "drive", "ingest", "drive_doc", size, suffix)
        if suffix in _MEDIA_SUFFIXES:
            return _record(rel, origin, "drive", "index", "drive_media_pointer", size, suffix)
        return _record(rel, origin, "drive", "skip", "drive_other", size, suffix)

    if "/fit/" in f"/{rel_l}/":
        return _record(rel, origin, "health", "skip", "health_default_off", size, suffix)

    if suffix in {".tcx", ".gpx", ".fit"}:
        return _record(rel, origin, "health", "skip", "health_default_off", size, suffix)

    if "/youtube" in rel_l and name == "watch-history.html":
        return _record(rel, origin, "youtube", "skip", "watch_history_deferred", size, suffix)

    if suffix in _MEDIA_SUFFIXES:
        return _record(rel, origin, "media", "skip", "loose_media", size, suffix)

    return _record(rel, origin, "noise", "skip", "takeout_noise", size, suffix)


def _record(
    rel: str,
    origin: Origin,
    adapter: str,
    disposition: Disposition,
    reason: str,
    size: int,
    suffix: str,
) -> CatalogRecord:
    return CatalogRecord(
        rel=rel,
        origin=origin,
        adapter=adapter,
        disposition=disposition,
        reason=reason,
        size=size,
        suffix=suffix,
    )


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _origin(rel: str) -> Origin:
    first = rel.split("/", 1)[0].lower()
    if first == "apple":
        return "apple"
    if first == "google":
        return "google"
    return "other"


def _size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _in_markers(rel_l: str, markers: tuple[str, ...]) -> bool:
    return any(marker in rel_l for marker in markers)


def _quarantine(rel_l: str, name: str, suffix: str) -> bool:
    if "trustwallet" in rel_l:
        return True
    if _in_markers(rel_l, _QUARANTINE_DIR_MARKERS):
        return True
    collapsed = name.replace(" ", "").replace("-", "").replace("_", "")
    return suffix in {".pdf", ".zip"} and any(marker in collapsed for marker in _GOV_ID_MARKERS)


_CSV_COVER: dict[str, bool] = {}


def _photos_csv_covers(directory: Path) -> bool:
    """True when Apple already gave us a dated Photo Details.csv for this folder."""
    key = str(directory)
    cached = _CSV_COVER.get(key)
    if cached is not None:
        return cached
    try:
        covered = any(directory.glob("Photo Details*.csv"))
    except OSError:
        covered = False
    _CSV_COVER[key] = covered
    return covered


def _is_photo_sidecar(path: Path, rel_l: str, suffix: str) -> bool:
    if suffix != ".json":
        return False
    if "google photos" in rel_l or "icloud photos" in rel_l or "/photos/" in f"/{rel_l}/":
        return True
    stem = path.name[: -len(".json")]
    parent = path.parent
    if (parent / stem).is_file():
        return True
    return any((parent / stem).with_suffix(ext).is_file() for ext in _MEDIA_SUFFIXES)
