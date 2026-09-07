"""Continuous living sensory daemon for EV (Evie).

The Mac is the continuity hub. iCloud + SMS/call forwarding already copy
owner life onto this machine. This follower reads those local copies:

- iMessage / SMS: ``~/Library/Messages/chat.db``
- WhatsApp Desktop: ``ChatStorage.sqlite`` (plaintext ZTEXT on this Mac)
- Call History: ``CallHistory.storedata`` (Continuity / iPhone forwarding)
- Apple Contacts / Mail envelopes / Google Calendar / health snapshots
- Photos library filenames (no pixels)

Rows are stored as events and opened only when recall chooses that shelf.
They are never written into live_context. Writes go through the helper
(Messages/Mail AppleScript, WhatsApp URL compose, call.place). Direct
edits of Apple/WhatsApp sqlite files are never used — they would not
sync and can corrupt the apps.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import utcnow

logger = logging.getLogger("ev.life_stream_daemon")

# Apple Cocoa reference date is 2001-01-01 00:00:00 UTC
APPLE_EPOCH = datetime(2001, 1, 1, 0, 0, 0, tzinfo=UTC)
_MAX_CONTACT_FPS = 5000
_ACCOUNT_PULL_SECONDS = 300
_LIVE_CATCHUP = 200
_HEALTH_METRIC_KEYS = (
    "sleep_hours",
    "hrv",
    "hrv_ms",
    "heart_rate",
    "resting_hr",
    "steps",
)


def life_stream_should_run() -> bool:
    """True when the owner opted into the headless follower.

    CI stays off (default provider=local, empty helper). A real Mac with
    ``EV_MESSAGING_PROVIDER=macos_life`` and a helper path auto-enables.
    """
    from app.config import settings

    if bool(getattr(settings, "life_stream_enabled", False)):
        return True
    provider = (getattr(settings, "messaging_provider", "local") or "local").strip().lower()
    helper = (getattr(settings, "life_helper_path", "") or "").strip()
    return provider == "macos_life" and bool(helper)


def _contact_fingerprint(contact: dict) -> str:
    phone = contact.get("phone") or ""
    if not phone and contact.get("phone_numbers"):
        numbers = contact.get("phone_numbers") or []
        phone = numbers[0] if numbers else ""
    email = contact.get("email") or ""
    if not email and contact.get("email_addresses"):
        addresses = contact.get("email_addresses") or []
        email = addresses[0] if addresses else ""
    return "|".join(
        [
            str(contact.get("id") or ""),
            str(contact.get("name") or contact.get("full_name") or ""),
            str(phone),
            str(email),
        ]
    )


def _apple_timestamp_to_datetime(raw_value: int | float | None) -> datetime:
    """Convert Apple CoreData/Cocoa timestamp to timezone-aware UTC datetime."""
    if raw_value is None or raw_value == 0:
        return utcnow()
    raw = float(raw_value)
    # Modern macOS chat.db stores nanoseconds since 2001; older versions stored seconds
    seconds = raw / 1_000_000_000.0 if abs(raw) > 1_000_000_000_000.0 else raw
    try:
        return APPLE_EPOCH + timedelta(seconds=seconds)
    except (OverflowError, OSError, ValueError):
        return utcnow()


def _mail_received_iso(raw_value: Any) -> str | None:
    if isinstance(raw_value, str) and raw_value.strip():
        return raw_value.strip()
    if not isinstance(raw_value, (int, float)):
        return None
    value = float(raw_value)
    try:
        if abs(value) > 1_000_000_000:
            occurred = datetime.fromtimestamp(value, tz=UTC)
        else:
            occurred = _apple_timestamp_to_datetime(value)
    except (OverflowError, OSError, ValueError):
        return None
    return occurred.isoformat()


def _sqlite_readable(path: str | None) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.R_OK)


def find_mail_envelope_index() -> str:
    """Newest Apple Mail Envelope Index on this Mac, or empty."""
    root = os.path.expanduser("~/Library/Mail")
    if not os.path.isdir(root):
        return ""
    versions: list[tuple[int, str]] = []
    try:
        names = os.listdir(root)
    except OSError:
        return ""
    for name in names:
        if not name.startswith("V"):
            continue
        try:
            versions.append((int(name[1:]), name))
        except ValueError:
            continue
    versions.sort(reverse=True)
    for _, name in versions:
        path = os.path.join(root, name, "MailData", "Envelope Index")
        if _sqlite_readable(path):
            return path
    return ""


def _sqlite_query(path: str, sql: str, params: tuple = ()) -> list[tuple]:
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        conn.close()


def _sqlite_scalar(path: str, sql: str, params: tuple = ()) -> Any:
    rows = _sqlite_query(path, sql, params)
    if not rows:
        return None
    return rows[0][0]


def _filter_peek(rows: list[dict[str, Any]], tokens: list[str], limit: int) -> list[dict[str, Any]]:
    cap = max(1, min(int(limit or 8), 8))
    if not tokens:
        return rows[:cap]
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        blob = " ".join(
            str(row.get(key) or "")
            for key in ("text", "gist", "subject", "sender")
        ).lower()
        hits = sum(1 for token in tokens if token and token in blob)
        if hits <= 0:
            continue
        item = dict(row)
        item["score"] = round(hits / len(tokens), 4)
        scored.append((hits / len(tokens), item))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in scored[:cap]]


def health_snapshot_event_create(row: Any) -> EventCreate | None:
    """Compact health envelope. Metrics only; never dumped into chat."""
    if isinstance(row, dict):
        sid = str(row.get("id") or row.get("occurred_at") or "")
        readiness = row.get("readiness")
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        occurred = row.get("occurred_at")
    else:
        sid = str(getattr(row, "id", "") or "")
        readiness = getattr(row, "readiness", None)
        raw_metrics = getattr(row, "metrics", None)
        metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
        occurred = getattr(row, "occurred_at", None)
    if not sid:
        return None
    bits = []
    if readiness is not None:
        bits.append(f"readiness {readiness}")
    compact: dict[str, Any] = {}
    for key in _HEALTH_METRIC_KEYS:
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            compact[key] = value
            bits.append(f"{key} {value}")
    text = "Health: " + ", ".join(bits) if bits else "Health snapshot"
    return EventCreate(
        event_type="health.snapshot.recorded",
        source="health",
        content={"text": text, "readiness": readiness, "metrics": compact},
        occurred_at=occurred if occurred else None,
        privacy_level="sensitive",
        metadata={"source": "health_snapshot", "channel": "health", "snapshot_id": sid},
    )


class LifeStreamDaemon:
    """Headless sensory daemon for continuous background data observation."""

    def __init__(
        self,
        *,
        chat_db_path: str | None = None,
        last_message_rowid: int = 0,
        whatsapp_db_path: str | None = None,
        call_db_path: str | None = None,
        photos_db_path: str | None = None,
        mail_index_path: str | None = None,
    ) -> None:
        default_chat = os.path.expanduser("~/Library/Messages/chat.db")
        default_whatsapp = os.path.expanduser(
            "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
        )
        default_calls = os.path.expanduser(
            "~/Library/Application Support/CallHistoryDB/CallHistory.storedata"
        )
        default_photos = os.path.expanduser(
            "~/Pictures/Photos Library.photoslibrary/database/Photos.sqlite"
        )
        self.chat_db_path = default_chat if chat_db_path is None else chat_db_path
        # Isolated tests pass a temp chat.db. Do not also open the owner's
        # WhatsApp / CallHistory / Photos libraries unless those paths are set.
        use_real_hub = chat_db_path is None
        self.whatsapp_db_path = (
            default_whatsapp
            if whatsapp_db_path is None and use_real_hub
            else (whatsapp_db_path or "")
        )
        self.call_db_path = (
            default_calls
            if call_db_path is None and use_real_hub
            else (call_db_path or "")
        )
        self.photos_db_path = (
            default_photos
            if photos_db_path is None and use_real_hub
            else (photos_db_path or "")
        )
        self.mail_index_path = (
            find_mail_envelope_index()
            if mail_index_path is None and use_real_hub
            else (mail_index_path or "")
        )
        self.last_message_rowid = last_message_rowid
        self.last_whatsapp_pk = 0
        self.last_call_pk = 0
        self.last_photo_pk = 0
        self._known_contact_ids: set[str] = set()
        self._contact_fps: dict[str, str] = {}
        self._mail_fps: dict[str, str] = {}
        self._calendar_fps: dict[str, str] = {}
        self._health_fps: dict[str, str] = {}
        self._last_account_pull: float = 0.0
        self._cursor_path: Path | None = None
        self._bootstrapped: set[str] = set()
        self._cached_contacts: list[dict[str, Any]] = []

    def remember_contacts(self, contacts: list[dict[str, Any]]) -> None:
        """Keep a short ask-time copy of the last helper address-book pull."""
        self._cached_contacts = [row for row in contacts if isinstance(row, dict)][:500]

    def is_chat_db_accessible(self) -> bool:
        """Return True if chat.db exists and is readable (Full Disk Access granted)."""
        return _sqlite_readable(self.chat_db_path)

    def _catchup_pk(self, attr: str, path: str, table: str, column: str = "Z_PK") -> int:
        """On first sight of a large DB, start near the live tail instead of year one."""
        current = int(getattr(self, attr) or 0)
        if current > 0 or attr in self._bootstrapped:
            return current
        self._bootstrapped.add(attr)
        if not _sqlite_readable(path):
            return 0
        try:
            max_pk = _sqlite_scalar(path, f"SELECT MAX({column}) FROM {table}")
        except Exception:  # noqa: BLE001 - missing table is a skip, not a crash
            return 0
        if not isinstance(max_pk, (int, float)) or max_pk is None:
            return 0
        start = max(0, int(max_pk) - _LIVE_CATCHUP)
        setattr(self, attr, start)
        return start

    async def _safe_sync(self, name: str, coro) -> list[Any]:
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - one stream must not skip the others
            logger.warning("life stream %s skipped: %s", name, type(exc).__name__)
            return []

    async def sync_imessage(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
    ) -> list[Any]:
        """Poll chat.db for messages newer than last_message_rowid and ingest them."""
        if not self.is_chat_db_accessible():
            logger.debug(
                "chat.db not accessible at '%s' (degraded or FDA missing)",
                self.chat_db_path,
            )
            return []

        events = []
        event_service = EventService(session, actor="life_stream_daemon")

        try:
            # Connect in read-only URI mode with a short timeout to prevent SQLite lock contention
            uri = f"file:{os.path.abspath(self.chat_db_path)}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            try:
                cursor = conn.cursor()
                query = """
                    SELECT message.ROWID, message.date, message.text, handle.id, message.is_from_me
                    FROM message
                    LEFT JOIN handle ON message.handle_id = handle.ROWID
                    WHERE message.ROWID > ?
                    ORDER BY message.ROWID ASC
                    LIMIT ?
                """
                cursor.execute(query, (self.last_message_rowid, max(1, limit)))
                rows = cursor.fetchall()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("Failed reading chat.db: %s", exc)
            return []

        for rowid, raw_date, raw_text, handle, is_from_me in rows:
            text = (raw_text or "").strip()
            if not text:
                if rowid > self.last_message_rowid:
                    self.last_message_rowid = rowid
                continue

            occurred_at = _apple_timestamp_to_datetime(raw_date)
            sender_or_target = str(handle or "unknown").strip()
            from_me = bool(is_from_me)

            event_type = (
                "message.imessage.sent" if from_me else "message.imessage.received"
            )
            event_create = EventCreate(
                event_type=event_type,
                source="imessage",
                content={
                    "text": text,
                    "handle": sender_or_target,
                    "is_from_me": from_me,
                    "rowid": rowid,
                },
                occurred_at=occurred_at,
                privacy_level="sensitive",
                metadata={"source_rowid": rowid, "channel": "messages"},
            )

            created_event = await event_service.create(event_create)
            events.append(created_event)

            if rowid > self.last_message_rowid:
                self.last_message_rowid = rowid

        if events:
            await session.commit()
            logger.info("Ingested %d new iMessage events (latest rowid: %d)", len(events), self.last_message_rowid)

        return events

    async def sync_whatsapp(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
    ) -> list[Any]:
        """Poll WhatsApp Desktop ChatStorage.sqlite. Read-only; never write that file."""
        path = self.whatsapp_db_path
        if not _sqlite_readable(path):
            return []
        self._catchup_pk("last_whatsapp_pk", path, "ZWAMESSAGE")
        try:
            rows = _sqlite_query(
                path,
                """
                SELECT m.Z_PK, m.ZMESSAGEDATE, m.ZTEXT, m.ZISFROMME, m.ZFROMJID, m.ZTOJID,
                       m.ZPUSHNAME, s.ZPARTNERNAME, s.ZCONTACTJID
                FROM ZWAMESSAGE m
                LEFT JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
                WHERE m.Z_PK > ?
                  AND m.ZTEXT IS NOT NULL
                  AND trim(m.ZTEXT) != ''
                ORDER BY m.Z_PK ASC
                LIMIT ?
                """,
                (self.last_whatsapp_pk, max(1, limit)),
            )
        except Exception as exc:
            logger.warning("Failed reading WhatsApp ChatStorage: %s", type(exc).__name__)
            return []

        events = []
        event_service = EventService(session, actor="life_stream_daemon")
        for (
            pk,
            raw_date,
            raw_text,
            is_from_me,
            from_jid,
            to_jid,
            push_name,
            partner_name,
            contact_jid,
        ) in rows:
            text = str(raw_text or "").strip()
            pk_i = int(pk or 0)
            if pk_i > self.last_whatsapp_pk:
                self.last_whatsapp_pk = pk_i
            if not text:
                continue
            from_me = bool(is_from_me)
            who = str(partner_name or push_name or from_jid or to_jid or contact_jid or "someone").strip()
            event_create = EventCreate(
                event_type="message.whatsapp.sent" if from_me else "message.whatsapp.received",
                source="whatsapp",
                content={
                    "text": text,
                    "handle": who,
                    "is_from_me": from_me,
                    "pk": pk_i,
                },
                occurred_at=_apple_timestamp_to_datetime(raw_date),
                privacy_level="sensitive",
                metadata={"source_pk": pk_i, "channel": "whatsapp"},
            )
            events.append(await event_service.create(event_create))

        if events:
            await session.commit()
            logger.info("Ingested %d WhatsApp events (latest pk: %d)", len(events), self.last_whatsapp_pk)
        return events

    async def sync_calls(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
    ) -> list[Any]:
        """Poll CallHistory.storedata copied here by Continuity / iPhone forwarding."""
        path = self.call_db_path
        if not _sqlite_readable(path):
            return []
        self._catchup_pk("last_call_pk", path, "ZCALLRECORD")
        try:
            rows = _sqlite_query(
                path,
                """
                SELECT Z_PK, ZDATE, ZDURATION, ZADDRESS, ZNAME, ZORIGINATED, ZANSWERED, ZCALLTYPE
                FROM ZCALLRECORD
                WHERE Z_PK > ?
                ORDER BY Z_PK ASC
                LIMIT ?
                """,
                (self.last_call_pk, max(1, limit)),
            )
        except Exception as exc:
            logger.warning("Failed reading CallHistory: %s", type(exc).__name__)
            return []

        events = []
        event_service = EventService(session, actor="life_stream_daemon")
        for pk, raw_date, duration, address, name, originated, answered, call_type in rows:
            pk_i = int(pk or 0)
            if pk_i > self.last_call_pk:
                self.last_call_pk = pk_i
            outgoing = bool(originated)
            picked_up = bool(answered)
            direction = "outgoing" if outgoing else "incoming"
            status = "answered" if picked_up else "missed"
            who = str(name or address or "unknown").strip()
            seconds = int(duration or 0) if isinstance(duration, (int, float)) else 0
            text = f"{status} {direction} call {who}"
            if seconds > 0:
                text = f"{text} ({seconds}s)"
            event_create = EventCreate(
                event_type="call.history.recorded",
                source="calls",
                content={
                    "text": text,
                    "name": str(name or "").strip(),
                    "address": str(address or "").strip(),
                    "duration": seconds,
                    "originated": outgoing,
                    "answered": picked_up,
                    "call_type": call_type,
                    "pk": pk_i,
                },
                occurred_at=_apple_timestamp_to_datetime(raw_date),
                privacy_level="sensitive",
                metadata={"source_pk": pk_i, "channel": "calls"},
            )
            events.append(await event_service.create(event_create))

        if events:
            await session.commit()
            logger.info("Ingested %d call-history events (latest pk: %d)", len(events), self.last_call_pk)
        return events

    async def sync_photos(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
    ) -> list[Any]:
        """Index Photos library filenames. Never pixels, never directory paths."""
        path = self.photos_db_path
        if not _sqlite_readable(path):
            return []
        self._catchup_pk("last_photo_pk", path, "ZASSET")
        try:
            rows = _sqlite_query(
                path,
                """
                SELECT Z_PK, ZFILENAME, ZDATECREATED
                FROM ZASSET
                WHERE Z_PK > ?
                  AND ZFILENAME IS NOT NULL
                  AND trim(ZFILENAME) != ''
                  AND IFNULL(ZTRASHEDSTATE, 0) = 0
                ORDER BY Z_PK ASC
                LIMIT ?
                """,
                (self.last_photo_pk, max(1, limit)),
            )
        except Exception as exc:
            logger.warning("Failed reading Photos library: %s", type(exc).__name__)
            return []

        events = []
        event_service = EventService(session, actor="life_stream_daemon")
        for pk, filename, raw_date in rows:
            pk_i = int(pk or 0)
            if pk_i > self.last_photo_pk:
                self.last_photo_pk = pk_i
            name = str(filename or "").strip()
            if not name:
                continue
            event_create = EventCreate(
                event_type="photo.library.indexed",
                source="photos",
                content={"text": name, "filename": name, "pk": pk_i},
                occurred_at=_apple_timestamp_to_datetime(raw_date),
                privacy_level="sensitive",
                metadata={"source_pk": pk_i, "channel": "photos"},
            )
            events.append(await event_service.create(event_create))

        if events:
            await session.commit()
            logger.info("Ingested %d photo filename events (latest pk: %d)", len(events), self.last_photo_pk)
        return events

    def peek_whatsapp(self, *, tokens: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """Read the live WhatsApp Desktop DB. No Event writes."""
        path = self.whatsapp_db_path
        if not _sqlite_readable(path):
            return []
        try:
            rows = _sqlite_query(
                path,
                """
                SELECT m.Z_PK, m.ZMESSAGEDATE, m.ZTEXT, m.ZISFROMME,
                       m.ZPUSHNAME, s.ZPARTNERNAME, m.ZFROMJID, m.ZTOJID
                FROM ZWAMESSAGE m
                LEFT JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
                WHERE m.ZTEXT IS NOT NULL AND trim(m.ZTEXT) != ''
                ORDER BY m.Z_PK DESC
                LIMIT 48
                """,
            )
        except Exception:
            logger.warning("WhatsApp live peek skipped", exc_info=False)
            return []
        hits: list[dict[str, Any]] = []
        for pk, raw_date, raw_text, is_from_me, push_name, partner_name, from_jid, to_jid in rows:
            body = str(raw_text or "").strip()
            if not body:
                continue
            who = "You" if is_from_me else str(partner_name or push_name or from_jid or to_jid or "someone").strip()
            occurred = _apple_timestamp_to_datetime(raw_date)
            hits.append(
                {
                    "id": f"wa-live-{int(pk or 0)}",
                    "source": "whatsapp",
                    "when": occurred.isoformat(),
                    "text": f"{who}: {body}"[:400],
                    "handle": str(partner_name or push_name or from_jid or to_jid or "").strip(),
                    "preview": body[:220],
                    "kind": "live_mac",
                    "memory_type": "message.whatsapp.sent" if is_from_me else "message.whatsapp.received",
                    "confidence": "live_mac",
                    "score": 0.15,
                    "provenance": [f"wa-live-{int(pk or 0)}"],
                    "shelf": "chats",
                    "channel": "whatsapp",
                }
            )
        return _filter_peek(hits, [token for token in (tokens or []) if token], limit)

    def read_whatsapp_person(self, token: str, *, limit: int = 800) -> list[dict[str, Any]]:
        """Ask-only: live WhatsApp lines whose partner name is that person."""

        compact = "".join(ch for ch in (token or "").lower() if ch.isalpha())
        if len(compact) < 2:
            return []
        path = self.whatsapp_db_path
        if not _sqlite_readable(path):
            return []
        cap = max(1, min(int(limit or 800), 2000))
        try:
            rows = _sqlite_query(
                path,
                """
                SELECT m.ZMESSAGEDATE, m.ZTEXT, m.ZISFROMME,
                       m.ZPUSHNAME, s.ZPARTNERNAME, m.ZFROMJID, m.ZTOJID
                FROM ZWAMESSAGE m
                LEFT JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
                WHERE m.ZTEXT IS NOT NULL AND trim(m.ZTEXT) != ''
                  AND lower(
                    ifnull(s.ZPARTNERNAME, '') || ' ' || ifnull(m.ZPUSHNAME, '')
                  ) LIKE ?
                ORDER BY m.ZMESSAGEDATE DESC
                LIMIT ?
                """,
                (f"%{compact}%", cap),
            )
        except Exception:
            logger.warning("WhatsApp person read skipped", exc_info=False)
            return []
        out: list[dict[str, Any]] = []
        for raw_date, raw_text, is_from_me, push_name, partner_name, from_jid, to_jid in rows:
            body = str(raw_text or "").strip()
            who = str(partner_name or push_name or from_jid or to_jid or "").strip()
            compact_who = "".join(ch for ch in who.lower() if ch.isalpha())
            if compact not in compact_who:
                continue
            if not body:
                continue
            out.append(
                {
                    "sender": who or token,
                    "body": body,
                    "owner": bool(is_from_me),
                    "when": _apple_timestamp_to_datetime(raw_date),
                }
            )
        return out

    def read_whatsapp_desk_tails(self, *, limit: int = 400) -> list[dict[str, Any]]:
        """Recent live WhatsApp lines grouped later by partner. Ask-only."""

        path = self.whatsapp_db_path
        if not _sqlite_readable(path):
            return []
        cap = max(40, min(int(limit or 400), 800))
        try:
            rows = _sqlite_query(
                path,
                """
                SELECT m.ZMESSAGEDATE, m.ZTEXT, m.ZISFROMME,
                       m.ZPUSHNAME, s.ZPARTNERNAME, m.ZFROMJID, m.ZTOJID
                FROM ZWAMESSAGE m
                LEFT JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
                WHERE m.ZTEXT IS NOT NULL AND trim(m.ZTEXT) != ''
                ORDER BY m.ZMESSAGEDATE DESC
                LIMIT ?
                """,
                (cap,),
            )
        except Exception:
            logger.warning("WhatsApp desk tails skipped", exc_info=False)
            return []
        out: list[dict[str, Any]] = []
        for raw_date, raw_text, is_from_me, push_name, partner_name, from_jid, to_jid in rows:
            body = str(raw_text or "").strip()
            partner = str(partner_name or push_name or from_jid or to_jid or "").strip()
            if not body or not partner:
                continue
            out.append(
                {
                    "sender": partner,
                    "partner": partner,
                    "body": body,
                    "owner": bool(is_from_me),
                    "when": _apple_timestamp_to_datetime(raw_date),
                }
            )
        return out

    def peek_imessage(self, *, tokens: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """Read live iMessage/SMS from chat.db. No Event writes."""
        if not self.is_chat_db_accessible():
            return []
        try:
            rows = _sqlite_query(
                self.chat_db_path,
                """
                SELECT message.ROWID, message.date, message.text, handle.id, message.is_from_me
                FROM message
                LEFT JOIN handle ON message.handle_id = handle.ROWID
                WHERE message.text IS NOT NULL AND trim(message.text) != ''
                ORDER BY message.ROWID DESC
                LIMIT 48
                """,
            )
        except Exception:
            logger.warning("iMessage live peek skipped", exc_info=False)
            return []
        hits: list[dict[str, Any]] = []
        for rowid, raw_date, raw_text, handle, is_from_me in rows:
            body = str(raw_text or "").strip()
            if not body:
                continue
            who = "You" if is_from_me else str(handle or "someone").strip()
            occurred = _apple_timestamp_to_datetime(raw_date)
            hits.append(
                {
                    "id": f"imessage-live-{int(rowid or 0)}",
                    "source": "imessage",
                    "when": occurred.isoformat(),
                    "text": f"{who}: {body}"[:400],
                    "handle": str(handle or "").strip(),
                    "preview": body[:220],
                    "kind": "live_mac",
                    "memory_type": "message.imessage.sent" if is_from_me else "message.imessage.received",
                    "confidence": "live_mac",
                    "score": 0.15,
                    "provenance": [f"imessage-live-{int(rowid or 0)}"],
                    "shelf": "chats",
                    "channel": "imessage",
                }
            )
        return _filter_peek(hits, [token for token in (tokens or []) if token], limit)

    def peek_calls(self, *, tokens: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """Read live Continuity call history. No Event writes."""
        path = self.call_db_path
        if not _sqlite_readable(path):
            return []
        try:
            rows = _sqlite_query(
                path,
                """
                SELECT Z_PK, ZDATE, ZDURATION, ZADDRESS, ZNAME, ZORIGINATED, ZANSWERED
                FROM ZCALLRECORD
                ORDER BY Z_PK DESC
                LIMIT 48
                """,
            )
        except Exception:
            logger.warning("Call history live peek skipped", exc_info=False)
            return []
        hits: list[dict[str, Any]] = []
        for pk, raw_date, duration, address, name, originated, answered in rows:
            who = str(name or address or "unknown").strip()
            direction = "outgoing" if originated else "incoming"
            status = "answered" if answered else "missed"
            seconds = int(duration or 0) if isinstance(duration, (int, float)) else 0
            text = f"{status} {direction} call {who}"
            if seconds > 0:
                text = f"{text} ({seconds}s)"
            occurred = _apple_timestamp_to_datetime(raw_date)
            hits.append(
                {
                    "id": f"call-live-{int(pk or 0)}",
                    "source": "calls",
                    "when": occurred.isoformat(),
                    "text": text[:400],
                    "kind": "live_mac",
                    "memory_type": "call.history.recorded",
                    "confidence": "live_mac",
                    "score": 0.15,
                    "provenance": [f"call-live-{int(pk or 0)}"],
                    "shelf": "calls",
                }
            )
        return _filter_peek(hits, [token for token in (tokens or []) if token], limit)

    def peek_photos(self, *, tokens: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """Read live Photos filenames. No pixels, no Event writes."""
        path = self.photos_db_path
        if not _sqlite_readable(path):
            return []
        try:
            rows = _sqlite_query(
                path,
                """
                SELECT Z_PK, ZFILENAME, ZDATECREATED
                FROM ZASSET
                WHERE ZFILENAME IS NOT NULL AND trim(ZFILENAME) != ''
                  AND IFNULL(ZTRASHEDSTATE, 0) = 0
                ORDER BY Z_PK DESC
                LIMIT 48
                """,
            )
        except Exception:
            logger.warning("Photos live peek skipped", exc_info=False)
            return []
        hits: list[dict[str, Any]] = []
        for pk, filename, raw_date in rows:
            name = str(filename or "").strip()
            if not name:
                continue
            occurred = _apple_timestamp_to_datetime(raw_date)
            hits.append(
                {
                    "id": f"photo-live-{int(pk or 0)}",
                    "source": "photos",
                    "when": occurred.isoformat(),
                    "text": name[:400],
                    "kind": "live_mac",
                    "memory_type": "photo.library.indexed",
                    "confidence": "live_mac",
                    "score": 0.15,
                    "provenance": [f"photo-live-{int(pk or 0)}"],
                    "shelf": "photos",
                }
            )
        return _filter_peek(hits, [token for token in (tokens or []) if token], limit)

    def peek_contacts(
        self,
        contacts: list[dict[str, Any]] | None = None,
        *,
        tokens: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Format live Apple Contacts rows. No Event writes."""
        hits: list[dict[str, Any]] = []
        for index, contact in enumerate(contacts or []):
            if not isinstance(contact, dict):
                continue
            name = str(
                contact.get("full_name") or contact.get("name") or ""
            ).strip()
            numbers = contact.get("phone_numbers") or []
            emails = contact.get("email_addresses") or []
            phone = str(contact.get("phone") or (numbers[0] if numbers else "") or "").strip()
            email = str(contact.get("email") or (emails[0] if emails else "") or "").strip()
            if not name and not phone and not email:
                continue
            text = name or phone or email
            if name and phone:
                text = f"{name} {phone}"
            elif name and email:
                text = f"{name} {email}"
            cid = str(contact.get("id") or name or index)
            hits.append(
                {
                    "id": f"contact-live-{cid}",
                    "source": "contacts",
                    "when": None,
                    "text": text[:400],
                    "kind": "live_mac",
                    "memory_type": "contact.discovered",
                    "confidence": "live_mac",
                    "score": 0.15,
                    "provenance": [f"contact-live-{cid}"],
                    "shelf": "contacts",
                    "channel": "contacts",
                }
            )
        return _filter_peek(hits, [token for token in (tokens or []) if token], limit)

        return _filter_peek(hits, [token for token in (tokens or []) if token], limit)

    def _mail_index_rows(self, path: str, *, inbox_only: bool) -> list[dict[str, Any]]:
        """Read Envelope Index without launching Mail.app."""
        where = "m.deleted = 0"
        if inbox_only:
            where += " AND (mb.url LIKE '%/INBOX' OR mb.url LIKE '%/INBOX/')"
        rich_sql = f"""
                SELECT replace(replace(COALESCE(sub.subject, ''), char(9), ' '), char(10), ' '),
                       CASE WHEN a.comment != '' THEN a.comment || ' <' || a.address || '>'
                            ELSE COALESCE(a.address, '') END,
                       m.date_received,
                       m.ROWID,
                       COALESCE(snip.summary, ''),
                       g.summary
                FROM messages m
                LEFT JOIN subjects sub ON m.subject = sub.ROWID
                LEFT JOIN addresses a ON m.sender = a.ROWID
                LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
                LEFT JOIN summaries snip ON m.summary = snip.ROWID
                LEFT JOIN generated_summaries g ON m.summary = g.ROWID
                WHERE {where}
                ORDER BY m.date_received DESC
                LIMIT 48
                """
        simple_sql = f"""
                SELECT replace(replace(COALESCE(s.subject, ''), char(9), ' '), char(10), ' '),
                       CASE WHEN a.comment != '' THEN a.comment || ' <' || a.address || '>'
                            ELSE COALESCE(a.address, '') END,
                       m.date_received,
                       m.ROWID,
                       '',
                       NULL
                FROM messages m
                LEFT JOIN subjects s ON m.subject = s.ROWID
                LEFT JOIN addresses a ON m.sender = a.ROWID
                LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
                WHERE {where}
                ORDER BY m.date_received DESC
                LIMIT 48
                """
        fetched: list[tuple] = []
        try:
            fetched = _sqlite_query(path, rich_sql)
        except Exception:
            try:
                fetched = _sqlite_query(path, simple_sql)
            except Exception:
                logger.warning("Mail live peek skipped", exc_info=False)
                return []
        rows: list[dict[str, Any]] = []
        for subject, sender, raw_date, rowid, snippet, generated in fetched:
            rows.append(
                {
                    "subject": str(subject or "").strip(),
                    "sender": str(sender or "").strip(),
                    "received": _mail_received_iso(raw_date),
                    "rowid": int(rowid) if rowid is not None else None,
                    "snippet": str(snippet or "").strip(),
                    "generated_summary": generated,
                }
            )
        return rows

    def peek_mail(
        self,
        items: list[dict[str, Any]] | None = None,
        *,
        tokens: list[str] | None = None,
        limit: int = 8,
        query: str = "",
    ) -> list[dict[str, Any]]:
        """Read live Mail envelopes and ask-time gists. No body Event writes."""
        from app.memory.mail_speak import (
            find_emlx,
            generated_summary_text,
            gist_from_preview,
            mail_selector,
            preview_from_emlx,
        )

        rows = items
        if rows is None:
            path = self.mail_index_path
            if not _sqlite_readable(path):
                return []
            fetched = self._mail_index_rows(path, inbox_only=True)
            if not fetched:
                fetched = self._mail_index_rows(path, inbox_only=False)
            rows = fetched
        selector = mail_selector(query)
        wanted = [token for token in (tokens or []) if token] or selector.tokens()
        hits: list[dict[str, Any]] = []
        for index, item in enumerate(rows or []):
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()
            sender = str(item.get("sender") or "").strip()
            if not subject and not sender:
                continue
            generated = generated_summary_text(
                item.get("generated_summary") or item.get("generated")
            )
            snippet = str(item.get("snippet") or item.get("preview") or "").strip()
            preview = generated or snippet
            gist = gist_from_preview(preview, subject=subject, want=wanted)
            if not gist:
                gist = subject
            text = f"{subject} from {sender}".strip() if subject and sender else (subject or sender)
            if gist and gist.lower() not in text.lower():
                text = f"{text}. {gist}".strip(" .")
            received = _mail_received_iso(item.get("received"))
            rowid = item.get("rowid")
            try:
                rowid_n = int(rowid) if rowid is not None else None
            except (TypeError, ValueError):
                rowid_n = None
            hits.append(
                {
                    "id": f"mail-live-{index}-{text[:40]}",
                    "source": "mail",
                    "when": received,
                    "text": text[:400],
                    "subject": subject,
                    "sender": sender,
                    "gist": gist,
                    "snippet": snippet[:2400],
                    "rowid": rowid_n,
                    "kind": "live_mac",
                    "memory_type": "mail.envelope.received",
                    "confidence": "live_mac",
                    "score": 0.15,
                    "provenance": [f"mail-live-{index}"],
                    "shelf": "mail",
                    "channel": "mail",
                    "gist_source": "generated" if generated else ("snippet" if snippet else "subject"),
                }
            )
        filtered = _filter_peek(hits, wanted, limit)
        if selector.particular and filtered:
            self._enrich_mail_gist(filtered[0], wanted=wanted)
        return filtered

    def _enrich_mail_gist(self, hit: dict[str, Any], *, wanted: list[str]) -> None:
        """Ask-time .emlx gist for a particular mail when the index has no snippet."""
        from app.memory.mail_speak import find_emlx, gist_from_preview, preview_from_emlx

        if str(hit.get("gist_source") or "") != "subject":
            return
        rowid = hit.get("rowid")
        if not rowid or not self.mail_index_path:
            return
        version_root = str(Path(self.mail_index_path).resolve().parent.parent)
        path = find_emlx(version_root, int(rowid))
        if path is None:
            return
        preview = preview_from_emlx(path)
        if not preview:
            return
        subject = str(hit.get("subject") or "")
        gist = gist_from_preview(preview, subject=subject, want=wanted)
        if not gist:
            return
        hit["gist"] = gist
        hit["gist_source"] = "emlx"
        text = str(hit.get("text") or "")
        if gist.lower() not in text.lower():
            hit["text"] = f"{text}. {gist}".strip(" .")[:400]

    async def sync_contacts_delta(
        self,
        session: AsyncSession,
        contacts: list[dict],
    ) -> list[Any]:
        """Ingest new or modified contacts as immutable events."""
        events = []
        event_service = EventService(session, actor="life_stream_daemon")

        for contact in contacts:
            cid = str(contact.get("id") or contact.get("name") or "")
            if not cid:
                continue

            is_new = cid not in self._known_contact_ids
            self._known_contact_ids.add(cid)

            fingerprint = _contact_fingerprint(contact)
            if not is_new and self._contact_fps.get(cid) == fingerprint:
                continue
            self._contact_fps[cid] = fingerprint
            if len(self._contact_fps) > _MAX_CONTACT_FPS:
                extra = list(self._contact_fps)[: len(self._contact_fps) - _MAX_CONTACT_FPS]
                for key in extra:
                    self._contact_fps.pop(key, None)
                    self._known_contact_ids.discard(key)

            event_type = "contact.discovered" if is_new else "contact.updated"
            phone = contact.get("phone") or ""
            if not phone and contact.get("phone_numbers"):
                numbers = contact.get("phone_numbers") or []
                phone = numbers[0] if numbers else ""
            email = contact.get("email") or ""
            if not email and contact.get("email_addresses"):
                addresses = contact.get("email_addresses") or []
                email = addresses[0] if addresses else ""
            event_create = EventCreate(
                event_type=event_type,
                source="contacts",
                content={
                    "contact_id": cid,
                    "name": contact.get("name") or contact.get("full_name") or "",
                    "phone": phone,
                    "email": email,
                    "company": contact.get("company") or "",
                },
                privacy_level="sensitive",
                metadata={"source": "apple_contacts"},
            )
            created_event = await event_service.create(event_create)
            events.append(created_event)

        if events:
            await session.commit()

        return events

    async def sync_mail_delta(
        self,
        session: AsyncSession,
        items: list[dict],
    ) -> list[Any]:
        """Ingest new Mail envelopes as immutable events. Subjects only, no bodies."""
        events = []
        event_service = EventService(session, actor="life_stream_daemon")
        for item in items:
            subject = str(item.get("subject") or "").strip()
            sender = str(item.get("sender") or "").strip()
            received = str(item.get("received") or "").strip()
            if not subject and not sender:
                continue
            fingerprint = f"{subject}|{sender}|{received}"
            if self._mail_fps.get(fingerprint) == "1":
                continue
            if len(self._mail_fps) >= _MAX_CONTACT_FPS:
                extra = list(self._mail_fps)[: len(self._mail_fps) - _MAX_CONTACT_FPS + 1]
                for key in extra:
                    self._mail_fps.pop(key, None)
            self._mail_fps[fingerprint] = "1"
            text = f"{subject} from {sender}".strip()
            event_create = EventCreate(
                event_type="mail.envelope.received",
                source="mail",
                content={
                    "text": text,
                    "subject": subject,
                    "sender": sender,
                    "received": received,
                },
                privacy_level="sensitive",
                metadata={"source": "apple_mail", "channel": "mail"},
            )
            created_event = await event_service.create(event_create)
            events.append(created_event)

        if events:
            await session.commit()
        return events

    async def sync_calendar_delta(
        self,
        session: AsyncSession,
        items: list[dict],
    ) -> list[Any]:
        """Ingest calendar envelopes. Titles and times only."""
        events = []
        event_service = EventService(session, actor="life_stream_daemon")
        for item in items:
            summary = str(item.get("summary") or item.get("title") or "").strip()
            start = str(item.get("start") or "").strip()
            event_id = str(item.get("event_id") or item.get("id") or "").strip()
            if not summary and not event_id:
                continue
            fingerprint = event_id or f"{summary}|{start}"
            if self._calendar_fps.get(fingerprint) == "1":
                continue
            if len(self._calendar_fps) >= _MAX_CONTACT_FPS:
                extra = list(self._calendar_fps)[: len(self._calendar_fps) - _MAX_CONTACT_FPS + 1]
                for key in extra:
                    self._calendar_fps.pop(key, None)
            self._calendar_fps[fingerprint] = "1"
            location = str(item.get("location") or "").strip()
            end = str(item.get("end") or "").strip()
            text = summary if not start else f"{summary} at {start}"
            event_create = EventCreate(
                event_type="calendar.event.recorded",
                source="calendar",
                content={
                    "text": text,
                    "summary": summary,
                    "start": start,
                    "end": end,
                    "location": location,
                    "event_id": event_id,
                },
                privacy_level="sensitive",
                metadata={"source": "google_calendar", "channel": "calendar"},
            )
            created_event = await event_service.create(event_create)
            events.append(created_event)

        if events:
            await session.commit()
        return events

    async def sync_health_delta(
        self,
        session: AsyncSession,
        items: list[dict] | None = None,
    ) -> list[Any]:
        """Record compact health snapshots as events. Metrics only, no dump into chat."""
        from app.models import HealthSnapshot

        rows: list[Any]
        if items:
            rows = items
        else:
            result = await session.execute(
                select(HealthSnapshot).order_by(HealthSnapshot.occurred_at.desc()).limit(8)
            )
            rows = list(result.scalars().all())
        events = []
        event_service = EventService(session, actor="life_stream_daemon")
        for row in rows:
            payload = health_snapshot_event_create(row)
            if payload is None:
                continue
            sid = str((payload.metadata or {}).get("snapshot_id") or "")
            if not sid or self._health_fps.get(sid) == "1":
                continue
            if len(self._health_fps) >= _MAX_CONTACT_FPS:
                extra = list(self._health_fps)[: len(self._health_fps) - _MAX_CONTACT_FPS + 1]
                for key in extra:
                    self._health_fps.pop(key, None)
            self._health_fps[sid] = "1"
            created_event = await event_service.create(payload)
            events.append(created_event)

        if events:
            await session.commit()
        return events

    def account_pull_due(self) -> bool:
        return (time.time() - self._last_account_pull) >= _ACCOUNT_PULL_SECONDS

    def mark_account_pulled(self) -> None:
        self._last_account_pull = time.time()

    async def tick(
        self,
        session: AsyncSession,
        *,
        sample_contacts: list[dict] | None = None,
        sample_mail: list[dict] | None = None,
        sample_calendar: list[dict] | None = None,
        sample_health: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Run a single sensory tick across all observed local life streams."""
        imessage_events = await self._safe_sync("imessage", self.sync_imessage(session, limit=25))
        whatsapp_events = await self._safe_sync("whatsapp", self.sync_whatsapp(session, limit=25))
        call_events = await self._safe_sync("calls", self.sync_calls(session, limit=25))
        photo_events = await self._safe_sync("photos", self.sync_photos(session, limit=25))
        contact_events = []
        mail_events = []
        calendar_events = []
        if sample_contacts:
            contact_events = await self.sync_contacts_delta(session, sample_contacts)
        if sample_mail:
            mail_events = await self.sync_mail_delta(session, sample_mail)
        if sample_calendar:
            calendar_events = await self.sync_calendar_delta(session, sample_calendar)
        if sample_health:
            health_events = await self._safe_sync(
                "health", self.sync_health_delta(session, sample_health)
            )
        else:
            health_events = []

        self.save_cursor()
        return {
            "ok": True,
            "messages_ingested": len(imessage_events),
            "whatsapp_ingested": len(whatsapp_events),
            "calls_ingested": len(call_events),
            "photos_ingested": len(photo_events),
            "contacts_ingested": len(contact_events),
            "mail_ingested": len(mail_events),
            "calendar_ingested": len(calendar_events),
            "health_ingested": len(health_events),
            "latest_message_rowid": self.last_message_rowid,
            "latest_whatsapp_pk": self.last_whatsapp_pk,
            "latest_call_pk": self.last_call_pk,
            "latest_photo_pk": self.last_photo_pk,
            "chat_db_accessible": self.is_chat_db_accessible(),
        }

    def attach_cursor(self, path: str | os.PathLike[str] | None) -> None:
        if not path:
            return
        self._cursor_path = Path(path).expanduser()
        self.load_cursor()

    def load_cursor(self) -> None:
        path = self._cursor_path
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        rowid = payload.get("last_message_rowid")
        if isinstance(rowid, int) and rowid >= 0:
            self.last_message_rowid = rowid
        for key, attr in (
            ("last_whatsapp_pk", "last_whatsapp_pk"),
            ("last_call_pk", "last_call_pk"),
            ("last_photo_pk", "last_photo_pk"),
        ):
            value = payload.get(key)
            if isinstance(value, int) and value >= 0:
                setattr(self, attr, value)
                if value > 0:
                    self._bootstrapped.add(attr)
        fps = payload.get("contact_fps")
        if isinstance(fps, dict):
            self._contact_fps = {str(key): str(value) for key, value in fps.items()}
            self._known_contact_ids = set(self._contact_fps)
        mail_fps = payload.get("mail_fps")
        if isinstance(mail_fps, dict):
            self._mail_fps = {str(key): str(value) for key, value in mail_fps.items()}
        calendar_fps = payload.get("calendar_fps")
        if isinstance(calendar_fps, dict):
            self._calendar_fps = {str(key): str(value) for key, value in calendar_fps.items()}
        health_fps = payload.get("health_fps")
        if isinstance(health_fps, dict):
            self._health_fps = {str(key): str(value) for key, value in health_fps.items()}
        pulled = payload.get("last_account_pull")
        if isinstance(pulled, (int, float)) and pulled >= 0:
            self._last_account_pull = float(pulled)

    def save_cursor(self) -> None:
        path = self._cursor_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "last_message_rowid": self.last_message_rowid,
                        "last_whatsapp_pk": self.last_whatsapp_pk,
                        "last_call_pk": self.last_call_pk,
                        "last_photo_pk": self.last_photo_pk,
                        "contact_fps": self._contact_fps,
                        "mail_fps": self._mail_fps,
                        "calendar_fps": self._calendar_fps,
                        "health_fps": self._health_fps,
                        "last_account_pull": self._last_account_pull,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            logger.debug("life_stream cursor save skipped", exc_info=True)


_PROCESS_DAEMON: LifeStreamDaemon | None = None


def get_life_stream_daemon() -> LifeStreamDaemon:
    """Process-local daemon so incremental rowids survive scheduler ticks."""
    global _PROCESS_DAEMON
    if _PROCESS_DAEMON is None:
        from app.config import settings

        _PROCESS_DAEMON = LifeStreamDaemon()
        _PROCESS_DAEMON.attach_cursor(
            getattr(settings, "life_stream_cursor_path", "") or None
        )
    return _PROCESS_DAEMON
