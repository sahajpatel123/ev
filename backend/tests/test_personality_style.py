from app.ev.interaction import build_strategy, strategy_block
from app.ev.personality import (
    DEFAULT_PROFILE,
    SPEECH_STYLE_INSTRUCTIONS,
    identity_block,
)
from app.ev.turn import operator_instructions
from app.filter.output_filter import enforce_persona
from app.voice.live.grok_voice import grok_voice_instructions, openai_realtime_instructions


def test_default_personality_is_casual_direct_and_concise() -> None:
    assert DEFAULT_PROFILE["formality"] == 1
    assert DEFAULT_PROFILE["verbosity"] == 2
    assert DEFAULT_PROFILE["directness"] == 4


def test_shared_speech_style_reaches_owner_facing_paths() -> None:
    prompts = (
        identity_block("EVIE", "the owner's personal AI", compact=False),
        identity_block("EVIE", "the owner's personal AI", compact=True),
        strategy_block(build_strategy("What's next?")),
        operator_instructions(who="EVIE", source="voice"),
        grok_voice_instructions(),
        openai_realtime_instructions(),
    )
    for prompt in prompts:
        assert SPEECH_STYLE_INSTRUCTIONS in prompt
    assert "Say each point once" in SPEECH_STYLE_INSTRUCTIONS
    assert "one or two short sentences" in SPEECH_STYLE_INSTRUCTIONS


def test_output_filter_removes_immediate_prose_repeats() -> None:
    final, persona, flags = enforce_persona(
        "The file is saved. The file is saved. I kept the original.",
        build_strategy("Hi."),
    )
    assert final == "The file is saved. I kept the original."
    assert persona["repetitions_removed"] == 1
    assert any(flag.name == "repeated_sentence_removed" for flag in flags)


def test_speech_style_emphasizes_casual_concise_and_no_repetition() -> None:
    assert "casual" in SPEECH_STYLE_INSTRUCTIONS.lower()
    assert "concise" in SPEECH_STYLE_INSTRUCTIONS.lower()
    assert "do not speak too much" in SPEECH_STYLE_INSTRUCTIONS.lower()
    assert "never repeat" in SPEECH_STYLE_INSTRUCTIONS.lower()


def test_output_filter_removes_non_immediate_duplicate_sentences() -> None:
    final, persona, flags = enforce_persona(
        "The file is saved. We updated the readme. The file is saved.",
        build_strategy("Hi."),
    )
    assert final == "The file is saved. We updated the readme."
    assert persona["repetitions_removed"] == 1
    assert any(flag.name == "repeated_sentence_removed" for flag in flags)


def test_output_filter_removes_near_duplicate_adjacent_sentences() -> None:
    final, persona, flags = enforce_persona(
        "The deployment has finished successfully. The deployment has finished. Let's move on.",
        build_strategy("Hi."),
    )
    assert final == "The deployment has finished successfully. Let's move on."
    assert persona["repetitions_removed"] == 1


def test_output_filter_strips_question_echo_preamble() -> None:
    final1, _, _ = enforce_persona(
        "You asked what time the train leaves. The train leaves at 5.",
        build_strategy("What time does the train leave?"),
    )
    assert final1 == "The train leaves at 5."

    final2, _, _ = enforce_persona(
        "Regarding your question about the server, everything is healthy.",
        build_strategy("How is the server?"),
    )
    assert final2 == "everything is healthy."
