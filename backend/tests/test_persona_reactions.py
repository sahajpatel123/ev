"""Reaction classifier tests: 60 + 80 labelled fixtures, precision >= 0.9.

Each entry is (owner message, expected label, optional prev_meta).
Labels: positive | neutral | negative.
"""

from __future__ import annotations

import pytest

from app.persona.reactions import (
    banter_invited,
    classify_reaction,
    hinglish,
    roast_me_anyway,
    topic_of,
)

FIXTURE: list[tuple[str, str, dict | None]] = [
    # --- positive (20): laughs, banters back, plays along ---
    ("haha", "positive", None),
    ("lol that's fair", "positive", None),
    ("lmao ok you got me", "positive", None),
    ("hahaha stop, I'm dying", "positive", None),
    ("good one 😂", "positive", None),
    ("nice one, I'll give you that", "positive", None),
    ("says you 😂", "positive", None),
    ("oh please, like you're perfect", "positive", None),
    ("fair", "positive", None),
    ("bet", "positive", None),
    ("touché", "positive", None),
    ("look who's talking", "positive", None),
    ("nice try, evie", "positive", None),
    ("that was good, not gonna lie", "positive", None),
    ("fight me 😤", "positive", None),
    ("jk jk, do the boring version", "positive", None),
    ("hehe ok fine you win this round", "positive", None),
    ("I'm dead 💀", "positive", None),
    ("ok that was funny", "positive", None),
    ("says the machine with no legs", "positive", None),
    # --- negative (20): shut it down, redirect, answer annoyed ---
    ("not now", "negative", None),
    ("focus", "negative", None),
    ("that's not funny", "negative", None),
    ("shut up and run it", "negative", None),
    ("stop joking, just fix it", "negative", None),
    ("enough", "negative", None),
    ("whatever", "negative", None),
    ("just do it", "negative", None),
    ("never mind, I'll do it myself", "negative", None),
    ("drop it", "negative", None),
    ("this isn't funny, I lost the file", "negative", None),
    ("too soon", "negative", None),
    ("read the room", "negative", None),
    ("I'm serious right now", "negative", None),
    ("I'm not in the mood", "negative", None),
    ("that's enough", "negative", None),
    ("can this wait", "negative", None),
    ("send the memo again", "negative", {"owner_request": "send the memo to sam"}),
    ("run the tests again", "negative", {"owner_request": "please run the tests again"}),
    ("stop", "negative", None),
    # --- neutral (20): just continues, incl. tricky near-misses ---
    ("ok", "neutral", None),
    ("thanks, moving on", "neutral", None),
    ("stop by the store on your way out", "neutral", None),
    ("do we have enough time to finish?", "neutral", None),
    ("focus on the API design first", "neutral", None),
    ("whatever works for you is fine", "neutral", None),
    ("fair enough point about the API, let me think", "neutral", None),
    ("run the migration", "neutral", None),
    ("what's the weather tomorrow", "neutral", None),
    ("remind me at five", "neutral", None),
    ("lollipop costs aside, what's the budget", "neutral", None),
    ("shell script is ready for review", "neutral", None),
    ("hello there", "neutral", None),
    ("yes", "neutral", None),
    ("no, use the other branch", "neutral", None),
    ("continue", "neutral", None),
    ("show me the diff", "neutral", None),
    ("how long will the build take", "neutral", None),
    ("hmm, not sure about that approach", "neutral", None),
    ("got it", "neutral", None),
]


