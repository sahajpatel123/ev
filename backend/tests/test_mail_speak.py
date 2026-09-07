"""Spoken mail is a short about-gist, not a body dump, for any phrasing."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from app.memory.mail_speak import (
    generated_summary_text,
    gist_from_preview,
    mail_selector,
    preview_from_emlx,
    shape_mail_payload,
    speak_mail,
)
from app.memory.recall import _spoken_from_evidence
from app.services.life_stream_daemon import LifeStreamDaemon


LONG_BODY = (
    "Please review the attached invoice for March and confirm the wire by Friday. "
    "The amount is forty two thousand dollars. "
    "Then a newsletter dump: " + ("lorem ipsum dolor sit amet. " * 80)
)


def _mail(
    subject: str,
    sender: str,
    *,
    preview: str = "",
    gist: str = "",
) -> dict:
    hit = {
        "subject": subject,
        "sender": sender,
        "received": "2026-09-05T10:00:00+00:00",
        "memory_type": "mail.envelope.received",
        "kind": "live_mac",
        "channel": "mail",
        "shelf": "mail",
    }
    if preview:
        hit["preview"] = preview
    if gist:
        hit["gist"] = gist
    return hit


def test_mail_selector_is_structural_not_a_phrase_list() -> None:
    digest = [
        "any new mail",
        "check my inbox",
        "what mails did I get",
        "did I get any email",
        "show me recent mail",
        "list my emails",
        "Find an email I received",
    ]
    particular = [
        "what was the email from Alex about",
        "summarise the mail from GitHub",
        "tell me about that email",
        "read me the latest mail",
        "what did Alex email me",
        "any mail from Ned about the invoice",
        "what's in Rahul's mail",
    ]
    for phrase in digest:
        assert mail_selector(phrase).particular is False, phrase
    for phrase in particular:
        assert mail_selector(phrase).particular is True, phrase
    from_alex = mail_selector("Did I get any email from Alex?")
    assert from_alex.who.lower() == "alex"
    assert "alex" in from_alex.tokens()
    about = mail_selector("any mail from GitHub about the invoice")
    assert "github" in about.tokens()
    assert "invoice" in about.tokens()


def test_gist_from_preview_never_returns_the_whole_body() -> None:
    gist = gist_from_preview(LONG_BODY, subject="March invoice")
    assert gist
    assert len(gist) <= 240
    assert LONG_BODY not in gist
    assert ("lorem ipsum dolor sit amet. " * 10) not in gist
    assert "invoice" in gist.lower() or "wire" in gist.lower() or "march" in gist.lower()
    html = gist_from_preview(
        "<html><body><p>Lunch is at noon at the cafe.</p>"
        "<p>Unsubscribe from this list.</p></body></html>",
        subject="Lunch",
    )
    assert "noon" in html.lower()
    assert "<p>" not in html
    assert "unsubscribe" not in html.lower()


def test_digest_speak_is_headlines_not_concatenated_bodies() -> None:
    items = [
        _mail("Lunch tomorrow", "Alex", preview=LONG_BODY),
        _mail("Your receipt", "Stripe <noreply@stripe.com>", preview=LONG_BODY),
        _mail("Design review", "Maya", preview=LONG_BODY),
        _mail("Spam blast", "Offers", preview=LONG_BODY),
    ]
    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    hits = daemon.peek_mail(items, query="any new mail", limit=8)
    spoken = _spoken_from_evidence(hits, "any new mail")
    assert spoken.lower().startswith("recent mail:")
    assert "lunch tomorrow" in spoken.lower()
    assert "alex" in spoken.lower()
    assert LONG_BODY not in spoken
    assert spoken.count("lorem ipsum") == 0
    assert "spam blast" not in spoken.lower()
    assert len(spoken) < 400


def test_particular_mail_speaks_a_short_about_gist() -> None:
    items = [
        _mail(
            "Lunch tomorrow",
            "Alex <alex@example.com>",
            preview="Can we move lunch to noon at the cafe? I'll book a table.",
        ),
        _mail("Your receipt", "Stripe", preview=LONG_BODY),
    ]
    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    phrases = [
        "what was the email from Alex about",
        "summarise the mail from Alex",
        "tell me about that email from Alex",
        "Did I get any email from Alex?",
        "what did Alex email me",
    ]
    for phrase in phrases:
        hits = daemon.peek_mail(items, query=phrase, limit=8)
        spoken = _spoken_from_evidence(hits, phrase)
        lowered = spoken.lower()
        assert "alex" in lowered, phrase
        assert LONG_BODY not in spoken
        assert "lorem ipsum" not in lowered
        assert "noon" in lowered or "lunch" in lowered or "cafe" in lowered
        assert not spoken.lower().startswith("recent mail on this mac:")
        assert len(spoken) < 400


def test_that_email_picks_the_latest_and_gists_it() -> None:
    items = [
        _mail(
            "Flight change",
            "Airline",
            preview="Your Friday flight to Delhi now leaves at 9pm. Check in online.",
        ),
        _mail("Old newsletter", "Shop", preview=LONG_BODY),
    ]
    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    hits = daemon.peek_mail(items, query="what was that email about", limit=8)
    spoken = _spoken_from_evidence(hits, "what was that email about")
    assert "flight" in spoken.lower() or "delhi" in spoken.lower() or "9pm" in spoken.lower()
    assert LONG_BODY not in spoken
    assert "lorem ipsum" not in spoken.lower()


def test_about_token_prefers_the_matching_mail() -> None:
    items = [
        _mail("Hello", "Alex", preview="Just saying hi, nothing else in this thread."),
        _mail(
            "Vendor note",
            "Accounts",
            preview="Please pay the invoice by Friday. The PO is 4419.",
        ),
    ]
    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    hits = daemon.peek_mail(items, query="any mail about the invoice", limit=8)
    spoken = _spoken_from_evidence(hits, "any mail about the invoice")
    assert "invoice" in spoken.lower() or "friday" in spoken.lower() or "4419" in spoken.lower()
    assert "just saying hi" not in spoken.lower()


def test_shape_mail_payload_drops_bodies_from_tool_json() -> None:
    payload = shape_mail_payload(
        {
            "ok": True,
            "messages": [
                {
                    "subject": "Lunch tomorrow",
                    "sender": "Alex",
                    "body": LONG_BODY,
                    "html": "<p>" + LONG_BODY + "</p>",
                }
            ],
        },
        "what was the email from Alex about",
    )
    dumped = str(payload)
    assert LONG_BODY not in dumped
    assert "html" not in payload["messages"][0]
    assert "body" not in payload["messages"][0]
    assert payload["spoken"]
    assert "lorem ipsum" not in payload["spoken"].lower()
    assert len(payload["spoken"]) <= 520


def test_emlx_preview_is_capped_plain_text(tmp_path: Path) -> None:
    message = EmailMessage()
    message["From"] = "Alex <alex@example.com>"
    message["Subject"] = "Lunch tomorrow"
    message.set_content(
        "Can we move lunch to noon?\n\n" + LONG_BODY + "\n-- \nAlex\n"
    )
    raw = str(len(message.as_bytes())).ljust(10) + "\n" + message.as_string()
    path = tmp_path / "12.emlx"
    path.write_text(raw, encoding="utf-8")
    preview = preview_from_emlx(path)
    gist = gist_from_preview(preview, subject="Lunch tomorrow")
    assert "noon" in gist.lower()
    assert LONG_BODY not in gist
    assert "unsubscribe" not in gist.lower()


def test_generated_summary_prefers_apple_topline() -> None:
    import plistlib
    from plistlib import UID

    blob = plistlib.dumps(
        {
            "$version": 100000,
            "$archiver": "NSKeyedArchiver",
            "$top": {
                "generatedSummary.synopsis": UID(0),
                "generatedSummary.topLine": UID(1),
            },
            "$objects": [
                "$null",
                {"NSString": UID(2), "$class": UID(3)},
                "Alex moved lunch to noon at the cafe.",
                {"$classname": "NSString", "$classes": ["NSString"]},
            ],
        },
        fmt=plistlib.FMT_BINARY,
    )
    text = generated_summary_text(blob)
    assert "noon" in text.lower()
    assert "alex" in text.lower()


def test_imessage_and_contacts_routing_untouched() -> None:
    from app.ev.tool_select import resolve_live_action, select_tool
    from app.memory.life_archive.locate import classify_shelf, life_channel

    assert life_channel("Who texted me?") == "imessage"
    assert life_channel("any new WhatsApp messages") == "whatsapp"
    assert life_channel("What's Rahul's email?") == "contacts"
    assert classify_shelf("What's Rahul's email?") == "contacts"
    assert select_tool("Call Ned").selected == "place_call"
    assert select_tool("Who texted me?").selected == "list_messages"
    assert resolve_live_action("check my inbox")[0] == "list_mail"
    chats = _spoken_from_evidence(
        [
            {
                "memory_type": "life.chat.thread",
                "text": "WhatsApp thread: Ada. 12 messages (2026).",
            }
        ],
        "tell me about my conversations with different people",
    )
    assert "Ada" in chats
    assert "recent mail" not in chats.lower()
