"""Official Gmail API client. Tokens stay on the call stack, never in results."""

from __future__ import annotations

import base64
import email.utils
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from app.digital.taint import scan_injection, taint_external
from app.digital.types import GMAIL_PAGE_MAX
from app.digital.vault_bound import TokenLease, strip_secrets
from app.integrations import oauth

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


class GmailTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        ...


class HttpxGmailTransport:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        own = self._client is None
        client = self._client or oauth.make_http_client(timeout=30.0)
        try:
            return await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                content=content,
            )
        finally:
            if own:
                await client.aclose()


@dataclass
class GmailClient:
    lease: TokenLease
    transport: GmailTransport
    page_size: int = 25

    def _headers(self) -> dict[str, str]:
        return {**self.lease.header(), "Accept": "application/json"}

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{GMAIL_API}{path}"
        response = await self.transport.request(
            method, url, headers=self._headers(), params=params, json_body=json_body
        )
        if response.status_code in (401, 403):
            raise oauth.google_api_auth_error(response, "gmail")
        if response.status_code == 429:
            raise oauth.OAuthProviderError("gmail rate limited (status 429)")
        if response.status_code == 404:
            raise oauth.OAuthProviderError("gmail resource missing (status 404)")
        if response.status_code >= 400:
            raise oauth.OAuthProviderError(f"gmail call failed (status {response.status_code})")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise oauth.OAuthProviderError("gmail returned non-JSON") from exc
        if not isinstance(data, dict):
            raise oauth.OAuthProviderError("gmail returned malformed JSON")
        return strip_secrets(data)

    async def profile(self) -> dict[str, Any]:
        data = await self._call("GET", "/users/me/profile")
        return {
            "email": data.get("emailAddress"),
            "messages_total": data.get("messagesTotal"),
            "history_id": data.get("historyId"),
        }

    async def list_labels(self) -> list[dict[str, Any]]:
        data = await self._call("GET", "/users/me/labels")
        _labels_raw: Any = data.get("labels")
        labels: list[Any] = _labels_raw if isinstance(_labels_raw, list) else []
        out = []
        for row in labels:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "type": row.get("type"),
                }
            )
        return out

    async def search(self, q: str, *, page_token: str | None = None, limit: int | None = None) -> dict[str, Any]:
        cap = max(1, min(int(limit or self.page_size), GMAIL_PAGE_MAX))
        params = {"q": q, "maxResults": str(cap)}
        if page_token:
            params["pageToken"] = page_token
        data = await self._call("GET", "/users/me/messages", params=params)
        _rows_raw: Any = data.get("messages")
        rows: list[Any] = _rows_raw if isinstance(_rows_raw, list) else []
        ids = [str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")]
        return {
            "ids": ids[:cap],
            "next_page_token": data.get("nextPageToken"),
            "result_size_estimate": data.get("resultSizeEstimate"),
            "q": q,
        }

    async def get_message(self, message_id: str, *, format: str = "full") -> dict[str, Any]:
        data = await self._call(
            "GET",
            f"/users/me/messages/{message_id}",
            params={"format": format},
        )
        parsed = parse_message(data)
        inj = scan_injection(parsed.get("text") or parsed.get("snippet") or "")
        return taint_external(
            parsed,
            source="gmail",
            extra={"gmail_id": message_id, **inj},
        )

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        data = await self._call(
            "GET",
            f"/users/me/threads/{thread_id}",
            params={"format": "full"},
        )
        _msgs_raw: Any = data.get("messages")
        messages: list[Any] = _msgs_raw if isinstance(_msgs_raw, list) else []
        parsed = [parse_message(m) for m in messages if isinstance(m, dict)]
        text = " ".join(p.get("text") or p.get("snippet") or "" for p in parsed)
        return taint_external(
            {
                "thread_id": data.get("id") or thread_id,
                "messages": parsed,
                "count": len(parsed),
            },
            source="gmail",
            extra={"gmail_thread": thread_id, **scan_injection(text)},
        )

    async def attachment_meta(self, message_id: str) -> list[dict[str, Any]]:
        msg = await self._call(
            "GET",
            f"/users/me/messages/{message_id}",
            params={"format": "full"},
        )
        return list_attachments(msg)

    async def download_attachment(
        self, message_id: str, attachment_id: str
    ) -> dict[str, Any]:
        data = await self._call(
            "GET",
            f"/users/me/messages/{message_id}/attachments/{attachment_id}",
        )
        raw = data.get("data")
        if not isinstance(raw, str):
            raise oauth.OAuthProviderError("gmail attachment missing data")
        blob = _b64url_decode(raw)
        return {
            "message_id": message_id,
            "attachment_id": attachment_id,
            "size": len(blob),
            "sha256": _sha256(blob),
            "bytes": blob,
            "origin": "EXTERNAL_CONTENT",
            "authority": "DATA",
        }

    async def modify(
        self,
        message_id: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        data = await self._call("POST", f"/users/me/messages/{message_id}/modify", json_body=body)
        return {"id": data.get("id"), "label_ids": data.get("labelIds") or []}

    async def trash(self, message_id: str) -> dict[str, Any]:
        data = await self._call("POST", f"/users/me/messages/{message_id}/trash")
        labels = data.get("labelIds") or []
        message = data.get("id")
        return {
            "id": message,
            "label_ids": labels,
            "trashed": bool(message) and "TRASH" in labels,
        }

    async def untrash(self, message_id: str) -> dict[str, Any]:
        data = await self._call("POST", f"/users/me/messages/{message_id}/untrash")
        labels = data.get("labelIds") or []
        message = data.get("id")
        return {
            "id": message,
            "label_ids": labels,
            "restored": bool(message) and "TRASH" not in labels,
        }

    async def list_drafts(self, *, limit: int = 20) -> list[dict[str, Any]]:
        cap = max(1, min(limit, GMAIL_PAGE_MAX))
        data = await self._call("GET", "/users/me/drafts", params={"maxResults": str(cap)})
        _drafts_raw: Any = data.get("drafts")
        rows: list[Any] = _drafts_raw if isinstance(_drafts_raw, list) else []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            _msg_raw: Any = row.get("message")
            msg: dict[str, Any] = _msg_raw if isinstance(_msg_raw, dict) else {}
            out.append({"id": row.get("id"), "message_id": msg.get("id"), "thread_id": msg.get("threadId")})
        return out

    async def create_draft(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        thread_id: str | None = None,
        cc: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        raw = build_raw_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            in_reply_to=in_reply_to,
            references=references,
            idempotency_key=idempotency_key,
        )
        message_part: dict[str, Any] = {"raw": raw}
        payload: dict[str, Any] = {"message": message_part}
        if thread_id:
            message_part["threadId"] = thread_id
        data = await self._call("POST", "/users/me/drafts", json_body=payload)
        _cmsg: Any = data.get("message")
        msg: dict[str, Any] = _cmsg if isinstance(_cmsg, dict) else {}
        return {
            "draft_id": data.get("id"),
            "message_id": msg.get("id"),
            "thread_id": msg.get("threadId") or thread_id,
            "sent": False,
            "prepared": True,
        }

    async def send_raw(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        thread_id: str | None = None,
        cc: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        raw = build_raw_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            in_reply_to=in_reply_to,
            references=references,
            idempotency_key=idempotency_key,
        )
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        data = await self._call("POST", "/users/me/messages/send", json_body=payload)
        message = data.get("id")
        return {
            "id": message,
            "thread_id": data.get("threadId") or thread_id,
            "label_ids": data.get("labelIds") or [],
            "sent": bool(message),
        }

    async def send_draft(self, draft_id: str) -> dict[str, Any]:
        data = await self._call("POST", "/users/me/drafts/send", json_body={"id": draft_id})
        message = data.get("id")
        return {
            "id": message,
            "thread_id": data.get("threadId"),
            "sent": bool(message),
        }

    async def find_by_rfc822(self, message_id_header: str) -> str | None:
        q = f"rfc822msgid:{message_id_header.strip('<>')}"
        found = await self.search(q, limit=1)
        ids = found.get("ids") or []
        return ids[0] if ids else None

    async def history(self, start_history_id: str, *, limit: int = 50) -> dict[str, Any]:
        data = await self._call(
            "GET",
            "/users/me/history",
            params={"startHistoryId": start_history_id, "maxResults": str(max(1, min(limit, 100)))},
        )
        _hist_raw: Any = data.get("history")
        records: list[Any] = _hist_raw if isinstance(_hist_raw, list) else []
        ids: list[str] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            for key in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
                _key_rows: Any = rec.get(key)
                rows: list[Any] = _key_rows if isinstance(_key_rows, list) else []
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    msg = row.get("message") if isinstance(row, dict) else None
                    if isinstance(msg, dict) and msg.get("id"):
                        ids.append(str(msg["id"]))
        return {
            "history_id": data.get("historyId") or start_history_id,
            "message_ids": list(dict.fromkeys(ids)),
            "next_page_token": data.get("nextPageToken"),
        }


def parse_message(data: dict[str, Any]) -> dict[str, Any]:
    _payload_raw: Any = data.get("payload")
    payload: dict[str, Any] = _payload_raw if isinstance(_payload_raw, dict) else {}
    _hdrs_raw: Any = payload.get("headers")
    _hdrs: list[Any] = _hdrs_raw if isinstance(_hdrs_raw, list) else []
    headers = _headers_map(_hdrs)
    text, html = _extract_bodies(payload)
    attachments = list_attachments(data)
    snippet = str(data.get("snippet") or "")[:500]
    return {
        "id": data.get("id"),
        "thread_id": data.get("threadId"),
        "label_ids": data.get("labelIds") or [],
        "internal_date": _ms_to_iso(data.get("internalDate")),
        "history_id": data.get("historyId"),
        "snippet": snippet,
        "subject": headers.get("subject") or "",
        "from": headers.get("from") or "",
        "to": headers.get("to") or "",
        "cc": headers.get("cc") or "",
        "date": headers.get("date") or "",
        "message_id_header": headers.get("message-id") or "",
        "in_reply_to": headers.get("in-reply-to") or "",
        "references": headers.get("references") or "",
        "text": (text or "")[:20000],
        "html_present": bool(html),
        "attachments": attachments,
        "unread": "UNREAD" in (data.get("labelIds") or []),
        "origin": "EXTERNAL_CONTENT",
        "authority": "DATA",
    }


def list_attachments(data: dict[str, Any]) -> list[dict[str, Any]]:
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
    found: list[dict[str, Any]] = []

    def walk(part: Any) -> None:
        if not isinstance(part, dict):
            return
        filename = str(part.get("filename") or "")
        _abody: Any = part.get("body")
        body: dict[str, Any] = _abody if isinstance(_abody, dict) else {}
        if filename and body.get("attachmentId"):
            found.append(
                {
                    "filename": filename,
                    "mime": part.get("mimeType"),
                    "size": body.get("size"),
                    "attachment_id": body.get("attachmentId"),
                    "part_id": part.get("partId"),
                }
            )
        _parts: Any = part.get("parts")
        for child in _parts or []:
            walk(child)

    walk(payload)
    return found


def build_raw_message(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    idempotency_key: str | None = None,
    from_addr: str | None = None,
) -> str:
    msg = EmailMessage()
    if from_addr:
        msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(datetime.now(UTC))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if idempotency_key:
        msg["X-Evie-Idempotency"] = idempotency_key
    msg.set_content(body or "")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")


def _headers_map(rows: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").lower()
        value = str(row.get("value") or "")
        if name:
            out[name] = value
    return out


def _extract_bodies(payload: dict[str, Any]) -> tuple[str, str]:
    text = ""
    html = ""

    def walk(part: Any) -> None:
        nonlocal text, html
        if not isinstance(part, dict):
            return
        mime = str(part.get("mimeType") or "")
        _bbody: Any = part.get("body")
        body: dict[str, Any] = _bbody if isinstance(_bbody, dict) else {}
        data = body.get("data")
        decoded = _b64url_decode(data).decode("utf-8", errors="replace") if isinstance(data, str) else ""
        if mime.startswith("text/plain") and decoded and not text:
            text = decoded
        elif mime.startswith("text/html") and decoded and not html:
            html = decoded
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    if not text and html:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
    return text, html


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + pad)


def _sha256(blob: bytes) -> str:
    import hashlib

    return hashlib.sha256(blob).hexdigest()


def _ms_to_iso(value: Any) -> str | None:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
