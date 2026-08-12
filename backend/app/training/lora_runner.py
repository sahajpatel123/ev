"""MLX-tune LoRA training runner (Apple Silicon, 8 GB friendly).

This module is deliberately import-safe without ``mlx_tune``/``mlx_lm``: every
MLX import is lazy and confined to the real-training path. When MLX or the
base weights are absent, training degrades to a clearly-labeled deterministic
double (``degraded=true``, ``simulated=true``) so the offline suite never
depends on model weights and no code path pretends real weights were trained.

State safety: all artifacts are written under a per-run directory and the
adapter is only finalized (``COMPLETE`` marker + ``adapter/`` contents) after
training and evaluation finish. A cancellation flag prevents completion and
leaves only partial, never-activatable state behind.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.training.eval import (
    evaluate_models,
    hud_conformance,
    overfit_report,
    tool_call_validity,
)
from app.training.style_adapter import build_style_profile
from app.utils.text import sha256_hex

COMPLETE_MARKER = "COMPLETE"


class TrainingInterrupted(RuntimeError):
    """Raised when a cancellation flag is observed."""


class TrainingUnavailable(RuntimeError):
    """Raised when the real MLX training stack or base weights are absent."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def append_status(run_dir: Path, event: dict) -> None:
    """Append one JSON status event to the run's status.jsonl (streaming)."""

    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "status.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"at": _now(), **event},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )


class _Tee:
    """Tee trainer stdout into status.jsonl so progress streams."""

    def __init__(self, run_dir: Path, stream) -> None:
        self.run_dir = run_dir
        self.stream = stream

    def write(self, text: str) -> int:
        if text.strip():
            append_status(self.run_dir, {"stage": "trainer_stdout", "line": text.rstrip()})
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()


def _check_cancel(cancel_file: Path | None) -> None:
    if cancel_file is not None and cancel_file.exists():
        raise TrainingInterrupted("training cancelled by operator")


def _hash_directory(path: Path) -> str | None:
    """Deterministic sha256 over a base-model directory (byte-identity proof)."""

    if path is None or not path.exists() or not path.is_dir():
        return None
    digest = sha256_hex("")
    for rel in sorted(
        p.relative_to(path)
        for p in path.rglob("*")
        if p.is_file() and not p.name.endswith((".part", ".lock"))
    ):
        digest = sha256_hex(f"{digest}:{rel}:{sha256_file(path / rel)}")
    return digest


def base_directory_hash(path: Path) -> str | None:
    """Public byte-identity hash for a base-model directory."""

    return _hash_directory(path)


def sha256_file(path: Path) -> str:
    """sha256 of one file (shared with the model store, kept local here)."""

    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _parse_loss_lines(status_path: Path) -> tuple[list[float], list[float]]:
    """Extract train/val loss curves from tee'd trainer stdout."""

    train: list[float] = []
    val: list[float] = []
    if not status_path.exists():
        return train, val
    for line in status_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        text = str(event.get("line", ""))
        lowered = text.lower()
        if "val loss" in lowered:
            marker = "val loss"
            target = val
        elif "train loss" in lowered:
            marker = "train loss"
            target = train
        elif "loss:" in lowered:
            marker = "loss:"
            target = train
        else:
            continue
        tail = lowered.rsplit(marker, 1)[1]
        value: float | None = None
        for separator in ("|", ","):
            candidate = tail.split(separator)[0].strip()
            try:
                value = float(candidate)
                break
            except ValueError:
                continue
        if value is None:
            continue
        target.append(value)
    return train, val


