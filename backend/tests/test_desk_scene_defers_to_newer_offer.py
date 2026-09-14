"""The owner's "yes" answers Evie's newest question, not a stale land offer.

The desk scene keeps a land-file offer of its own. When Evie has since asked
something newer (the live cognitive offer), the scene must not claim the reply.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.cognitive.intent import pending_offer, set_pending_offer
from app.cognitive.session_store import current
from app.ev import desk_scene
from app.ev.desk_scene import looks_like_scene_turn, parse_scene_goal

LANDED_PATH = "/tmp/desk-scene-defers-test/Sahaj_I-20.pdf"
SCENE_OFFER = "Sahaj_I-20.pdf just landed. Add it to the visa packet?"
CONFIRM = {"action": "confirm_land", "goal": "yes"}


@pytest.fixture
def scene_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(desk_scene, "memory_root", lambda: tmp_path)
    desk_scene.reset_desk_names()
    yield tmp_path
    desk_scene.reset_desk_names()


def _seed_scene_offer(*, touched_at: float) -> None:
    """A land-file offer exactly as scan_landed_files records it."""
    scene = desk_scene._empty_scene()
    scene["objects"] = [
        {
            "id": "landed-1",
            "kind": "doc",
            "path": LANDED_PATH,
            "aliases": ["i-20"],
            "inode": None,
            "name": "Sahaj_I-20.pdf",
            "members": [],
            "held_items": [],
            "source": "landed",
            "touched_at": touched_at,
        },
        {
            "id": "packet-1",
            "kind": "packet",
            "path": "",
            "aliases": ["visa packet"],
            "inode": None,
            "name": "visa packet",
            "members": [],
            "held_items": [],
            "source": "packet",
            "touched_at": touched_at,
        },
    ]
    scene["pending_offer"] = {
        "landed_id": "landed-1",
        "packet_id": "packet-1",
        "path": LANDED_PATH,
        "spoken": SCENE_OFFER,
    }
    desk_scene._save(scene)


def test_stale_scene_offer_defers_to_the_newer_question(scene_root: Path) -> None:
    _seed_scene_offer(touched_at=time.time() - 300)
    set_pending_offer(current(), "Do you want me to read out the full mail?")

    assert pending_offer(current()) is not None, "precondition: the newer question is live"
    assert parse_scene_goal("yes") is None, "the stale scene offer consumed the yes"
    assert looks_like_scene_turn("yes") is False
    # The Mac file-goal lane (the real consumer) declines the same way.
    from app.ev.laptop_files import parse_file_goal

    assert parse_file_goal("yes") is None
    # The scene offer is left intact; the newer offer owns the reply.
    assert desk_scene._load().get("pending_offer") is not None


def test_scene_only_offer_still_claims_the_yes(scene_root: Path) -> None:
    _seed_scene_offer(touched_at=time.time())

    assert pending_offer(current()) is None, "precondition: no newer question is live"
    assert parse_scene_goal("yes") == CONFIRM
    assert looks_like_scene_turn("yes") is True
    assert parse_scene_goal("add that") == {"action": "confirm_land", "goal": "add that"}


def test_stale_scene_offer_defers_add_and_file_phrasing(scene_root: Path) -> None:
    _seed_scene_offer(touched_at=time.time() - 300)
    set_pending_offer(current(), "Do you want me to read out the full mail?")

    for phrase in ("add that", "file them"):
        assert parse_scene_goal(phrase) is None, phrase


def test_unreadable_session_keeps_todays_behaviour(
    scene_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_scene_offer(touched_at=time.time() - 300)

    from app.cognitive import session_store

    def _unreadable():
        raise RuntimeError("session store unavailable")

    monkeypatch.setattr(session_store, "current", _unreadable)

    assert parse_scene_goal("yes") == CONFIRM
    assert looks_like_scene_turn("yes") is True


def test_scene_offer_newer_than_a_live_offer_still_claims(scene_root: Path) -> None:
    _seed_scene_offer(touched_at=time.time())
    session = current()
    session.constraints["pending_offer"] = {
        "text": "Do you want me to read out the full mail?",
        "at": "2000-01-01T00:00:00+00:00",
        "expires_at": time.time() + 600.0,
    }
    from app.cognitive.session_store import save

    save(session)

    assert parse_scene_goal("yes") == CONFIRM
