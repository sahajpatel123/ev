"""Chats and iMessage speak a header + gist, not the thread."""

from __future__ import annotations

from app.ev.spark_task import TaskDecision, fallback_task_decision
from app.memory.message_speak import shape_message_payload, speak_messages, speak_person_gist
from app.memory.recall import _spoken_from_evidence

LONG = (
    "Can you send the notes from standup when you get a minute? "
    "Also here is a dump: " + ("lorem ipsum dolor sit amet. " * 40)
)


def _imessage(who: str, body: str, *, me: bool = False) -> dict:
    speaker = "You" if me else who
    return {
        "text": f"{speaker}: {body}",
        "handle": who,
        "preview": body[:220],
        "kind": "live_mac",
        "memory_type": "message.imessage.sent" if me else "message.imessage.received",
        "channel": "imessage",
        "source": "imessage",
        "when": "2026-09-07T18:00:00+00:00",
    }


def test_latest_messages_are_headers_not_bodies() -> None:
    items = [
        _imessage("Mansi", LONG),
        _imessage("Puran", "Ok cool see you"),
        _imessage("Gopal", LONG),
    ]
    spoken = speak_messages(
        "what are my latest messages",
        items,
        decision=TaskDecision(family="messages", manner="digest", source="fallback"),
    )
    lowered = spoken.lower()
    assert lowered.startswith("latest messages:")
    assert "mansi" in lowered
    assert LONG not in spoken
    assert "lorem ipsum" not in lowered
    assert spoken.count("lorem ipsum") == 0
    assert fallback_task_decision("what are my latest messages", family_hint="messages").manner != "readout"


def test_digest_manner_is_headlines_even_when_latest_is_set() -> None:
    """`latest` on a digest is recency of the set, not a one-thread readout."""
    items = [
        _imessage("Mansi", "on my way"),
        _imessage("Puran", "ok cool"),
        _imessage("Gopal", "see you"),
    ]
    spoken = speak_messages(
        "recent messages",
        items,
        decision=TaskDecision(
            family="messages", manner="digest", latest=True, source="fallback"
        ),
    )
    lowered = spoken.lower()
    assert lowered.startswith("latest messages:")
    assert "mansi" in lowered
    assert "puran" in lowered
    assert "on my way" in lowered or "ok cool" in lowered


def test_last_chat_is_a_gist_not_a_recitation() -> None:
    beats = [
        {
            "title": "Mansi",
            "who": "Mansi",
            "body": "Let's meet after work about the project.",
            "when": "2026-09-07T18:00:00+00:00",
        },
        {
            "title": "Mansi",
            "who": "You",
            "body": "I'll be there after six.",
            "when": "2026-09-07T18:01:00+00:00",
        },
    ]
    spoken = speak_person_gist(
        "what did I talk with Mansi last",
        beats,
        ["Mansi"],
        channel="WhatsApp",
    ).lower()
    assert "mansi" in spoken
    assert "last you talked" in spoken
    assert "let's meet after work about the project" not in spoken
    assert "i'll be there after six" not in spoken
    assert "mansi said" not in spoken
    assert "you said" not in spoken
    evidence = _spoken_from_evidence(
        [
            {
                "memory_type": "life.chat.excerpt",
                "text": "WhatsApp with Mansi — Mansi: Let's meet after work about the project.",
                "when": "2026-09-07T18:00:00+00:00",
            }
        ],
        "what did I talk with Mansi last",
    ).lower()
    assert "let's meet after work about the project" not in evidence
    assert "mansi said" not in evidence


def test_readout_is_the_only_path_that_speaks_the_words() -> None:
    items = [_imessage("Mansi", "Bring the charger when you come over.")]
    spoken = speak_messages(
        "read that message out",
        items,
        decision=TaskDecision(family="messages", manner="readout", latest=True, source="spark"),
    ).lower()
    assert spoken.startswith("reading it out")
    assert "charger" in spoken
    shaped = shape_message_payload(
        {
            "messages": items,
            "spoken": speak_messages(
                "latest messages",
                items,
                decision=TaskDecision(family="messages", manner="digest"),
            ),
        },
        "latest messages",
    )
    public = str(shaped["messages"][0]["text"])
    assert "—" in public
    assert len(public) <= 160


def _bare_adapter_row(who: str, body: str, rowid: int) -> dict:
    """The CONDUIT messages-adapter row: handle + text, no kind/channel."""
    return {
        "id": f"msg-{rowid}",
        "date": "2026-10-07T10:00:00+00:00",
        "handle": who,
        "text": body,
    }


def test_digest_speaks_bare_adapter_rows() -> None:
    """19 bare rows must not collapse to a one-line digest (live catch)."""
    from app.memory.message_speak import is_chat_hit

    items = [
        _bare_adapter_row("Mansi", "on my way home now", 1),
        _bare_adapter_row("Puran", "ok cool see you there", 2),
        _bare_adapter_row("Gopal", "running ten minutes late", 3),
    ]
    assert all(is_chat_hit(item) for item in items)
    spoken = speak_messages(
        "any new messages",
        items,
        decision=TaskDecision(family="messages", manner="digest", source="fallback"),
    )
    lowered = spoken.lower()
    assert lowered.startswith("latest messages:")
    assert "mansi" in lowered
    assert "puran" in lowered
    assert "gopal" in lowered


def test_mail_row_with_handle_is_not_chat() -> None:
    """A mail envelope carrying handle+text stays mail, never chat."""
    from app.memory.message_speak import is_chat_hit

    assert (
        is_chat_hit(
            {
                "memory_type": "mail.envelope.received",
                "channel": "mail",
                "sender": "ops@example.com",
                "handle": "ops@example.com",
                "text": "Deploy finished",
            }
        )
        is False
    )


def test_pick_latest_returns_newest_by_when() -> None:
    """A `latest` particular must pick the newest line, not rows[0]."""
    from app.memory.message_speak import _pick

    old = _imessage("Mansi", "older line")
    old["when"] = "2026-09-01T10:00:00+00:00"
    new = _imessage("Puran", "newer line")
    new["when"] = "2026-10-08T10:00:00+00:00"
    picked = _pick([old, new], who="", latest=True)
    assert picked is not None
    assert "newer line" in str(picked.get("preview") or picked.get("text"))
    spoken = speak_messages(
        "latest message",
        [old, new],
        decision=TaskDecision(family="messages", manner="particular", latest=True, source="fallback"),
    )
    assert "Puran" in spoken
