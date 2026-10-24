"""Worker effects are durable before they count (R6).

Graph workers run tools inside a session of their own. Tools flush; they do
not commit, because on the typed path the request commits after the turn.
The workers had no such commit, so their writes were rolled back when the
session closed (live catch: three voice timers reported "Timer set" with an
id and observed=true; none existed).

``WriteLedger`` records every row a tool call flushes, commits that call, and
then reads those rows back on a fresh connection. A row that is not where
the call left it is a lost write: the call does not count as done, whatever
the tool said. A call that raises is rolled back so its half-written rows
cannot ride along with the next call's commit.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

CHECK = "fresh_read_after_commit"
NOT_SAVED_SPOKEN = "I tried, but it didn't save, so it isn't done."
_MAX_ROWS = 32
# (mapped class, primary key values): one row a tool call touched.
_Key = tuple[type[Any], tuple[Any, ...]]


@dataclass
class Settlement:
    """Outcome of committing one tool call and reading its rows back."""

    committed: bool
    expected: int = 0
    missing: list[str] = field(default_factory=list)
    unchecked: int = 0
    error: str | None = None

    @property
    def durable(self) -> bool:
        return self.committed and not self.missing

    def evidence(self, tool: str) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "tool": tool,
            "check": CHECK,
            "durable": self.durable,
            "rows_verified": self.expected - len(self.missing),
            "rows_missing": len(self.missing),
        }
        if self.unchecked:
            entry["rows_unchecked"] = self.unchecked
        if self.error:
            entry["error"] = self.error
        return entry


async def _commit(session: AsyncSession) -> None:
    await session.commit()


async def _rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001 - a failed rollback must not mask the call's outcome
        logger.debug("worker rollback failed", exc_info=True)


class WriteLedger:
    """Rows flushed by the current tool call, and whether each should exist."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._expect: dict[_Key, bool] = {}
        self._overflow = 0
        event.listen(session.sync_session, "after_flush", self._on_flush)

    def close(self) -> None:
        event.remove(self._session.sync_session, "after_flush", self._on_flush)

    def _note(self, obj: Any, *, present: bool) -> None:
        mapper = inspect(obj).mapper
        key = (mapper.class_, tuple(mapper.primary_key_from_instance(obj)))
        if None in key[1]:
            return
        if key not in self._expect and len(self._expect) >= _MAX_ROWS:
            self._overflow += 1
            return
        # Last operation wins: insert-then-delete in one call expects absence.
        self._expect[key] = present

    def _on_flush(self, sync_session: Any, _context: Any) -> None:
        for obj in sync_session.new:
            self._note(obj, present=True)
        for obj in sync_session.dirty:
            if sync_session.is_modified(obj, include_collections=False):
                self._note(obj, present=True)
        for obj in sync_session.deleted:
            self._note(obj, present=False)

    async def settle(self, *, raised: bool = False) -> Settlement:
        expect, self._expect = self._expect, {}
        overflow, self._overflow = self._overflow, 0
        if raised:
            await _rollback(self._session)
            return Settlement(committed=False, error="tool_raised")
        try:
            await _commit(self._session)
        except Exception as exc:  # noqa: BLE001 - a failed commit is a lost write, not a crash
            await _rollback(self._session)
            lost = [_label(key) for key in expect]
            return Settlement(committed=False, expected=len(lost), missing=lost,
                              unchecked=overflow, error=type(exc).__name__)
        if not expect:
            return Settlement(committed=True, unchecked=overflow)
        missing = await _read_back(expect)
        if missing:
            logger.error("worker write did not persist rows=%s", missing[:5])
        return Settlement(committed=True, expected=len(expect), missing=missing,
                          unchecked=overflow)


def _label(key: _Key) -> str:
    cls, pk = key
    table = getattr(cls, "__tablename__", cls.__name__)
    return f"{table}:{','.join(str(part) for part in pk)}"


async def _read_back(expect: dict[_Key, bool]) -> list[str]:
    """Rows not where the call left them, read on a connection of its own."""

    from app.db import SessionLocal

    missing: list[str] = []
    async with SessionLocal() as fresh:
        for key, present in expect.items():
            cls, pk = key
            row = await fresh.get(cls, pk[0] if len(pk) == 1 else pk)
            if (row is not None) != present:
                missing.append(_label(key))
    return missing


@asynccontextmanager
async def worker_session() -> AsyncIterator[tuple[AsyncSession, WriteLedger]]:
    """A worker's session plus the ledger that settles each tool call on it."""

    from app.db import SessionLocal

    async with SessionLocal() as session:
        ledger = WriteLedger(session)
        try:
            yield session, ledger
        finally:
            ledger.close()


async def settled_call(
    ledger: WriteLedger, tool: str, call: Awaitable[Any]
) -> dict[str, Any]:
    """Await one tool call, then make its writes durable or discard them.

    The returned body carries a ``durability`` entry when the call wrote
    rows. A success whose rows did not persist becomes a failure with
    ``error="not_persisted"``; the tool's own success line is not spoken.
    """

    try:
        out = await call
    except BaseException:
        await ledger.settle(raised=True)
        raise
    settlement = await ledger.settle()
    if not isinstance(out, dict):
        return out
    if not settlement.expected:
        # Nothing was written, so there is nothing to lose: a read keeps its
        # answer even if the empty commit failed.
        return out
    body = dict(out)
    body["durability"] = settlement.evidence(tool)
    if body.get("ok") and not settlement.durable:
        body["ok"] = False
        body["error"] = "not_persisted"
        body["spoken"] = NOT_SAVED_SPOKEN
    return body


def durability_evidence(body: Any) -> list[dict[str, Any]]:
    """The durability entry of one settled body, as a receipt evidence list."""

    if isinstance(body, dict) and isinstance(body.get("durability"), dict):
        return [dict(body["durability"])]
    return []