FIXTURE_2: list[tuple[str, str, dict | None]] = [
    # --- positive (30): emoji, Hinglish praise, laugh context ---
    ("😂", "positive", None),
    ("🤣🤣🤣", "positive", None),
    ("💀", "positive", None),
    ("that killed me 💀", "positive", None),
    ("bro 😭", "positive", None),
    ("haha 😭", "positive", None),
    ("lol 😭 ok", "positive", None),
    ("sahi", "positive", None),
    ("sahi hai 😂", "positive", None),
    ("mast", "positive", None),
    ("ekdum mast", "positive", None),
    ("kya baat", "positive", None),
    ("kya baat hai bhai", "positive", None),
    ("ekdum", "positive", None),
    ("dead", "positive", None),
    ("fair 😂", "positive", None),
    ("touché 😂", "positive", None),
    ("haha very good", "positive", None),
    ("😭😭 bro that was good", "positive", None),
    ("lmao dead", "positive", None),
    ("hehe mast", "positive", None),
    ("sahi, fair", "positive", None),
    ("ekdum sahi", "positive", None),
    ("haha kya baat", "positive", None),
    ("💀💀💀", "positive", None),
    ("ok lol", "positive", None),
    ("rofl mast", "positive", None),
    ("heh fair", "positive", None),
    ("good one 💀", "positive", None),
    ("you're killing me 😭", "positive", None),
    # --- negative (30): sarcasm, shutdowns, Hinglish stops ---
    ("haha very funny", "negative", None),
    ("lol very funny. stop", "negative", None),
    ("ha so funny, I swear", "negative", None),
    ("hehe sure 🙄", "negative", None),
    ("just answer", "negative", None),
    ("just answer the question", "negative", None),
    ("bas", "negative", None),
    ("bas kar", "negative", None),
    ("bas bhai, focus", "negative", None),
    ("chup", "negative", None),
    ("chup kar yaar", "negative", None),
    ("seriously", "negative", None),
    ("ok seriously.", "negative", None),
    ("not now, focus", "negative", None),
    ("stop it, seriously", "negative", None),
    ("bas. just do it", "negative", None),
    ("chup, not funny", "negative", None),
    ("haha very funny, now focus", "negative", None),
    ("lol sure whatever", "negative", None),
    ("seriously?", "negative", None),
    ("enough. seriously", "negative", None),
    ("bas karo focus", "negative", None),
    ("just answer, no jokes", "negative", None),
    ("chup chaap run it", "negative", None),
    ("arey bas kar", "negative", None),
    ("haha so funny. drop it", "negative", None),
    ("very funny. seriously", "negative", None),
    ("stop. bas.", "negative", None),
    ("focus, chup", "negative", None),
    ("not now seriously", "negative", None),
    # --- neutral (20): ambiguous emoji, distress, near-misses ---
    ("😭", "neutral", None),
    ("I'm so tired 😭", "neutral", None),
    ("sad day 😭", "neutral", None),
    ("stressed about the deploy 😭", "neutral", None),
    ("my server is 💀", "neutral", None),
    ("seriously, thank you", "neutral", None),
    ("are you serious? that's great", "neutral", None),
    ("the report is due, focus on the API", "neutral", None),
    ("stop by the store", "neutral", None),
    ("bas theek hai", "neutral", None),
    ("that meeting was funny", "neutral", None),
    ("very funny story", "neutral", None),
    ("sarcasm noted", "neutral", None),
    ("ok noted", "neutral", None),
    ("mast plan, tell me more", "neutral", None),
    ("sahi time pe aaja", "neutral", None),
    ("deadline is Friday", "neutral", None),
    ("ekdum time waste", "neutral", None),
    ("kya baat kar rahe ho", "neutral", None),
    ("fair enough, continue", "neutral", None),
]


def test_fixture_has_60_messages() -> None:
    assert len(FIXTURE) == 60


def test_fixture_2_has_80_messages() -> None:
    assert len(FIXTURE_2) == 80


@pytest.mark.parametrize("msg,expected,meta", FIXTURE_2)
def test_fixture_2_labels(msg: str, expected: str, meta: dict | None) -> None:
    assert classify_reaction(msg, meta) == expected


@pytest.mark.parametrize("msg,expected,meta", FIXTURE)
def test_fixture_labels(msg: str, expected: str, meta: dict | None) -> None:
    assert classify_reaction(msg, meta) == expected


def _precision(label: str) -> float:
    both = FIXTURE + FIXTURE_2
    predicted = [
        (msg, meta) for msg, _, meta in both
        if classify_reaction(msg, meta) == label
    ]
    if not predicted:
        return 1.0
    expected = {(msg, repr(meta)) for msg, want, meta in both if want == label}
    hits = sum(1 for msg, meta in predicted if (msg, repr(meta)) in expected)
    return hits / len(predicted)


def test_positive_precision_at_least_09() -> None:
    assert _precision("positive") >= 0.9


def test_negative_precision_at_least_09() -> None:
    assert _precision("negative") >= 0.9


def test_negative_wins_over_positive_on_conflict() -> None:
    assert classify_reaction("haha, not now seriously") == "negative"


def test_empty_is_neutral() -> None:
    assert classify_reaction("") == "neutral"
    assert classify_reaction(None) == "neutral"


def test_roast_me_anyway_detector() -> None:
    for msg in (
        "roast me",
        "roast me anyway",
        "go on, roast me",
        "lay it on me",
        "I can take it",
        "don't hold back",
        "let me have it",
        "give me your worst",
    ):
        assert roast_me_anyway(msg) is True, msg
    for msg in ("roasted vegetables for dinner", "hit me up later", "coast to coast", ""):
        assert roast_me_anyway(msg) is False, msg


def test_banter_invited() -> None:
    assert banter_invited("haha nice") is True
    assert banter_invited("says you") is True
    assert banter_invited("run the tests") is False
    assert banter_invited("") is False


def test_hinglish_detector() -> None:
    assert hinglish("bhai yeh bug fix hi nahi ho raha") is True
    assert hinglish("yaar kal baat karte hai") is True
    assert hinglish("यह काम हो गया") is True
    for msg in ("hi there", "na na", "yeah no", "run it", "", "the rain in spain"):
        assert hinglish(msg) is False, msg


def test_topic_of_buckets() -> None:
    assert topic_of("this bug makes no sense") == "code"
    assert topic_of("should I go to the gym") == "habit"
    assert topic_of("microservices vs monolith?") in ("opinion", "code")
    assert topic_of("so tired tonight") == "sleep"
    assert topic_of("hello") == "general"
    assert topic_of(None) == "general"
