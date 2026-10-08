Synthesized 16 kHz mono PCM16 clips used by `test_voice_vosk.py`.

Generated with Piper (`en_US-lessac-medium`) so the grammar-restricted Vosk
spotter can be proven against real speech, not ASCII bytes in a WAV.

`acoustic_short.wav` / `acoustic_long.wav`: spoken prompts for the acoustic
flow gate (`scripts/acoustic_flow.py`), played through the Mac speakers into
the live Evie.app. Regenerate on macOS with:

    say -v Samantha -o /tmp/p.aiff "Hello Evie, please reply with a short greeting so I know you can hear me."
    afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/p.aiff backend/tests/fixtures/voice/acoustic_short.wav
    say -v Samantha -o /tmp/q.aiff "Evie, please explain in great detail how photosynthesis works, step by step, including the light reactions and the Calvin cycle."
    afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/q.aiff backend/tests/fixtures/voice/acoustic_long.wav
