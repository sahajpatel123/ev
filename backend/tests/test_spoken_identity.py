from app.ev.assistant import spoken_name
from app.ev.personality import identity_block, spoken_identity
from app.voice.live.grok_voice import openai_realtime_instructions


def test_default_identity_is_spelled_for_speech() -> None:
    assert spoken_name("EVIE") == "E V"
    assert spoken_name("EV") == "E V"
    assert spoken_identity("EVIE") == "E V"


def test_speech_prompts_pin_e_v_pronunciation() -> None:
    assert "two letter names E V" in identity_block("EVIE", "the owner's assistant")
    assert "never E-y or Evie" in openai_realtime_instructions()
