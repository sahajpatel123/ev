"""Native Gmail adapter — official Gmail API over the existing OAuth vault."""

from __future__ import annotations

from typing import Any

from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.gmail_http import GmailClient, HttpxGmailTransport
from app.digital.query import compile_gmail_query
from app.digital.types import Availability, OpStatus, Verb
from app.digital.vault_bound import diagnose_oauth_error, lease_for

GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"


def _availability(connected: bool, write: bool, scopes: tuple[str, ...]) -> Availability:
    if not connected:
        return Availability.CONNECTION_REQUIRED
    if write:
        need = {GMAIL_SEND, GMAIL_MODIFY, GMAIL_COMPOSE}
        if not (set(scopes) & need) and GMAIL_MODIFY not in scopes and GMAIL_SEND not in scopes:
            return Availability.CONNECTION_REQUIRED
    return Availability.NATIVE


class GmailAdapter:
    slug = "gmail"
    display_name = "Gmail"

    def __init__(self, *, connected: bool | None = None, scopes: tuple[str, ...] | None = None) -> None:
        self._connected_override = connected
        self._scopes_override = scopes

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        connected = bool(self._connected_override) if self._connected_override is not None else False
        scopes = self._scopes_override or ()
        read_av = _availability(connected, False, scopes) if self._connected_override is not None else Availability.CONNECTION_REQUIRED
        write_av = _availability(connected, True, scopes) if self._connected_override is not None else Availability.CONNECTION_REQUIRED
        # Live projection is filled by graph.project() after vault inspection.
        if self._connected_override is None:
            read_av = Availability.NATIVE  # capability exists; runtime may be CONNECTION_REQUIRED
            write_av = Availability.NATIVE
        common = dict(
            credential_type="google_oauth",
            data_classification="external_mail",
            backing="gmail.googleapis.com",
        )
        return [
            cap("gmail", "search", Verb.SEARCH, read_av, read_write="read", risk="R0",
                verification_method="message_ids", **common),
            cap("gmail", "list_inbox", Verb.LIST, read_av, read_write="read", risk="R0",
                verification_method="message_ids", **common),
            cap("gmail", "read", Verb.READ, read_av, read_write="read", risk="R0",
                verification_method="message_id", **common),
            cap("gmail", "read_thread", Verb.READ, read_av, read_write="read", risk="R0",
                verification_method="thread_id", **common),
            cap("gmail", "download_attachment", Verb.DOWNLOAD, read_av, read_write="read", risk="R1",
                verification_method="sha256", **common),
            cap("gmail", "list_labels", Verb.LIST, read_av, read_write="read", risk="R0",
                verification_method="label_ids", **common),
            cap("gmail", "list_drafts", Verb.LIST, read_av, read_write="read", risk="R0",
                verification_method="draft_ids", **common),
            cap("gmail", "draft", Verb.PREPARE, write_av, read_write="write", risk="R1",
                requires_confirmation=False, verification_method="draft_id", **common),
            cap("gmail", "send", Verb.SEND, write_av, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="gmail_id+thread", **common),
            cap("gmail", "reply", Verb.REPLY, write_av, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="gmail_id+thread", **common),
            cap("gmail", "reply_all", Verb.REPLY, write_av, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="gmail_id+thread", **common),
            cap("gmail", "forward", Verb.SEND, write_av, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="gmail_id", **common),
            cap("gmail", "archive", Verb.ARCHIVE, write_av, read_write="write", risk="R1",
                requires_confirmation=False, verification_method="label_ids", **common),
            cap("gmail", "label", Verb.LABEL, write_av, read_write="write", risk="R1",
                verification_method="label_ids", **common),
            cap("gmail", "mark_read", Verb.UPDATE, write_av, read_write="write", risk="R0",
                verification_method="label_ids", **common),
            cap("gmail", "star", Verb.UPDATE, write_av, read_write="write", risk="R0",
                verification_method="label_ids", **common),
            cap("gmail", "trash", Verb.DELETE, write_av, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="trashed", **common),
            cap("gmail", "restore", Verb.UPDATE, write_av, read_write="write", risk="R1",
                verification_method="untrash", **common),
            cap("gmail", "watch", Verb.WATCH, read_av, read_write="read", risk="R0",
                verification_method="history_id", **common,
                notes="history.list / users.watch; no full-inbox scrape"),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        client = await self._client(ctx)
        if client is None:
            return OpResult(
                status=OpStatus.SERVICE_AUTH_REQUIRED,
                service="gmail",
                operation=operation,
                availability=Availability.CONNECTION_REQUIRED,
                error="gmail_oauth_required",
                diagnosis="oauth_expired",
            )
        try:
            return await self._run(client, operation, args, ctx)
        except Exception as exc:
            return OpResult(
                status=OpStatus.FAILED,
                service="gmail",
                operation=operation,
                availability=Availability.NATIVE,
                error="gmail_api_error",
                diagnosis=diagnose_oauth_error(exc),
            )

    async def _client(self, ctx: OpContext) -> GmailClient | None:
        if getattr(ctx, "lease", None) is not None and ctx.transport is not None:
            return GmailClient(lease=ctx.lease, transport=ctx.transport)
        if ctx.session is None:
            return None
        lease = await lease_for(ctx.session, "mail") or await lease_for(ctx.session, "gmail_ops")
        if lease is None:
            return None
        transport = args_transport(ctx) or HttpxGmailTransport()
        return GmailClient(lease=lease, transport=transport)

    async def _run(self, client: GmailClient, operation: str, args: dict[str, Any], ctx: OpContext) -> OpResult:
        if operation in {"search", "list_inbox"}:
            q = args.get("q")
            if not q:
                compiled = compile_gmail_query(str(args.get("text") or args.get("query") or "in:inbox"))
                q = compiled["q"] if operation == "search" else (compiled["q"] or "in:inbox")
            if operation == "list_inbox" and "in:inbox" not in str(q):
                q = f"in:inbox {q}".strip()
            found = await client.search(str(q), page_token=args.get("page_token"), limit=args.get("limit"))
            # Hydrate a bounded metadata page — never the whole mailbox.
            previews = []
            for mid in (found.get("ids") or [])[: min(10, int(args.get("hydrate") or 10))]:
                wrapped = await client.get_message(mid, format="metadata")
                content = wrapped.get("content") if isinstance(wrapped.get("content"), dict) else wrapped
                previews.append(
                    {
                        "id": content.get("id"),
                        "thread_id": content.get("thread_id"),
                        "subject": content.get("subject"),
                        "from": content.get("from"),
                        "date": content.get("date") or content.get("internal_date"),
                        "snippet": content.get("snippet"),
                        "unread": content.get("unread"),
                    }
                )
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED,
                service="gmail",
                operation=operation,
                availability=Availability.NATIVE,
                payload={"q": q, "ids": found.get("ids"), "previews": previews,
                         "next_page_token": found.get("next_page_token")},
                verification={"count": len(found.get("ids") or [])},
                taint={"origin": "EXTERNAL_CONTENT", "authority": "DATA"},
            )
        if operation == "read":
            wrapped = await client.get_message(str(args["message_id"]))
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED,
                service="gmail",
                operation=operation,
                availability=Availability.NATIVE,
                payload=wrapped,
                verification={"message_id": args.get("message_id")},
                taint=wrapped if isinstance(wrapped, dict) else None,
            )
        if operation == "read_thread":
            wrapped = await client.get_thread(str(args["thread_id"]))
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED,
                service="gmail",
                operation=operation,
                availability=Availability.NATIVE,
                payload=wrapped,
                verification={"thread_id": args.get("thread_id")},
                taint=wrapped if isinstance(wrapped, dict) else None,
            )
        if operation == "download_attachment":
            blob = await client.download_attachment(str(args["message_id"]), str(args["attachment_id"]))
            data = dict(blob)
            raw = data.pop("bytes", b"")
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED,
                service="gmail",
                operation=operation,
                availability=Availability.NATIVE,
                payload={**data, "bytes_present": bool(raw), "origin": "EXTERNAL_CONTENT"},
                verification={"sha256": data.get("sha256"), "size": data.get("size")},
            )
        if operation == "list_labels":
            labels = await client.list_labels()
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="gmail", operation=operation,
                            availability=Availability.NATIVE, payload={"labels": labels})
        if operation == "list_drafts":
            drafts = await client.list_drafts()
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="gmail", operation=operation,
                            availability=Availability.NATIVE, payload={"drafts": drafts})
        if operation == "draft":
            made = await client.create_draft(
                to=_as_list(args.get("to")),
                subject=str(args.get("subject") or ""),
                body=str(args.get("body") or ""),
                thread_id=args.get("thread_id"),
                cc=_as_list(args.get("cc")) or None,
                in_reply_to=args.get("in_reply_to"),
                references=args.get("references"),
                idempotency_key=ctx.idempotency_key or args.get("idempotency_key"),
            )
            return OpResult(
                status=OpStatus.PREPARED,
                service="gmail",
                operation=operation,
                availability=Availability.NATIVE,
                payload={**made, "sent": False},
                verification={"draft_id": made.get("draft_id")},
            )
        if operation in {"send", "reply", "reply_all", "forward"}:
            to = _as_list(args.get("to"))
            cc = _as_list(args.get("cc"))
            subject = str(args.get("subject") or "")
            body = str(args.get("body") or "")
            thread_id = args.get("thread_id")
            in_reply_to = args.get("in_reply_to")
            references = args.get("references")
            if operation in {"reply", "reply_all", "forward"} and args.get("message_id"):
                orig = await client.get_message(str(args["message_id"]))
                content = orig.get("content") if isinstance(orig.get("content"), dict) else orig
                thread_id = thread_id or content.get("thread_id")
                orig_from = str(content.get("from") or "")
                orig_to = str(content.get("to") or "")
                orig_cc = str(content.get("cc") or "")
                orig_subj = str(content.get("subject") or "")
                orig_mid = str(content.get("message_id_header") or "")
                if operation == "forward":
                    subject = subject or (f"Fwd: {orig_subj}" if orig_subj else "Fwd:")
                    quoted = str(content.get("text") or content.get("snippet") or "")[:4000]
                    body = body or f"\n\n---------- Forwarded message ----------\n{quoted}"
                    if not to:
                        return OpResult(status=OpStatus.CLARIFY, service="gmail", operation=operation,
                                        availability=Availability.NATIVE, error="missing_recipient",
                                        payload={"sent": False})
                else:
                    subject = subject or (f"Re: {orig_subj}" if orig_subj and not orig_subj.lower().startswith("re:") else orig_subj)
                    if not to:
                        to = _as_list(orig_from)
                    if operation == "reply_all":
                        extra = _as_list(orig_to) + _as_list(orig_cc)
                        for addr in extra:
                            if addr and addr.lower() not in {x.lower() for x in to}:
                                cc.append(addr)
                    in_reply_to = in_reply_to or orig_mid
                    references = references or orig_mid
            if not to:
                return OpResult(status=OpStatus.CLARIFY, service="gmail", operation=operation,
                                availability=Availability.NATIVE, error="missing_recipient",
                                payload={"sent": False})
            existing = args.get("existing_rfc822")
            if existing:
                found = await client.find_by_rfc822(str(existing))
                if found:
                    return OpResult(
                        status=OpStatus.COMPLETED_VERIFIED,
                        service="gmail",
                        operation=operation,
                        availability=Availability.NATIVE,
                        payload={"id": found, "sent": True, "idempotent_replay": True},
                        verification={"message_id": found, "duplicate_prevented": True},
                    )
            sent = await client.send_raw(
                to=to,
                subject=subject,
                body=body,
                thread_id=thread_id,
                cc=cc or None,
                in_reply_to=in_reply_to,
                references=references,
                idempotency_key=ctx.idempotency_key or args.get("idempotency_key"),
            )
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED,
                service="gmail",
                operation=operation,
                availability=Availability.NATIVE,
                payload=sent,
                verification={
                    "message_id": sent.get("id"),
                    "thread_id": sent.get("thread_id"),
                    "sent": True,
                },
            )
        if operation == "archive":
            data = await client.modify(str(args["message_id"]), remove=["INBOX"])
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="gmail", operation=operation,
                            availability=Availability.NATIVE, payload=data, verification=data)
        if operation == "mark_read":
            data = await client.modify(str(args["message_id"]), remove=["UNREAD"] if not args.get("unread") else None,
                                       add=["UNREAD"] if args.get("unread") else None)
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="gmail", operation=operation,
                            availability=Availability.NATIVE, payload=data, verification=data)
        if operation == "star":
            add = ["STARRED"] if not args.get("unstar") else None
            remove = ["STARRED"] if args.get("unstar") else None
            data = await client.modify(str(args["message_id"]), add=add, remove=remove)
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="gmail", operation=operation,
                            availability=Availability.NATIVE, payload=data, verification=data)
        if operation == "label":
            data = await client.modify(
                str(args["message_id"]),
                add=_as_list(args.get("add")),
                remove=_as_list(args.get("remove")),
            )
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="gmail", operation=operation,
                            availability=Availability.NATIVE, payload=data, verification=data)
        if operation == "trash":
            data = await client.trash(str(args["message_id"]))
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="gmail", operation=operation,
                            availability=Availability.NATIVE, payload=data, verification=data)
        if operation == "restore":
            data = await client.untrash(str(args["message_id"]))
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="gmail", operation=operation,
                            availability=Availability.NATIVE, payload=data, verification=data)
        if operation == "watch":
            hist = await client.history(str(args.get("history_id") or args.get("start_history_id") or "1"))
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="gmail", operation=operation,
                            availability=Availability.NATIVE, payload=hist, verification={"history_id": hist.get("history_id")})
        return OpResult(status=OpStatus.FAILED, service="gmail", operation=operation,
                        availability=Availability.NATIVE, error="unknown_operation")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def args_transport(ctx: OpContext) -> Any:
    extra = getattr(ctx, "transport", None)
    return extra
