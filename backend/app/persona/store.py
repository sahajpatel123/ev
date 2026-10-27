"""Persona-owned SQLite store: weights, latch, lines, bits, stances.

One file, no migrations framework, no models.py. Path derives from the
repo's ~/.ev home convention (location_file, telegram-offset and friends
all live there); no data-dir setting exists in config.py (verified), so
there is nothing to read — callers may pass an explicit path instead
(tests and the lab do).

Writes are small and synchronous; reads stay far under the 5 ms budget.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.persona import learning
from app.persona.callbacks import normalize_line
from app.persona.learning import Beta, LearningWeights
from app.persona.opinions import Stance

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lines (
  line TEXT PRIMARY KEY,
  first_seen TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,
  technique TEXT NOT NULL,
  topic TEXT NOT NULL,
  reaction TEXT NOT NULL,
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS landed_bits (
  bit TEXT PRIMARY KEY,
  positives INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS callbacks_used (
  key TEXT NOT NULL,
  bit TEXT NOT NULL,
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (key, bit)
);
CREATE TABLE IF NOT EXISTS stances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL,
  text TEXT NOT NULL,
  reason TEXT NOT NULL,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_key ON outcomes(key);
CREATE INDEX IF NOT EXISTS idx_stances_topic ON stances(topic);
"""


def default_db_path() -> Path:
    """~/.ev/persona/persona.db (parents created on first open)."""
    return Path("~/.ev/persona/persona.db").expanduser()


class PersonaStore:
    """SQLite-backed CallbackStore + ledger + live state, bound to one key."""

    def __init__(self, path: Path | str | None = None, key: str = "owner:default"):
        self.path = Path(path).expanduser() if path else default_db_path()
        self.key = key or "owner:default"
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.executescript(SCHEMA)
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- CallbackStore protocol (lines global; outcomes/weights per key) --

    def has_line(self, line: str) -> bool:
        normalized = normalize_line(line)
        if not normalized:
            return False
        with self._lock:
            row = self._connect().execute(
                "SELECT 1 FROM lines WHERE line = ?", (normalized,)
            ).fetchone()
        return row is not None

    def record_line(self, line: str) -> None:
        normalized = normalize_line(line)
        if not normalized:
            return
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR IGNORE INTO lines (line) VALUES (?)", (normalized,)
            )
            conn.commit()

    def record_outcome(self, technique: str, topic: str, reaction: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO outcomes (key, technique, topic, reaction) VALUES (?,?,?,?)",
                (self.key, technique or "", topic or "", reaction),
            )
            conn.commit()
        self.save_weights(
            learning.update(self.load_weights(), technique, topic, reaction)
        )

    def load_weights(self) -> LearningWeights:
        state = self._load_blob()
        raw = state.get("weights") or {}
        techniques_raw = raw.get("techniques", {}) or {}
        topics_raw = raw.get("topics", {}) or {}
        rate_raw = raw.get("joke_rate", {}) or {}
        base = LearningWeights.default()
        merged = dict(base.techniques)
        for key, value in techniques_raw.items():
            if isinstance(value, dict):
                merged[key] = Beta(
                    float(value.get("alpha", 1.0)), float(value.get("beta", 1.0))
                )
        return LearningWeights(
            techniques=merged,
            topics={
                key: Beta(float(value.get("alpha", 1.0)), float(value.get("beta", 1.0)))
                for key, value in topics_raw.items()
                if isinstance(value, dict)
            },
            joke_rate=Beta(
                float(rate_raw.get("alpha", 1.0)), float(rate_raw.get("beta", 1.0))
            ),
        )

    def save_weights(self, weights: LearningWeights) -> None:
        state = self._load_blob()
        state["weights"] = learning.describe(weights)
        self._save_blob(state)

    def landed(self, technique: str, topic: str) -> bool:
        with self._lock:
            row = self._connect().execute(
                "SELECT COUNT(*) FROM outcomes "
                "WHERE technique = ? AND topic = ? AND reaction = 'positive'",
                (technique, topic),
            ).fetchone()
        return (row[0] if row else 0) >= 2

    # -- Landed bits: persist globally, callback at most once per key --

    def record_landed_bit(self, bit: str) -> None:
        text = " ".join((bit or "").split())
        if not text:
            return
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO landed_bits (bit, positives) VALUES (?, 1) "
                "ON CONFLICT(bit) DO UPDATE SET positives = positives + 1",
                (text[:300],),
            )
            conn.commit()

    def suggest_callback(self, key: str | None = None) -> str | None:
        """An unused landed bit for this conversation, or None."""
        who = key or self.key
        with self._lock:
            row = self._connect().execute(
                "SELECT bit FROM landed_bits WHERE positives >= 2 AND bit NOT IN "
                "(SELECT bit FROM callbacks_used WHERE key = ?) "
                "ORDER BY positives DESC, first_seen ASC LIMIT 1",
                (who,),
            ).fetchone()
        return str(row[0]) if row else None

    def confirm_callback(self, key: str, bit: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR IGNORE INTO callbacks_used (key, bit) VALUES (?, ?)",
                (key, bit),
            )
            conn.commit()

    # -- Opinion ledger (owner-wide) --

    def record(self, stance: Stance) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO stances (topic, text, reason, ts) VALUES (?,?,?,?)",
                (stance.topic, stance.text, stance.reason, stance.ts),
            )
            conn.commit()

    def recent(self, topic: str, limit: int = 5) -> list[Stance]:
        wanted = " ".join((topic or "").split()).lower()
        with self._lock:
            rows = self._connect().execute(
                "SELECT topic, text, reason, ts FROM stances "
                "WHERE lower(topic) = ? ORDER BY id DESC LIMIT ?",
                (wanted, max(1, limit)),
            ).fetchall()
        return [Stance(*row) for row in reversed(rows)]

    def contradicts(self, new_stance: Stance) -> list[Stance]:
        wanted_topic = " ".join((new_stance.topic or "").split()).lower()
        wanted_text = " ".join((new_stance.text or "").split()).lower()
        with self._lock:
            rows = self._connect().execute(
                "SELECT topic, text, reason, ts FROM stances WHERE lower(topic) = ?",
                (wanted_topic,),
            ).fetchall()
        return [
            Stance(*row)
            for row in rows
            if " ".join(str(row[1]).split()).lower() != wanted_text
        ]

    # -- Live-state blob (latch, flags, meta, night, session) --

    def _load_blob(self) -> dict[str, Any]:
        with self._lock:
            row = self._connect().execute(
                "SELECT blob FROM state WHERE key = ?", (self.key,)
            ).fetchone()
        if not row:
            return {}
        try:
            return dict(json.loads(row[0]) or {})
        except (ValueError, TypeError):
            return {}

    def _save_blob(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, sort_keys=True, default=str)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO state (key, blob) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET blob = excluded.blob",
                (self.key, payload),
            )
            conn.commit()

    def load_state(self) -> dict[str, Any]:
        """Raw live-state blob for this key (live.py interprets it)."""
        return self._load_blob()

    def save_state(self, state: dict[str, Any]) -> None:
        """Persist a live-state blob for this key."""
        self._save_blob(dict(state))
