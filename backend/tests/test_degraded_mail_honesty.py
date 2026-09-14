"""A failed Mac read must never be spoken as an empty inbox.

The peek helpers return a list for their callers, so a read failure and a
genuinely empty shelf both arrive as ``[]``. Before this, the spoken layer
rendered that ``[]`` as "I don't see new mail on this Mac right now" — a claim
about the owner's inbox that the code could not support.
"""

from __future__ import annotations

import types

import pytest

from app.memory import live_life
from app.memory.recall import _freshness_diag, _spoken_empty_connected

MAIL_ASK = "any new email"
CONTACTS_ASK = "Is Alex in my contacts?"


@pytest.fixture(autouse=True)
def _isolated_read_errors():
    live_life._READ_ERRORS.clear()
    yield
    live_life._READ_ERRORS.clear()


@pytest.fixture(autouse=True)
def _readable_index(monkeypatch):
    """A readable, freshly synced index: the empty-inbox path stays reachable."""
    import app.services.life_stream_daemon as daemon_mod

    fresh = {
        "ok": True,
        "mail": {"readable": True, "mtime_age_s": 60},
        "whatsapp": {"readable": True, "mtime_age_s": 60},
        "imessage": {"readable": True, "mtime_age_s": 60},
    }
    monkeypatch.setattr(daemon_mod, "life_freshness", lambda: fresh)


class _MailReadFailureDaemon:
    """Mail peeks raise, as a locked or unreadable Envelope Index would."""

    _cached_contacts: list = []

    def peek_mail(self, items=None, *, tokens=None, limit=8, query=""):
        raise RuntimeError("index locked while reading 'Invoice 4419 from Rahul'")

    def peek_whatsapp(self, *, tokens=None, limit=8):
        return []

    def peek_imessage(self, *, tokens=None, limit=8):
        return []

    def peek_calls(self, *, tokens=None, limit=8):
        return []

    def peek_photos(self, *, tokens=None, limit=8):
        return []

    def peek_contacts(self, contacts=None, *, tokens=None, limit=8):
        return []


class _EmptyMailDaemon(_MailReadFailureDaemon):
    def peek_mail(self, items=None, *, tokens=None, limit=8, query=""):
        return []


def test_failed_mail_read_is_not_spoken_as_an_empty_inbox() -> None:
    hits = live_life.peek_mac_life(
        MAIL_ASK, shelf="mail", tokens=["invoice"], daemon=_MailReadFailureDaemon()
    )

    assert hits == []  # callers still get the shape they depend on
    assert live_life.live_read_error("mail") == "RuntimeError"  # recorded, not swallowed

    spoken = _spoken_empty_connected(MAIL_ASK)
    assert "couldn't read" in spoken.lower()
    assert "i don't see new mail" not in spoken.lower()
    # Neither the error text nor invented mail content reaches the owner.
    assert "4419" not in spoken
    assert "rahul" not in spoken.lower()
    assert "invoice" not in spoken.lower()
    assert "couldn't read" in _freshness_diag("mail").lower()


def test_genuinely_empty_inbox_is_spoken_as_empty() -> None:
    hits = live_life.peek_mac_life(
        MAIL_ASK, shelf="mail", tokens=["invoice"], daemon=_EmptyMailDaemon()
    )

    assert hits == []
    assert live_life.live_read_error("mail") == ""

    spoken = _spoken_empty_connected(MAIL_ASK)
    assert spoken == "I don't see new mail on this Mac right now."
    assert "couldn't read" not in spoken.lower()


def test_read_failure_and_empty_inbox_speak_differently() -> None:
    live_life.peek_mac_life(MAIL_ASK, shelf="mail", daemon=_MailReadFailureDaemon())
    failed = _spoken_empty_connected(MAIL_ASK)

    live_life.peek_mac_life(MAIL_ASK, shelf="mail", daemon=_EmptyMailDaemon())
    empty = _spoken_empty_connected(MAIL_ASK)

    assert failed != empty
    assert "4419" not in failed and "4419" not in empty


def test_successful_read_clears_the_previous_failure() -> None:
    live_life.peek_mac_life(MAIL_ASK, shelf="mail", daemon=_MailReadFailureDaemon())
    assert live_life.live_read_error("mail")

    live_life.peek_mac_life(MAIL_ASK, shelf="mail", daemon=_EmptyMailDaemon())
    assert live_life.live_read_error("mail") == ""
    assert _spoken_empty_connected(MAIL_ASK) == "I don't see new mail on this Mac right now."


async def test_contacts_helper_failure_is_spoken_as_a_read_failure(monkeypatch) -> None:
    import app.integrations.life_helper as helper_mod
    from app.config import settings

    monkeypatch.setattr(settings, "life_helper_path", "/nonexistent/EVLifeHelper")

    async def _boom(command, args, *, helper_path=None, timeout=None):
        raise TimeoutError("EVLifeHelper timed out")

    monkeypatch.setattr(helper_mod, "run_life_helper", _boom)

    rows = await live_life._helper_account_rows(
        "contacts.resolve", "matches", args={"query": "Alex"}
    )
    assert rows == []
    assert live_life.live_read_error("contacts") == "TimeoutError"

    spoken = _spoken_empty_connected(CONTACTS_ASK)
    assert "couldn't read" in spoken.lower()
    assert "i don't see that contact" not in spoken.lower()


async def test_contacts_helper_bad_payload_is_a_read_failure(monkeypatch) -> None:
    import app.integrations.life_helper as helper_mod
    from app.config import settings

    monkeypatch.setattr(settings, "life_helper_path", "/nonexistent/EVLifeHelper")

    async def _malformed(command, args, *, helper_path=None, timeout=None):
        return types.SimpleNamespace(data={})

    monkeypatch.setattr(helper_mod, "run_life_helper", _malformed)

    assert await live_life._helper_account_rows("contacts.list", "contacts") == []
    assert live_life.live_read_error("contacts") == "bad_helper_payload"
    assert "couldn't read" in _spoken_empty_connected(CONTACTS_ASK).lower()


async def test_mail_helper_failure_does_not_blame_the_index(monkeypatch) -> None:
    """The helper is only a mail fallback: a slow helper must not turn a readable
    index's honest empty into "couldn't read mail"."""
    import app.integrations.life_helper as helper_mod
    from app.config import settings

    monkeypatch.setattr(settings, "life_helper_path", "/nonexistent/EVLifeHelper")

    async def _boom(command, args, *, helper_path=None, timeout=None):
        raise TimeoutError("EVLifeHelper timed out")

    monkeypatch.setattr(helper_mod, "run_life_helper", _boom)

    assert await live_life._helper_account_rows("mail.list", "messages", args={"limit": 8}) == []
    assert live_life.live_read_error("mail") == ""
    assert _spoken_empty_connected(MAIL_ASK) == "I don't see new mail on this Mac right now."