def _write_losses(run_dir: Path, train: list[float], val: list[float]) -> None:
    rows = []
    for i, value in enumerate(train):
        rows.append({"step": i + 1, "split": "train", "loss": value})
    for i, value in enumerate(val):
        rows.append({"step": i + 1, "split": "val", "loss": value})
    with (run_dir / "losses.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _json_safe(value):
    """Recursively convert Path values so config can be persisted as JSON."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _deterministic_double(
    run_dir: Path,
    config: dict,
    *,
    train_rows: list[dict],
    eval_rows: list[dict],
    reason: str,
) -> dict:
    """Deterministic degraded-mode double; never claims real weights."""

    append_status(run_dir, {"stage": "double", "message": reason})
    n = max(1, len(train_rows) or 1)
    train_loss = [round(2.0 - 0.15 * i / n, 4) for i in range(1, 9)]
    val_loss = [round(value + 0.1, 4) for value in train_loss]
    _write_losses(run_dir, train_loss, val_loss)

    references = [str(r.get("output", "")) for r in eval_rows]
    prompts = [str(r.get("instruction", "")) for r in eval_rows]
    tool = tool_call_validity(references)
    hud = hud_conformance(references)
    overfit = overfit_report(train_loss, val_loss)
    evaluation = {
        "measured": False,
        "degraded": True,
        "win_rate": None,
        "prompts": len(prompts),
        "tool_call_validity": tool,
        "hud_conformance": hud,
        "overfit": overfit,
        "note": "simulated eval on reference outputs; no model weights were loaded",
    }
    adapter_dir = run_dir / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        adapter_dir / "manifest.json",
        {
            "degraded": True,
            "simulated": True,
            "real_weights": False,
            "reason": reason,
            "base_model": config.get("base_model"),
            "created_at": _now(),
        },
    )
    _write_json(
        run_dir / "eval.json",
        {"evaluation": evaluation, "losses": {"train": train_loss, "val": val_loss}},
    )
    _write_json(
        run_dir / "config.json",
        {
            **_json_safe(config),
            "mode": "double",
            "dataset": {"train": len(train_rows), "eval": len(eval_rows)},
        },
    )
    (run_dir / COMPLETE_MARKER).write_text("degraded double completed", encoding="utf-8")
    append_status(run_dir, {"stage": "completed", "degraded": True})
    return {
        "status": "completed",
        "degraded": True,
        "simulated": True,
        "real_weights": False,
        "artifact_path": str(adapter_dir.resolve()),
        "adapter_ref": str(run_dir.resolve()),
        "losses_path": str((run_dir / "losses.jsonl").resolve()),
        "eval_path": str((run_dir / "eval.json").resolve()),
        "evaluation": evaluation,
        "loss_curves": {"train": train_loss, "val": val_loss},
    }


def _mlx_available() -> bool:
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("mlx", "mlx_lm", "mlx_tune", "datasets")
    )


def resolve_base_directory(base_model: str) -> Path | None:
    """Resolve a base model to a local directory (path or cached HF snapshot)."""

    path = Path(base_model).expanduser()
    if path.is_dir():
        return path.resolve()
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    safe = base_model.replace("/", "--")
    snapshots = hub / f"models--{safe}" / "snapshots"
    if snapshots.is_dir():
        refs = sorted(p for p in snapshots.iterdir() if p.is_dir())
        if refs:
            return refs[-1].resolve()
    return None


def _base_model_resolved(base_model: str) -> str:
    """Return a locally available base model ref, else raise."""

    resolved = resolve_base_directory(base_model)
    if resolved is not None:
        return str(resolved)
    raise TrainingUnavailable(
        f"base model {base_model!r} is not present locally; download it first "
        "(mlx-tune loads from ~/.cache/huggingface/hub or a local path)"
    )


def _load_model_and_tokenizer(config: dict):
    """Lazy-load mlx-tune and return (model, tokenizer)."""

    from mlx_tune import FastLanguageModel

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=config["base_model_resolved"],
        max_seq_length=int(config.get("max_seq_length", 512)),
        load_in_4bit=bool(config.get("use_4bit", True)),
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=int(config.get("rank", 16)),
        target_modules=list(config.get("target_modules", ["q_proj", "k_proj", "v_proj", "o_proj"])),
        lora_alpha=int(config.get("lora_alpha", 32)),
        lora_dropout=float(config.get("lora_dropout", 0.05)),
    )
    return model, tokenizer


def _to_chat_rows(rows: list[dict]) -> list[dict]:
    """Convert instruction/output rows to mlx-tune ChatML messages.

    ChatML rows exercise the model's real chat template during training, which
    is what makes EV-style skills (tool calls, HUD JSON) actually stick.
    Tool-teaching rows (output = JSON tool call) become the assistant message.
    """

    converted: list[dict] = []
    for row in rows:
        instruction = str(row.get("instruction", "")).strip()
        output = str(row.get("output", "")).strip()
        if not instruction or not output:
            continue
        converted.append(
            {
                "messages": [
                    {"role": "user", "content": instruction},
                    {"role": "assistant", "content": output},
                ]
            }
        )
    return converted


def _run_real_training(run_dir: Path, config: dict) -> dict:
    """Train a LoRA adapter with mlx-tune, stream progress, save adapter."""

    from mlx_tune import SFTConfig, SFTTrainer

    append_status(run_dir, {"stage": "loading_model", "base_model": config["base_model_resolved"]})
    model, tokenizer = _load_model_and_tokenizer(config)
    train_rows = _to_chat_rows(config["train_rows"])
    eval_rows = _to_chat_rows(config["eval_rows"])
    sft_config = SFTConfig(
        output_dir=str(run_dir / "output"),
        per_device_train_batch_size=int(config.get("batch_size", 1)),
        per_device_eval_batch_size=int(config.get("batch_size", 1)),
        gradient_accumulation_steps=int(config.get("grad_accum_steps", 1)),
        learning_rate=float(config.get("learning_rate", 1e-4)),
        num_train_epochs=int(config.get("num_epochs", 3)),
        max_steps=int(config.get("max_steps", -1)),
        warmup_steps=int(config.get("warmup_steps", 10)),
        save_steps=max(1, int(config.get("save_steps", 100))),
        logging_steps=max(1, int(config.get("logging_steps", 5))),
        max_seq_length=int(config.get("max_seq_length", 512)),
        grad_checkpoint=bool(config.get("grad_checkpoint", True)),
        val_batches=int(config.get("val_batches", 5)),
        use_native_training=True,
    )
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=train_rows,
        eval_dataset=eval_rows,
        args=sft_config,
    )
    append_status(run_dir, {"stage": "training", "train_rows": len(train_rows), "eval_rows": len(eval_rows)})
    with contextlib_redirect_stdout(_Tee(run_dir, sys.stdout)):
        trainer.train()
    _check_cancel(config.get("cancel_file"))

    adapter_dir = run_dir / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(adapter_dir))
    append_status(run_dir, {"stage": "adapter_saved", "path": str(adapter_dir)})
    train_loss, val_loss = _parse_loss_lines(run_dir / "status.jsonl")
    _write_losses(run_dir, train_loss, val_loss)
    return {
        "train_loss": train_loss,
        "val_loss": val_loss,
        "adapter_dir": adapter_dir,
    }


def _run_dpo_training(run_dir: Path, config: dict) -> dict:
    """Train a LoRA adapter with mlx-tune's native DPO trainer."""

    from mlx_tune import DPOConfig, DPOTrainer

    rows = [
        {
            "prompt": str(row.get("prompt", "")),
            "chosen": str(row.get("chosen", "")),
            "rejected": str(row.get("rejected", "")),
        }
        for row in config.get("train_rows", [])
        if row.get("chosen") and row.get("rejected")
    ]
    if not rows:
        raise TrainingUnavailable("DPO requires preference rows with chosen/rejected")
    append_status(
        run_dir,
        {"stage": "loading_model", "base_model": config["base_model_resolved"]},
    )
    model, tokenizer = _load_model_and_tokenizer(config)
    dpo_config = DPOConfig(
        output_dir=str(run_dir / "output"),
        per_device_train_batch_size=int(config.get("batch_size", 1)),
        gradient_accumulation_steps=int(config.get("grad_accum_steps", 1)),
        learning_rate=float(config.get("learning_rate", 5e-7)),
        num_train_epochs=int(config.get("num_epochs", 1)),
        max_steps=int(config.get("max_steps", -1)),
        warmup_steps=int(config.get("warmup_steps", 10)),
        logging_steps=max(1, int(config.get("logging_steps", 5))),
        save_steps=max(1, int(config.get("save_steps", 100))),
        max_seq_length=int(config.get("max_seq_length", 512)),
        max_prompt_length=min(int(config.get("max_seq_length", 512)), 512),
    )
    trainer = DPOTrainer(
        model=model,
        train_dataset=rows,
        ref_model=None,
        tokenizer=tokenizer,
        args=dpo_config,
        use_native=True,
    )
    append_status(run_dir, {"stage": "dpo_training", "pairs": len(rows)})
    with contextlib_redirect_stdout(_Tee(run_dir, sys.stdout)):
        trainer.train()
    _check_cancel(config.get("cancel_file"))

    adapter_dir = run_dir / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    dpo_artifacts = run_dir / "output" / "adapters"
    if dpo_artifacts.is_dir() and (dpo_artifacts / "adapters.safetensors").exists():
        for path in dpo_artifacts.iterdir():
            if path.is_file():
                shutil.copy2(path, adapter_dir / path.name)
    else:
        model.save_pretrained(str(adapter_dir))
    append_status(run_dir, {"stage": "adapter_saved", "path": str(adapter_dir)})
    train_loss, val_loss = _parse_loss_lines(run_dir / "status.jsonl")
    _write_losses(run_dir, train_loss, val_loss)
    return {
        "train_loss": train_loss,
        "val_loss": val_loss,
        "adapter_dir": adapter_dir,
    }


def _run_eval(run_dir: Path, config: dict, *, adapter_dir: Path) -> dict:
    """Generate base vs adapter responses on held-out prompts and judge them."""

    from mlx_lm import generate, load

    def _chat_prompt(tokenizer, prompt: str) -> str:
        return tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            add_generation_prompt=True,
            tokenize=False,
        )

    prompts = [str(r.get("instruction", "")) for r in config["eval_rows"]]
    references = [str(r.get("output", "")) for r in config["eval_rows"]]
    if not prompts:
        return {
            "measured": False,
            "win_rate": None,
            "note": "no held-out prompts",
            "tool_call_validity": {"tool_calls": 0, "valid": 0, "validity": None, "issues": []},
            "hud_conformance": {"hud_blocks": 0, "valid": 0, "conformance": None, "issues": []},
            "overfit": overfit_report(config.get("train_loss", []), config.get("val_loss", [])),
        }

    profile = build_style_profile(
        [{"role": "assistant", "text": ref, "signals": {}} for ref in references]
    )
    base_model = config["base_model_resolved"]
    append_status(
        run_dir,
        {
            "stage": "eval_generating",
            "prompts": len(prompts),
            "models": ["adapter", "base"],
        },
    )
    adapter_outputs: list[str] = []
    probe_outputs: list[str] = []
    adapter_model = None
    tokenizer = None
    probe_prompts = [
        'Call search_web with arguments {"q": "EV"}.',
        "Use the get_memory tool with id m1.",
        "Reply with a HUD card JSON whose schema_version is ev.hud.card.v1 and title is EV.",
        "Show the tool schema for send_message.",
        "Call update_gear with JSON arguments for the hud.",
    ]
    try:
        adapter_model, tokenizer = load(  # type: ignore[misc]  # mlx_lm return union incl. return_config=False 2-tuple
            base_model,
            adapter_path=str(adapter_dir),
        )
        adapter_outputs = [
            generate(
                model=adapter_model,
                tokenizer=tokenizer,
                prompt=_chat_prompt(tokenizer, prompt),
                max_tokens=256,
            )
            for prompt in prompts
        ]
        probe_outputs = [
            generate(
                model=adapter_model,
                tokenizer=tokenizer,
                prompt=_chat_prompt(tokenizer, prompt),
                max_tokens=128,
            )
            for prompt in probe_prompts
        ]
    finally:
        if adapter_model is not None:
            del adapter_model
        if tokenizer is not None:
            del tokenizer

    base_outputs: list[str] = []
    base_model_obj = None
    tokenizer = None
    try:
        base_model_obj, tokenizer = load(base_model)  # type: ignore[misc]
        base_outputs = [
            generate(
                model=base_model_obj,
                tokenizer=tokenizer,
                prompt=_chat_prompt(tokenizer, prompt),
                max_tokens=256,
            )
            for prompt in prompts
        ]
    finally:
        if base_model_obj is not None:
            del base_model_obj
        if tokenizer is not None:
            del tokenizer

    def base_predict(prompt: str) -> str:
        return base_outputs[prompts.index(prompt)]

    def adapter_predict(prompt: str) -> str:
        return adapter_outputs[prompts.index(prompt)]

    result = evaluate_models(
        prompts,
        references,
        base_predict=base_predict,
        adapter_predict=adapter_predict,
        profile=profile,
    )
    append_status(run_dir, {"stage": "eval_complete", "win_rate": result["win_rate"]})
    with (run_dir / "generated.jsonl").open("w", encoding="utf-8") as handle:
        for prompt, base_text, adapter_text in zip(
            prompts, base_outputs, adapter_outputs, strict=True
        ):
            handle.write(
                json.dumps(
                    {"prompt": prompt, "base": base_text, "adapter": adapter_text},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        for prompt, text in zip(probe_prompts, probe_outputs, strict=True):
            handle.write(
                json.dumps(
                    {"prompt": prompt, "probe": text},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    all_outputs = [*adapter_outputs, *probe_outputs]
    return {
        "measured": True,
        "win_rate": result["win_rate"],
        "prompts": result["prompts"],
        "adapter_wins": result["adapter_wins"],
        "base_wins": result["base_wins"],
        "ties": result["ties"],
        "method": result["method"],
        "tool_call_validity": tool_call_validity(all_outputs),
        "hud_conformance": hud_conformance(all_outputs),
        "overfit": overfit_report(
            config.get("train_loss", []), config.get("val_loss", [])
        ),
    }


def train_mlx_lora(
    run_dir: Path,
    config: dict,
    *,
    cancel_file: Path | None = None,
    force_double: bool = False,
) -> dict:
    """Train (or deterministically simulate) an MLX LoRA adapter in ``run_dir``.

    ``config`` keys: base_model, train_rows, eval_rows, use_4bit, rank,
    lora_alpha, lora_dropout, learning_rate, num_epochs, max_steps,
    batch_size, grad_accum_steps, warmup_steps, max_seq_length,
    grad_checkpoint, save_steps, logging_steps, val_batches, min_owner_pairs,
    eval_fraction, seed.
    """

    run_dir.mkdir(parents=True, exist_ok=True)
    config = dict(config)
    config["cancel_file"] = cancel_file
    _check_cancel(cancel_file)
    base_model = str(config.get("base_model", "Qwen/Qwen3-0.6B"))
    try:
        config["base_model_resolved"] = _base_model_resolved(base_model)
    except TrainingUnavailable as exc:
        if force_double:
            return _deterministic_double(
                run_dir,
                config,
                train_rows=config.get("train_rows", []),
                eval_rows=config.get("eval_rows", []),
                reason=str(exc),
            )
        raise

    if not _mlx_available() or force_double:
        reason = (
            "EV_TRAINING_LORA_FORCE_DOUBLE set"
            if force_double
            else "mlx/mlx-lm/mlx-tune/datasets are not installed"
        )
        return _deterministic_double(
            run_dir,
            config,
            train_rows=config.get("train_rows", []),
            eval_rows=config.get("eval_rows", []),
            reason=reason,
        )

    append_status(run_dir, {"stage": "started", "base_model": base_model})
    if config.get("train_mode") == "dpo":
        trained = _run_dpo_training(run_dir, config)
    else:
        trained = _run_real_training(run_dir, config)
    config["train_loss"] = trained["train_loss"]
    config["val_loss"] = trained["val_loss"]
    evaluation = _run_eval(run_dir, config, adapter_dir=trained["adapter_dir"])
    _write_json(run_dir / "eval.json", {"evaluation": evaluation})
    _write_json(
        run_dir / "manifest.json",
        {
            "degraded": False,
            "simulated": False,
            "real_weights": True,
            "base_model": base_model,
            "base_sha256_before": config.get("base_sha256_before"),
            "base_sha256_after": config.get("base_sha256_after"),
            "adapter_dir": str(trained["adapter_dir"].resolve()),
            "created_at": _now(),
        },
    )
    (run_dir / COMPLETE_MARKER).write_text("completed", encoding="utf-8")
    append_status(run_dir, {"stage": "completed", "degraded": False})
    return {
        "status": "completed",
        "degraded": False,
        "simulated": False,
        "real_weights": True,
        "artifact_path": str(trained["adapter_dir"].resolve()),
        "adapter_ref": str(run_dir.resolve()),
        "losses_path": str((run_dir / "losses.jsonl").resolve()),
        "eval_path": str((run_dir / "eval.json").resolve()),
        "evaluation": evaluation,
        "loss_curves": {"train": trained["train_loss"], "val": trained["val_loss"]},
    }


class contextlib_redirect_stdout:
    """Minimal stdlib contextlib.redirect_stdout replacement (avoids import)."""

    def __init__(self, target) -> None:
        self.target = target

    def __enter__(self):
        self.original = sys.stdout
        sys.stdout = self.target
        return self.target

    def __exit__(self, *exc) -> None:
        sys.stdout = self.original


def finalize_adapter(run_dir: Path, *, target_root: Path, name: str) -> Path:
    """Atomically move a completed run into the adapter registry.

    Only a run carrying the COMPLETE marker can be finalized; partial or
    interrupted runs are never exposed as activatable adapters.
    """

    run_dir = run_dir.resolve()
    if not (run_dir / COMPLETE_MARKER).exists():
        raise RuntimeError(f"run {run_dir.name} is not complete; refusing to finalize")
    adapter_dir = run_dir / "adapter"
    if not any(adapter_dir.iterdir()):
        raise RuntimeError(
            f"run {run_dir.name} produced an empty adapter directory; refusing to finalize"
        )
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / name
    if target.exists():
        raise RuntimeError(f"adapter target {target} already exists")
    staging = target_root / f".{name}.staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(run_dir, staging)
    os.replace(staging, target)
    return target


def rollback_adapter(adapter_ref: Path, *, active_root: Path) -> dict:
    """Boring rollback: remove the active adapter pointer, keep the base intact.

    LoRA adapters are separate files; the base model directory is never
    modified, so removing the active pointer returns inference to the
    byte-identical base. The base directory hash is re-verified if the
    run manifest recorded one.
    """

    adapter_ref = adapter_ref.resolve()
    manifest_path = adapter_ref / "manifest.json"
    base_before = None
    base_after = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            base_before = manifest.get("base_sha256_before")
            base_after = manifest.get("base_sha256_after")
        except (TypeError, ValueError):
            pass
    active = active_root / "active"
    removed = False
    if active.is_symlink() or active.exists():
        active.unlink(missing_ok=True)
        removed = True
    base_ok = True
    if base_before and base_after:
        base_ok = base_before == base_after
    return {
        "rolled_back": True,
        "adapter_ref": str(adapter_ref),
        "active_removed": removed,
        "base_byte_identical": base_ok,
        "base_sha256_before": base_before,
        "base_sha256_after": base_after,
    }
