"""Train the custom "EVIE" openWakeWord head and export ONNX.

This wraps openWakeWord's official training utility (the same path as
``automatic_model_training.ipynb``): a small head is trained on the frozen
shared feature extractor and the result is exported as ``.onnx``. The config
is generated from the installed package's own example schema so we never
fabricate a YAML contract; if the examples are missing, the script prints the
exact upstream command to run instead.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _find_package_files():
    try:
        import openwakeword
    except ImportError as exc:
        raise RuntimeError(
            "openwakeword is not installed (Agent 2 dependency request). "
            "It is required to train and export the custom EVIE head."
        ) from exc
    package_root = Path(openwakeword.__file__).resolve().parent
    train_py = package_root / "train.py"
    example = package_root.parent / "examples" / "custom_model.yml"
    return train_py, example


def _write_config(example: Path, out: Path, *, model_name: str, positive_dir: Path,
                  negative_dir: Path, output_dir: Path) -> Path:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("pyyaml is required to generate the training config") from exc
    with example.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    config["model_name"] = model_name
    config["output_directory"] = str(output_dir)
    if "training_data" in config:
        training_data = config["training_data"]
        if isinstance(training_data, dict):
            training_data["positive"] = str(positive_dir)
            training_data["negative"] = str(negative_dir)
    if "training_metadata" in config:
        metadata = config["training_metadata"]
        if isinstance(metadata, dict):
            metadata["model_name"] = model_name
    out.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-name", default="evie")
    parser.add_argument("--positive-dir", required=True, help="synthetic/real 'EVIE' clips")
    parser.add_argument("--negative-dir", default=None, help="non-wake speech/ambient clips")
    parser.add_argument("--output-dir", default="data/wake/model")
    args = parser.parse_args(argv)

    train_py, example = _find_package_files()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not example.is_file():
        print(
            "openwakeword installed without examples/custom_model.yml. Run the "
            "official training flow instead:",
            file=sys.stderr,
        )
        print(
            f"  python {train_py} --config <your custom_model.yml>",
            file=sys.stderr,
        )
        print("See https://github.com/dscripka/openWakeWord (automatic_model_training.ipynb).")
        return 2
    config_path = _write_config(
        example,
        output_dir / "custom_model.yml",
        model_name=args.model_name,
        positive_dir=Path(args.positive_dir),
        negative_dir=Path(args.negative_dir) if args.negative_dir else Path(args.positive_dir),
        output_dir=output_dir,
    )
    command = [sys.executable, str(train_py), "--config", str(config_path)]
    print("training with:", " ".join(command))
    subprocess.run(command, check=True)
    onnx = output_dir / f"{args.model_name}.onnx"
    if not onnx.is_file():
        print(f"training finished but {onnx} not found; check the log", file=sys.stderr)
        return 3
    print(f"exported head: {onnx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
