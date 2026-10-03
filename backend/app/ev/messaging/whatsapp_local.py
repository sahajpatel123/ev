"""Read-only WhatsApp Desktop cache access without UI, AX, or browser pairing.

This reads the owner's existing ChatStorage; it never mutates WhatsApp's
SQLite database, marks messages read, or claims cache contents are live.
Sending remains a separate authenticated, owner-approved background route.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_DEFAULT_DB = "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"


def _under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _database() -> Path:
    return Path(os.environ.get("EV_WHATSAPP_LOCAL_DB") or _DEFAULT_DB).expanduser()


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text)).split()).casefold()


def _snapshot(path: Path) -> dict[str, Any]:
    modified = max(path.stat().st_mtime, (Path(str(path) + "-wal").stat().st_mtime
                   if Path(str(path) + "-wal").is_file() else 0))
    return {"source": "whatsapp_desktop_cache", "driver": "desktop_sqlite_readonly",
            "background": True, "focus_theft": 0, "marks_read": False,
            "cache_modified_at": datetime.fromtimestamp(modified, UTC).isoformat(),
            "freshness": "desktop_sync_unknown", "complete_history": False}


def _connect(path: Path) -> sqlite3.Connection:
    # mode=ro includes committed WAL records; immutable would silently omit
    # recent messages. query_only is a connection setting, not a database write.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    required = {"ZWACHATSESSION": {"Z_PK", "ZPARTNERNAME", "ZCONTACTJID"},
                "ZWAMESSAGE": {"Z_PK", "ZCHATSESSION", "ZTEXT", "ZISFROMME", "ZMESSAGEDATE"}}
    try:
        for table, fields in required.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not fields <= columns:
                raise sqlite3.DatabaseError("unsupported_schema")
    except Exception:
        connection.close()
        raise
    return connection


def _chats(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(ZWACHATSESSION)")}
    order = "ZLASTMESSAGEDATE DESC, Z_PK DESC" if "ZLASTMESSAGEDATE" in columns else "Z_PK DESC"
    rows = connection.execute(f"SELECT Z_PK,ZPARTNERNAME,ZCONTACTJID FROM ZWACHATSESSION ORDER BY {order} LIMIT 10000")
    chats = []
    for row in rows:
        jid = str(row["ZCONTACTJID"] or "")
        if not jid or "broadcast" in jid or jid.endswith("@newsletter") or jid == "status@broadcast":
            continue
        digits = jid.split("@", 1)[0]
        phone = digits if digits.isdigit() and jid.endswith("@s.whatsapp.net") else ""
        name = str(row["ZPARTNERNAME"] or phone or jid).strip()
        chats.append({"chat_ref": f"local:{row['Z_PK']}", "name": name,
                      "phone": phone, "kind": "group" if jid.endswith("@g.us") else "chat"})
    return chats


def _match(connection: sqlite3.Connection, requested: str) -> dict[str, Any]:
    chats = _chats(connection)
    if re.fullmatch(r"local:[1-9]\d*", requested):
        hits = [chat for chat in chats if chat["chat_ref"] == requested]
    else:
        query = _norm(requested)
        hits = [chat for chat in chats if _norm(chat["name"]) == query]
        if not hits and re.fullmatch(r"[+\d\s().-]+", requested or ""):
            digits = re.sub(r"\D", "", requested)
            hits = [chat for chat in chats if len(digits) >= 7 and chat["phone"] == digits]
    if len(hits) != 1:
        return {"ok": False, "error": "ambiguous_recipient" if hits else "chat_not_found",
                "candidates": [{"name": row["name"], "chat_ref": row["chat_ref"]} for row in hits[:8]]}
    return {"ok": True, **hits[0]}


def _read_sync(operation: str, *, to: str = "", query: str = "", limit: int = 20) -> dict[str, Any]:
    if _under_pytest():
        return {"ok": False, "error": "local_disabled_in_tests"}
    path = _database().resolve()
    connection = None
    try:
        connection = _connect(path)
        snapshot = _snapshot(path)
        if operation == "status":
            return {"ok": True, **snapshot, "read_available": True, "draft_available": True,
                    "send_available": False, "authenticated": False, "local_only": True,
                    "diagnosis": "desktop_cache_ready"}
        if operation == "search_chats":
            wanted = _norm(query)
            chats = [row for row in _chats(connection)
                     if not wanted or wanted in _norm(row["name"]) or wanted in row["phone"]]
            return {"ok": True, **snapshot, "chats": chats[:max(1, min(int(limit), 200))],
                    "scope": "desktop_cache_chat_list"}
        match = _match(connection, to)
        if not match.get("ok"):
            return {**match, **snapshot}
        if operation == "open_chat":
            return {**match, **snapshot}
        cap = max(1, min(int(limit), 2000))
        identifier = int(match["chat_ref"].split(":", 1)[1])
        message_columns = {row[1] for row in connection.execute("PRAGMA table_info(ZWAMESSAGE)")}
        sender_name = "ZPUSHNAME" if "ZPUSHNAME" in message_columns else "NULL"
        sender_jid = "ZFROMJID" if "ZFROMJID" in message_columns else "NULL"
        sql = f"SELECT {sender_name} AS sender_name,{sender_jid} AS sender_jid,Z_PK,substr(ZTEXT,1,65536) AS body,length(ZTEXT) AS body_length,ZISFROMME,ZMESSAGEDATE FROM ZWAMESSAGE WHERE ZCHATSESSION=? AND ZTEXT IS NOT NULL AND ZTEXT!=''"
        parameters: list[Any] = [identifier]
        if operation == "search_messages":
            sql += " AND instr(lower(ZTEXT),lower(?))>0"
            parameters.append(query)
        sql += " ORDER BY ZMESSAGEDATE DESC,Z_PK DESC LIMIT ?"
        parameters.append(cap + 1)
        rows = list(connection.execute(sql, parameters))
        truncated = len(rows) > cap
        messages = []
        characters = 0
        for row in rows[:cap]:
            body = str(row["body"] or "")
            characters += len(body)
            if characters > 2_000_000:
                truncated = True
                break
            raw_time = row["ZMESSAGEDATE"]
            try:
                stamped = (datetime(2001, 1, 1, tzinfo=UTC) + timedelta(seconds=float(raw_time))).isoformat() if raw_time is not None else None
            except (TypeError, ValueError, OverflowError):
                stamped = None
            messages.append({"id": f"local-message:{row['Z_PK']}", "chat_ref": match["chat_ref"],
                             "from_me": bool(row["ZISFROMME"]), "text": body, "body": body,
                             "timestamp": stamped,
                             "sender": "owner" if row["ZISFROMME"] else str(row["sender_name"] or row["sender_jid"] or match["name"]),
                             "body_truncated": int(row["body_length"] or 0) > len(body)})
        messages.reverse()
        return {"ok": True, **snapshot, "to": match["name"], "chat_ref": match["chat_ref"],
                "name": match["name"], "messages": messages, "scope": "desktop_cache_text_messages",
                "bounded": truncated, "non_text_media_included": False}
    except (sqlite3.Error, OSError, ValueError):
        return {"ok": False, "error": "desktop_cache_unavailable", "read_available": False,
                "draft_available": False, "send_available": False, "authenticated": False,
                "background": True, "focus_theft": 0, "diagnosis": "desktop_cache_unavailable"}
    finally:
        if connection is not None:
            connection.close()


async def status(*, refresh: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(_read_sync, "status")


async def search_chats(query: str = "", *, limit: int = 30) -> dict[str, Any]:
    return await asyncio.to_thread(_read_sync, "search_chats", query=query, limit=limit)


async def open_chat(to: str) -> dict[str, Any]:
    return await asyncio.to_thread(_read_sync, "open_chat", to=to)


async def read_recent(to: str, *, limit: int = 20) -> dict[str, Any]:
    return await asyncio.to_thread(_read_sync, "read_recent", to=to, limit=limit)


async def search_messages(to: str, query: str, *, limit: int = 30) -> dict[str, Any]:
    return await asyncio.to_thread(_read_sync, "search_messages", to=to, query=query, limit=limit)
