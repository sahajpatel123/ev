"""Guard tests: filler strip, emoji caps, flags, idempotence, confirmations."""

from __future__ import annotations

from app.persona.callbacks import InMemoryCallbackStore
from app.persona.context import PersonaContext
from app.persona.guard import persona_guard


def ctx(**kwargs) -> PersonaContext:
    kwargs.setdefault("channel", "text")
    return PersonaContext(**kwargs)


def test_strips_banned_openers() -> None:
    text, flags = persona_guard("Great question! The file is saved.", ctx())
    assert text == "The file is saved."
    assert "filler_stripped" in flags


def test_strips_banned_closers() -> None:
    text, flags = persona_guard(
        "The file is saved. Hope this helps!", ctx())
    assert text == "The file is saved."
    assert "filler_stripped" in flags


def test_keeps_legit_sentences_starting_near_banned_words() -> None:
    text, flags = persona_guard("Absolutely certain it failed twice.", ctx())
    assert text == "Absolutely certain it failed twice."
    assert "filler_stripped" not in flags


def test_emoji_capped_to_one_in_text_zero_in_voice() -> None:
    text, flags = persona_guard("done 🎉🎉 nice 🎉", ctx(channel="text"))
    assert text.count("🎉") == 1
    assert "emoji_capped" in flags
    voice, flags = persona_guard("done 🎉 nice", ctx(channel="voice"))
    assert "🎉" not in voice
    assert "emoji_capped" in flags


def test_humour_flagged_on_roast0_turn() -> None:
    _, flags = persona_guard("haha whoops 😂", ctx(confirmation_pending=False,
                                                 emotion="sad"))
    assert "humour_in_roast0" in flags


def test_no_humour_flag_when_roast_allowed() -> None:
    _, flags = persona_guard("haha nice", ctx(owner_msg="hi"))
    assert "humour_in_roast0" not in flags


def test_voice_first_sentence_length_flagged() -> None:
    long = " ".join(["word"] * 30) + ". Short tail."
    _, flags = persona_guard(long, ctx(channel="voice"))
    assert "voice_first_sentence_long" in flags
    _, flags = persona_guard("Short enough. Tail.", ctx(channel="voice"))
    assert "voice_first_sentence_long" not in flags


def test_repeated_line_flagged_with_store() -> None:
    store = InMemoryCallbackStore()
    store.record_line("You've announced a few.")
    _, flags = persona_guard(
        "You've announced a few. New work here.", ctx(), store=store)
    assert "repeated_line" in flags
    _, flags = persona_guard("Fresh material only here.", ctx(), store=store)
    assert "repeated_line" not in flags


def test_on_behalf_profanity_stripped_and_flagged() -> None:
    text, flags = persona_guard(
        "This damn delay is Hell to explain.", ctx(audience="on_behalf"))
    assert "damn" not in text.lower()
    assert "[redacted]" in text
    assert "on_behalf_profanity_stripped" in flags


def test_on_behalf_evie_isms_flagged_not_stripped() -> None:
    text, flags = persona_guard(
        "That's not working, that's a hostage situation.", ctx(audience="on_behalf"))
    assert "hostage situation" in text
    assert "on_behalf_voice_leak" in flags


def test_confirmation_text_never_altered() -> None:
    original = "Great question! This permanently deletes 3 files. haha extra 😂😂"
    text, flags = persona_guard(original, ctx(confirmation_pending=True))
    assert text == original
    assert "confirmation_extra_text" in flags


def test_cliche_opener_stripped_keeps_remainder() -> None:
    text, flags = persona_guard("Well, well, well. The build passed.", ctx())
    assert text == "The build passed."
    assert "cliche_stripped" in flags
    text, _ = persona_guard("Plot twist: it was the cache.", ctx())
    assert text == "It was the cache."


def test_cliche_mid_sentence_flagged_not_stripped() -> None:
    text, flags = persona_guard("It works, not gonna lie, finally.", ctx())
    assert "not gonna lie" in text
    assert "cliche_flagged" in flags


def test_cliche_lexicon_covers_brief_list() -> None:
    from app.persona.cliches import all_phrases

    phrases = " | ".join(all_phrases()).lower()
    for required in ("well, well, well", "plot twist", "buckle up",
                     "spoiler alert", "not gonna lie", "delve", "i hear you",
                     "game-changer", "oh boy", "classic you"):
        assert required in phrases, required


def test_idempotent() -> None:
    store = InMemoryCallbackStore()
    once, flags_once = persona_guard(
        "Great question! Done 🎉🎉", ctx(), store=store)
    twice, flags_twice = persona_guard(once, ctx(), store=store)
    assert twice == once
    assert flags_twice == []
    assert flags_once == ["filler_stripped", "emoji_capped"]
