"""Train the custom logistic-regression verifier on the human's wake clips.

Uses openWakeWord's documented ``train_custom_verifier`` (see
``docs/custom_verifier_models.md``): a lightweight logistic regression over the
shared audio features that filters base-model activations to the target
speaker, which is the documented way to crush false accepts.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from clients.ears.train.common import iter_clips


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positive-dir", required=True, help="human 'EVIE' wake clips")
    parser.add_argument("--negative-dir", required=True, help="human non-wake speech + ambient")
    parser.add_argument(
        "--model-name",
        default="evie.onnx",
        help="base model path/name the verifier filters (must match the trained head)",
    )
    parser.add_argument("--output", default="data/wake/verifier/evie_verifier.pkl")
    args = parser.parse_args(argv)
    try:
        import openwakeword
    except ImportError as exc:
        raise RuntimeError(
            "openwakeword is not installed (Agent 2 dependency request)"
        ) from exc
    if not hasattr(openwakeword, "train_custom_verifier"):
        raise RuntimeError(
            "this openwakeword version has no train_custom_verifier; "
            "install openwakeword>=0.6"
        )
    positive = [str(p) for p in iter_clips(args.positive_dir)]
    negative = [str(p) for p in iter_clips(args.negative_dir)]
    if len(positive) < 3:
        raise SystemExit(
            f"need at least 3 positive wake clips, found {len(positive)}. "
            "Record 30 'EVIE' clips (10 at 3 m) first."
        )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    openwakeword.train_custom_verifier(
        positive_reference_clips=positive,
        negative_reference_clips=negative,
        output_path=str(output),
        model_name=args.model_name,
    )
    print(f"verifier saved: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
