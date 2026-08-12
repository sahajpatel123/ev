"""MLX-native LoRA training provider for EV (Apple Silicon, 8 GB budget).

``MLXLoRAProvider`` implements the ``WeightTrainingProvider`` contract with a
real mlx-tune trainer: Qwen3-0.6B/1.7B base, 4-bit QLoRA, short sequences,
gradient checkpointing, and the ``trainer-mlx-lora`` exclusive arbiter tier.
The estimate is local and deterministic; training streams progress to
``status.jsonl`` and is interruptible without ever exposing a partial adapter
as activatable. When MLX/weights are absent the provider degrades to a
clearly-labeled deterministic double (``degraded=true``, ``real_weights=false``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.ml.arbiter import ModelArbiter, ModelLoadRefused, create_default_arbiter
from app.ml.settings import get_ml_settings
from app.training import corpus as corpus_service
from app.training.adapter import TrainingDataset, TrainingRunError, WeightTrainingProvider
from app.training.eval import held_out_split
from app.training.lora_runner import (
    COMPLETE_MARKER,
    append_status,
    base_directory_hash,
    finalize_adapter,
    resolve_base_directory,
    rollback_adapter,
    train_mlx_lora,
)


class LoRASettings(BaseSettings):
    """Env-backed training defaults (prefix ``EV_TRAINING_LORA_``)."""

    model_config = SettingsConfigDict(
        env_prefix="EV_TRAINING_LORA_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    base_model: str = "Qwen/Qwen3-0.6B"
    output_root: Path = Field(default=Path.home() / ".ev" / "adapters")
    min_owner_pairs: int = Field(default=200, ge=1)
    max_seq_length: int = Field(default=512, ge=64)
    rank: int = Field(default=16, ge=1)
    lora_alpha: int = Field(default=32, ge=1)
    lora_dropout: float = Field(default=0.05, ge=0.0, le=0.5)
    learning_rate: float = Field(default=1e-4, gt=0.0)
    num_epochs: int = Field(default=3, ge=1)
    max_steps: int = Field(default=-1)
    batch_size: int = Field(default=1, ge=1)
    grad_accum_steps: int = Field(default=1, ge=1)
    warmup_steps: int = Field(default=10, ge=0)
    save_steps: int = Field(default=100, ge=1)
    logging_steps: int = Field(default=5, ge=1)
    eval_fraction: float = Field(default=0.2, gt=0.0, lt=0.5)
    seed: int = Field(default=42)
    grad_checkpoint: bool = True
    use_4bit: bool = True
    val_batches: int = Field(default=5, ge=0)
    target_modules: tuple[str, ...] = ("q_proj", "k_proj", "v_proj", "o_proj")
    train_mode: str = "sft"  # sft | dpo
    force_double: bool = False
    arbiter_name: str = "trainer-mlx-lora"
    timeout_seconds: float = Field(default=6 * 3600.0, ge=60.0)
    interrupt_grace_seconds: float = Field(default=30.0, ge=1.0)


def _default_settings() -> LoRASettings:
    return LoRASettings()


def _local_target_ready() -> bool:
    """Mirror of adapter.local_inference_target_configured (avoids import cycle)."""

    import os

    from app.config import settings

    base_url = settings.local_model_base_url or os.getenv("EV_LOCAL_MODEL_BASE_URL")
    return settings.chat_provider == "local" and bool(base_url)


@dataclass(frozen=True)
class _RunPlan:
    pairs: int
    steps: int
    eval_prompts: int
    wall_clock_seconds: int
    peak_mb: int
    resident_mb: int


class MLXLoRAProvider(WeightTrainingProvider):
    """Real local weight training on Apple Silicon via mlx-tune."""

    name = "mlx-lora"
    supports_remote = False
    # Staged: a trained adapter can only be served by a self-hosted local model.
    # Until EV_CHAT_PROVIDER=local + EV_LOCAL_MODEL_BASE_URL are configured,
    # run_training refuses to produce an artifact with nowhere to load.
    staged = True

    def __init__(
        self,
        settings: LoRASettings | None = None,
        *,
        arbiter: ModelArbiter | None = None,
    ) -> None:
        self.settings = settings or _default_settings()
        self.arbiter = arbiter or create_default_arbiter(get_ml_settings())

    def _records_for(self, records: list[dict], fmt: str) -> list[dict]:
        """Format records, passing through already-formatted rows unchanged."""

        formatted = corpus_service.format_records(records, fmt=fmt)
        if formatted:
            return formatted
        required = {
            "sft": ("instruction", "output"),
            "preference": ("chosen", "rejected"),
        }.get(fmt)
        if required and all(any(key in record for key in required) for record in records):
            for record in records:
                record.setdefault("hash", corpus_service._entry_hash(record))
            return records
        return formatted

    # -- contract ------------------------------------------------------------

    async def estimate(
        self, dataset: TrainingDataset, *, base_model: str | None = None
    ) -> dict:
        preference_records = self._records_for(dataset.records, "preference")
        if self.settings.train_mode == "dpo":
            records = self._records_for(dataset.records, "preference")
            preference_records = records
            pairs = len(records)
            train_rows, eval_rows = held_out_split(
                records,
                eval_fraction=self.settings.eval_fraction,
                seed=self.settings.seed,
            )
        else:
            records = self._records_for(dataset.records, "sft")
            pairs = len(records)
            train_rows, eval_rows = held_out_split(
                records,
                eval_fraction=self.settings.eval_fraction,
                seed=self.settings.seed,
            )
        plan = _RunPlan(
            pairs=pairs,
            steps=max(1, math.ceil(len(train_rows) / self.settings.batch_size) * self.settings.num_epochs),
            eval_prompts=len(eval_rows),
            wall_clock_seconds=max(1, int(len(train_rows) * 2.0)),
            peak_mb=3500,
            resident_mb=2000,
        )
        if self.settings.train_mode == "dpo" and not preference_records:
            raise TrainingRunError(
                "DPO training requires preference pairs (filter-ledger "
                "draft/final rows); none were found in the dataset"
            )
        if plan.pairs < self.settings.min_owner_pairs:
            raise TrainingRunError(
                "MLX LoRA refuses to train below the overfitting floor: "
                f"dataset has {plan.pairs} owner pairs, requires at least "
                f"{self.settings.min_owner_pairs} (set EV_TRAINING_LORA_MIN_OWNER_PAIRS "
                "only after collecting more real correction data)"
            )
        return {
            "provider": self.name,
            "estimated_cost_usd": 0.0,
            "dataset_records": dataset.record_count,
            "owner_pairs": plan.pairs,
            "estimated_steps": plan.steps,
            "estimated_eval_prompts": plan.eval_prompts,
            "estimated_wall_clock_seconds": plan.wall_clock_seconds,
            "estimated_peak_mb": plan.peak_mb,
            "estimated_resident_mb": plan.resident_mb,
            "base_model": base_model or self.settings.base_model,
            "tier": "exclusive",
            "staged": True,
            "servable": _local_target_ready(),
            "notes": (
                "local compute only; no bytes leave the machine; model registry "
                "entry trainer-mlx-lora holds the arbiter global lock. STAGED: "
                "requires a self-hosted inference target (EV_CHAT_PROVIDER=local + "
                "EV_LOCAL_MODEL_BASE_URL) that can load the adapter"
            ),
        }

    async def train(
        self,
        dataset: TrainingDataset,
        *,
        base_model: str | None = None,
        adapter_ref: str | None = None,
        cost_approved: bool = False,
    ) -> dict:
        preference_records = self._records_for(dataset.records, "preference")
        if self.settings.train_mode == "dpo":
            records = self._records_for(dataset.records, "preference")
            pairs = len(records)
            train_rows, eval_rows = held_out_split(
                records,
                eval_fraction=self.settings.eval_fraction,
                seed=self.settings.seed,
            )
        else:
            records = self._records_for(dataset.records, "sft")
            pairs = len(records)
            train_rows, eval_rows = held_out_split(
                records,
                eval_fraction=self.settings.eval_fraction,
                seed=self.settings.seed,
            )
        plan = _RunPlan(
            pairs=pairs,
            steps=max(1, math.ceil(len(train_rows) / self.settings.batch_size) * self.settings.num_epochs),
            eval_prompts=len(eval_rows),
            wall_clock_seconds=max(1, int(len(train_rows) * 2.0)),
            peak_mb=3500,
            resident_mb=2000,
        )
        if self.settings.train_mode == "dpo" and not preference_records:
            raise TrainingRunError(
                "DPO training requires preference pairs (filter-ledger "
                "draft/final rows); none were found in the dataset"
            )
        if plan.pairs < self.settings.min_owner_pairs:
            raise TrainingRunError(
                "MLX LoRA refuses to train below the overfitting floor: "
                f"dataset has {plan.pairs} owner pairs, requires at least "
                f"{self.settings.min_owner_pairs}"
            )
        base_model = base_model or self.settings.base_model
        run_root = self.settings.output_root / "runs"
        run_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"{adapter_ref or 'evie-mlx'}-v{dataset.corpus_version}-{stamp}"
        run_dir = run_root / name
        if run_dir.exists():
            run_dir = run_root / f"{name}-{len(list(run_root.glob(f'{name}*')))}"
        run_dir.mkdir(parents=True, exist_ok=False)
        cancel_file = run_dir / "CANCEL"
        append_status(run_dir, {"stage": "queued", "provider": self.name})

        # Dataset artifacts: every format, re-privacy-checked by the exporter.
        for fmt in ("canonical", "sft", "preference", "tool"):
            records = self._records_for(dataset.records, fmt=fmt)
            (run_dir / f"{fmt}.jsonl").write_text(
                corpus_service.to_jsonl(records), encoding="utf-8"
            )
        sft_records = self._records_for(dataset.records, fmt="sft")
        if self.settings.train_mode == "dpo":
            _, eval_rows = held_out_split(
                sft_records,
                eval_fraction=self.settings.eval_fraction,
                seed=self.settings.seed,
            )
            if not eval_rows:
                _, preference_eval = held_out_split(
                    preference_records,
                    eval_fraction=self.settings.eval_fraction,
                    seed=self.settings.seed,
                )
                eval_rows = [
                    {
                        "instruction": str(row.get("prompt", "")),
                        "output": str(row.get("chosen", "")),
                        "signals": row.get("signals", {}),
                        "source": str(row.get("source", "")),
                    }
                    for row in preference_eval
                ]
        else:
            train_rows, eval_rows = held_out_split(
                sft_records,
                eval_fraction=self.settings.eval_fraction,
                seed=self.settings.seed,
            )

        # Arbiter: exclusive global lock, evicts on-demand models, refuses if
        # another exclusive holder is active.
        stats = self.arbiter.stats()
        holder = stats.get("exclusive_holder")
        if holder is not None and holder != self.settings.arbiter_name:
            raise TrainingRunError(
                f"another exclusive model {holder!r} holds the arbiter global lock; "
                "training cannot start"
            )

        local_base = resolve_base_directory(base_model)
        base_before = base_directory_hash(local_base) if local_base is not None else None
        config = {
            "base_model": base_model,
            "train_mode": self.settings.train_mode,
            "train_rows": train_rows,
            "eval_rows": eval_rows,
            "preference_rows": preference_records,
            "use_4bit": self.settings.use_4bit,
            "rank": self.settings.rank,
            "lora_alpha": self.settings.lora_alpha,
            "lora_dropout": self.settings.lora_dropout,
            "learning_rate": self.settings.learning_rate,
            "num_epochs": self.settings.num_epochs,
            "max_steps": self.settings.max_steps,
            "batch_size": self.settings.batch_size,
            "grad_accum_steps": self.settings.grad_accum_steps,
            "warmup_steps": self.settings.warmup_steps,
            "save_steps": self.settings.save_steps,
            "logging_steps": self.settings.logging_steps,
            "max_seq_length": self.settings.max_seq_length,
            "grad_checkpoint": self.settings.grad_checkpoint,
            "val_batches": self.settings.val_batches,
            "eval_fraction": self.settings.eval_fraction,
            "seed": self.settings.seed,
            "base_sha256_before": base_before,
        }
        try:
            with self.arbiter.acquire(
                self.settings.arbiter_name, release_on_exit=True
            ):
                task = asyncio.create_task(
                    asyncio.to_thread(
                        train_mlx_lora,
                        run_dir,
                        config,
                        cancel_file=cancel_file,
                        force_double=self.settings.force_double,
                    )
                )
                try:
                    result = await asyncio.wait_for(
                        task, timeout=self.settings.timeout_seconds
                    )
                except asyncio.CancelledError:
                    cancel_file.write_text("cancelled", encoding="utf-8")
                    append_status(run_dir, {"stage": "interrupt_requested"})
                    with contextlib.suppress(
                        TimeoutError, asyncio.CancelledError
                    ):
                        await asyncio.wait_for(
                            asyncio.shield(task),
                            timeout=self.settings.interrupt_grace_seconds,
                        )
                    append_status(run_dir, {"stage": "interrupted"})
                    raise TrainingRunError(
                        f"training interrupted; partial state at {run_dir} is not "
                        "activatable"
                    ) from None
        except ModelLoadRefused as exc:
            append_status(run_dir, {"stage": "refused", "reason": str(exc)})
            raise TrainingRunError(f"arbiter refused training: {exc}") from exc
        except Exception:
            append_status(run_dir, {"stage": "failed"})
            raise

        local_base = resolve_base_directory(base_model)
        base_after = base_directory_hash(local_base) if local_base is not None else None
        if base_before and base_after and base_before != base_after:
            raise TrainingRunError(
                "base model directory changed during training; refusing to register "
                "the adapter (rollback safety violated)"
            )
        manifest = {
            "base_model": base_model,
            "base_model_resolved": str(local_base) if local_base is not None else None,
            "base_sha256_before": base_before,
            "base_sha256_after": base_after,
            "corpus_version": dataset.corpus_version,
            "content_hash": dataset.content_hash,
            "min_owner_pairs": self.settings.min_owner_pairs,
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        finalized = finalize_adapter(
            run_dir,
            target_root=self.settings.output_root / "adapters",
            name=run_dir.name,
        )
        append_status(run_dir, {"stage": "finalized", "path": str(finalized)})
        evaluation = result.get("evaluation", {})
        return {
            "provider": self.name,
            "status": "completed",
            "adapter_ref": str(finalized),
            "artifact_path": str(finalized / "adapter"),
            "base_model": base_model,
            "degraded": result.get("degraded", False),
            "simulated": result.get("simulated", False),
            "real_weights": result.get("real_weights", False),
            "loss_curves": result.get("loss_curves", {"train": [], "val": []}),
            "losses_path": str(run_dir / "losses.jsonl"),
            "eval": evaluation,
            "win_rate": evaluation.get("win_rate"),
            "tool_call_validity": evaluation.get("tool_call_validity"),
            "hud_conformance": evaluation.get("hud_conformance"),
            "overfit": evaluation.get("overfit"),
            "base_sha256_before": base_before,
            "base_sha256_after": base_after,
            "cost_approved": cost_approved,
        }


def rollback(adapter_ref: str | Path) -> dict:
    """One-command rollback to the byte-identical base model."""

    settings = _default_settings()
    return rollback_adapter(
        Path(adapter_ref).expanduser(),
        active_root=settings.output_root / "active",
    )


def is_complete_run(adapter_ref: str | Path) -> bool:
    return (Path(adapter_ref).expanduser() / COMPLETE_MARKER).exists()
