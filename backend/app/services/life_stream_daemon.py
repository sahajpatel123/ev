"""Continuous living sensory daemon for EV (Evie).

Headless follower for local and account life streams:
- iMessage / SMS: polls ``~/Library/Messages/chat.db`` for new messages
- Apple Contacts: delta-syncs the address book via the life helper
- Apple Mail / Gmail metadata: inbox envelopes (subject/sender only)
- Google Calendar: titles and times copied from OAuth or the live channel
- Health snapshots: compact metric envelopes when a snapshot is posted

Rows are stored as events and opened only when recall chooses that shelf.
They are never written into live_context. Degrades in CI, Linux, or without
Full Disk Access / OAuth grants.
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
    ) -> None:
        self.chat_db_path = (
            chat_db_path
            if chat_db_path is not None
            else os.path.expanduser("~/Library/Messages/chat.db")
        )
        self.last_message_rowid = last_message_rowid
        self._known_contact_ids: set[str] = set()
        self._contact_fps: dict[str, str] = {}
        self._mail_fps: dict[str, str] = {}
        self._calendar_fps: dict[str, str] = {}
        self._health_fps: dict[str, str] = {}
        self._last_account_pull: float = 0.0
        self._cursor_path: Path | None = None

    def is_chat_db_accessible(self) -> bool:
        """Return True if chat.db exists and is readable (Full Disk Access granted)."""
        return (
            bool(self.chat_db_path)
            and os.path.isfile(self.chat_db_path)
            and os.access(self.chat_db_path, os.R_OK)
        )

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
        imessage_events = await self.sync_imessage(session, limit=25)
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
            try:
                health_events = await self.sync_health_delta(session, sample_health)
            except Exception:  # noqa: BLE001 - health ingest must not block iMessage
                health_events = []
        else:
            health_events = []

        self.save_cursor()
        return {
            "ok": True,
            "messages_ingested": len(imessage_events),
            "contacts_ingested": len(contact_events),
            "mail_ingested": len(mail_events),
            "calendar_ingested": len(calendar_events),
            "health_ingested": len(health_events),
            "latest_message_rowid": self.last_message_rowid,
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
