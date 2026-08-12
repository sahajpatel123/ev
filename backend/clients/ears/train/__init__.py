"""Training + evaluation tooling for the custom "EVIE" wake engine.

Pipeline (docs/AUDIO.md):
1. synthesize.py   — piper-sample-generator positives + RIR/noise augmentation
2. train_head.py   — small head on frozen openWakeWord features, export ONNX
3. train_verifier.py — logistic verifier on the human's own wake clips
4. tune_threshold.py — tune the runtime threshold against the 12 h ambient
5. evaluate.py     — acceptance metrics (recall, false accepts, VAD, scene)

All scripts require the human-collected clips + ambient recording; they fail
with explicit instructions when inputs are missing.
"""
