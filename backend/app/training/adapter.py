"""Versioned EVIE adapter registry + weight-training provider contract (7.3).

An adapter registration binds a consent-gated corpus snapshot, runs
deterministic eval gates (non-empty corpus, corrections present, no leaked
secrets), and can be activated or rolled back. Actual weight training happens
behind an explicit ``WeightTrainingProvider``: a dry run validates the dataset
and gates with no external call, while a real run hands the exported JSONL to
a named provider (local LoRA runner or hosted fine-tune API). Registration,
versioning, and rollback are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.policy import remote_processing_allowed
from app.config import settings
from app.models import AdapterRegistration, TrainingCorpusSnapshot, VoiceSession
from app.services.access_log import log_access
from app.training.consent import active_consent, require_consent
from app.training.corpus import (
    dataset_records,
    dataset_summary,
    get_snapshot,
    to_jsonl,
)
from app.training.style_adapter import build_style_profile
from app.utils.text import utcnow

TRACK = "adapter_fine_tuning"
SECRET_PATTERN = re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")

# Mirrors app.voice.lifecycle.VoiceState/ACTIVE_STATES without importing the
# heavy voice stack into the training path. Training must never run while a
# voice session is live.
_VOICE_ACTIVE_STATES = {"verifying", "awake", "processing", "responding", "follow_up"}


def local_inference_target_configured() -> bool:
    """True when EV routes chat to a self-hosted model that could serve a LoRA.

    Weight training is staged: a trained adapter is only useful if there is an
    actual local inference target configured to load it (Ollama/llama.cpp with
    ``EV_CHAT_PROVIDER=local``). On the owner's current M2/8 GB setup reasoning
    runs through the DeepSeek API, so no servable target exists and training
    must refuse rather than produce an artifact with nowhere to load.
    """

    base_url = settings.local_model_base_url or os.getenv("EV_LOCAL_MODEL_BASE_URL")
    return settings.chat_provider == "local" and bool(base_url)


def _assert_servable_target(provider_obj: WeightTrainingProvider) -> None:
    """Refuse staged weight training when no local inference target exists."""

    if not getattr(provider_obj, "staged", False):
        return
    if local_inference_target_configured():
        return
    raise TrainingRunError(
        "MLX LoRA weight training is staged: no servable local inference target "
        "is configured. The owner's M2/8 GB hosts no local LLM inference, so a "
        "trained adapter would have nowhere to load. Configure EV_CHAT_PROVIDER=local "
        "with EV_LOCAL_MODEL_BASE_URL pointing at an Ollama/llama.cpp server that can "
        "load the adapter, or rely on the active prompt-level personalization "
        "(style profile, importance calibration, filter recalibration), which "
        "applies to every request at zero training cost."
    )


def _eval_gates(entries: list[dict]) -> dict:
    profile = build_style_profile(entries)
    rated = profile["signal_coverage"]["rated"]
    corrected = profile["signal_coverage"]["corrected"]
    gates = {
        "corpus_nonempty": len(entries) > 0,
        "corrections_present": any(
            (entry.get("signals") or {}).get("was_correction") is True
            for entry in entries
        ),
        "secrets_absent": not any(
            SECRET_PATTERN.search(str(entry.get("text", ""))) for entry in entries
        ),
        "style_profile_derived": profile["signal_coverage"]["assistant"] > 0,
        "style_signal_coverage": profile["signal_coverage"]["rated"] >= 1,
        "correction_rate": round(corrected / rated, 3) if rated else 0.0,
        "style_profile_coverage": profile["signal_coverage"]["assistant"] > 0,
    }
    return {
        "gates": gates,
        "passed": all(gates.values()),
        "profile": profile,
        "profile_hash": profile["profile_hash"],
    }


# --------------------------------------------------------------------------- #
# Weight-training provider contract
# --------------------------------------------------------------------------- #


class TrainingRunError(Exception):
    """Raised when a dry run or real training run cannot proceed."""

    def __init__(
        self,
        message: str,
        *,
        gates: dict | None = None,
        payload: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.gates = gates or {}
        self.payload = payload or {}


@dataclass(frozen=True)
class TrainingDataset:
    """Canonical dataset handed to a weight-training provider."""

    corpus_version: int
    records: list[dict]
    jsonl: str
    content_hash: str | None = None

    @property
    def record_count(self) -> int:
        return len(self.records)


class WeightTrainingProvider(ABC):
    """Explicit provider boundary for actual weight training.

    ``estimate`` must be local and deterministic (no external call); it powers
    the dry-run plan and the cost-approval gate. ``train`` is the only method
    allowed to contact a provider.
    """

    name: str = ""
    supports_remote: bool = False

    @abstractmethod
    async def estimate(
        self, dataset: TrainingDataset, *, base_model: str | None = None
    ) -> dict:
        """Return a deterministic plan/cost estimate without external calls."""

    @abstractmethod
    async def train(
        self,
        dataset: TrainingDataset,
        *,
        base_model: str | None = None,
        adapter_ref: str | None = None,
        cost_approved: bool = False,
    ) -> dict:
        """Run real weight training and return provider artifacts/results."""


class LocalLoRAProvider(WeightTrainingProvider):
    """Runs a configured local LoRA training command against the JSONL dataset."""

    name = "local-lora"
    supports_remote = False

    def __init__(
        self,
        command: str | None = None,
        *,
        workdir: Path | None = None,
        timeout_seconds: float = 1800.0,
    ) -> None:
        self.command = command or getattr(settings, "training_local_cmd", None)
        self.workdir = workdir
        self.timeout_seconds = timeout_seconds

    async def estimate(
        self, dataset: TrainingDataset, *, base_model: str | None = None
    ) -> dict:
        return {
            "provider": self.name,
            "estimated_cost_usd": 0.0,
            "dataset_records": dataset.record_count,
            "jsonl_bytes": len(dataset.jsonl.encode("utf-8")),
            "notes": "local runner; cost is local compute only",
        }

    async def train(
        self,
        dataset: TrainingDataset,
        *,
        base_model: str | None = None,
        adapter_ref: str | None = None,
        cost_approved: bool = False,
    ) -> dict:
        if not self.command:
            raise TrainingRunError(
                "Local LoRA runner not configured: set EV_TRAINING_LOCAL_CMD to the "
                "command that consumes --dataset (JSONL), --base-model, --adapter-ref"
            )
        workdir = self.workdir or Path(tempfile.mkdtemp(prefix="ev-lora-"))
        dataset_path = workdir / f"corpus-v{dataset.corpus_version}.jsonl"
        dataset_path.write_text(dataset.jsonl, encoding="utf-8")
        command = [
            *shlex.split(self.command),
            "--dataset",
            str(dataset_path),
        ]
        if base_model:
            command += ["--base-model", base_model]
        if adapter_ref:
            command += ["--adapter-ref", adapter_ref]
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self.timeout_seconds
        )
        if proc.returncode != 0:
            raise TrainingRunError(
                "Local LoRA runner failed "
                f"(exit {proc.returncode}): {stderr.decode()[:500]}"
            )
        artifact = workdir / "adapter"
        return {
            "provider": self.name,
            "adapter_ref": adapter_ref or str(artifact.resolve()),
            "artifact_path": str(artifact.resolve()),
            "stdout": stdout.decode()[:500],
            "cost_approved": cost_approved,
        }


class OpenAIFineTuneProvider(WeightTrainingProvider):
    """Hosted OpenAI fine-tuning: uploads the JSONL file, creates a job."""

    name = "openai-fine-tune"
    supports_remote = True

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_key = api_key or getattr(settings, "training_openai_api_key", None)
        self.base_url = (
            base_url or getattr(settings, "training_openai_base_url", None)
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or getattr(settings, "training_openai_model", None)
        self.timeout_seconds = timeout_seconds

    async def estimate(
        self, dataset: TrainingDataset, *, base_model: str | None = None
    ) -> dict:
        tokens = max(1, len(dataset.jsonl) // 4)
        # Engineering estimate only (training + a small eval margin); the real
        # bill comes from the provider job. A human must approve it explicitly.
        cost = round(tokens / 1_000_000 * 3.0, 4)
        return {
            "provider": self.name,
            "estimated_cost_usd": cost,
            "tokens_estimate": tokens,
            "base_model": base_model or self.model,
            "notes": "rough cost estimate; provider job is the authoritative bill",
        }

    async def train(
        self,
        dataset: TrainingDataset,
        *,
        base_model: str | None = None,
        adapter_ref: str | None = None,
        cost_approved: bool = False,
    ) -> dict:
        if not self.api_key:
            raise TrainingRunError(
                "OpenAI fine-tune API key missing: set EV_TRAINING_OPENAI_API_KEY"
            )
        if not cost_approved:
            raise TrainingRunError(
                "Training cost requires explicit human approval (cost_approved=true)"
            )
        model = base_model or self.model or "gpt-4o-mini-2024-07-18"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout_seconds,
        ) as client:
            upload = await client.post(
                "/files",
                files={
                    "file": (
                        f"ev-corpus-v{dataset.corpus_version}.jsonl",
                        dataset.jsonl.encode("utf-8"),
                        "application/jsonl",
                    )
                },
                data={"purpose": "fine-tune"},
            )
            upload.raise_for_status()
            file_id = upload.json()["id"]
            job = await client.post(
                "/fine_tuning/jobs",
                json={
                    "model": model,
                    "training_file": file_id,
                    "suffix": "evie",
                },
            )
            job.raise_for_status()
            body = job.json()
        return {
            "provider": self.name,
            "file_id": file_id,
            "job_id": body.get("id"),
            "status": body.get("status"),
            "model": model,
            "adapter_ref": adapter_ref or body.get("id"),
            "cost_approved": True,
        }


def _default_providers() -> dict[str, Callable[[], WeightTrainingProvider]]:
    return {
        "local-lora": LocalLoRAProvider,
        "openai-fine-tune": OpenAIFineTuneProvider,
        "mlx-lora": _mlx_lora_factory,
    }


def _mlx_lora_factory() -> WeightTrainingProvider:
    from app.training.lora import MLXLoRAProvider

    return MLXLoRAProvider()


PROVIDER_REGISTRY: dict[str, Callable[[], WeightTrainingProvider]] = _default_providers()


def register_training_provider(
    name: str, factory: Callable[[], WeightTrainingProvider]
) -> None:
    """Register an explicit weight-training provider (used by tests/extensions)."""

    PROVIDER_REGISTRY[name] = factory


def get_training_provider(name: str) -> WeightTrainingProvider:
    factory = PROVIDER_REGISTRY.get(name)
    if factory is None:
        raise TrainingRunError(f"Unknown training provider: {name}")
    return factory()


async def _build_dataset(
    session: AsyncSession, corpus_version: int
) -> tuple[TrainingCorpusSnapshot, TrainingDataset]:
    corpus = await get_snapshot(session, corpus_version)
    if corpus.redacted:
        raise ValueError(f"Corpus snapshot v{corpus_version} is redacted")
    records = dataset_records(list(corpus.entries or []))
    return corpus, TrainingDataset(
        corpus_version=corpus_version,
        records=records,
        jsonl=to_jsonl(records),
        content_hash=corpus.content_hash,
    )


def _approvals_required(provider: WeightTrainingProvider, estimate: dict) -> list[str]:
    approvals = ["adapter_fine_tuning consent"]
    if provider.supports_remote:
        approvals.append("remote training enabled (EV_ALLOW_REMOTE_TRAINING=true)")
        approvals.append("provider API key configured")
    if (estimate.get("estimated_cost_usd") or 0) > 0:
        approvals.append("cost approval (cost_approved=true)")
    return approvals


async def dry_run(
    session: AsyncSession,
    *,
    corpus_version: int,
    provider: str = "local-lora",
    base_model: str | None = None,
    adapter_ref: str | None = None,
    actor: str = "system",
) -> dict:
    """Validate dataset + eval gates for a provider without any external call."""

    await require_consent(session, TRACK)
    corpus, dataset = await _build_dataset(session, corpus_version)
    gates = _eval_gates(list(corpus.entries or []))
    provider_obj = get_training_provider(provider)
    estimate = await provider_obj.estimate(dataset, base_model=base_model)
    plan = {
        "mode": "dry_run",
        "provider": provider,
        "staged": getattr(provider_obj, "staged", False),
        "servable": (
            local_inference_target_configured()
            if getattr(provider_obj, "staged", False)
            else None
        ),
        "corpus_version": corpus_version,
        "gates": gates["gates"],
        "passed": gates["passed"],
        "eval_metrics": gates,
        "dataset": dataset_summary(dataset.records),
        "estimated_cost_usd": estimate.get("estimated_cost_usd"),
        "approvals_required": _approvals_required(provider_obj, estimate),
        "provider_remote": provider_obj.supports_remote,
        "content_hash": corpus.content_hash,
    }
    await log_access(
        session,
        actor=actor,
        action="adapter_dry_run",
        endpoint="POST /v1/training/adapter/dry-run",
        resource_type="adapter",
        resource_ids=[],
        details={
            "provider": provider,
            "corpus_version": corpus_version,
            "passed": gates["passed"],
            "dataset_records": dataset.record_count,
        },
    )
    return plan


async def run_training(
    session: AsyncSession,
    *,
    corpus_version: int,
    provider: str = "local-lora",
    base_model: str | None = None,
    adapter_ref: str | None = None,
    adapter_id: UUID | None = None,
    cost_approved: bool = False,
    actor: str = "system",
    reason: str | None = None,
) -> dict:
    """Run real weight training behind an explicit provider."""

    await require_consent(session, TRACK)
    await _assert_no_active_voice_session(session)
    corpus, dataset = await _build_dataset(session, corpus_version)
    gates = _eval_gates(list(corpus.entries or []))
    if not gates["passed"]:
        raise TrainingRunError(
            f"Adapter eval gates failed for corpus v{corpus_version}",
            gates=gates,
        )
    provider_obj = get_training_provider(provider)
    _assert_servable_target(provider_obj)
    if provider_obj.supports_remote and not remote_processing_allowed(TRACK):
        raise TrainingRunError(
            "Remote adapter training denied: EV_ALLOW_REMOTE_TRAINING is not enabled",
            gates=gates,
        )
    estimate = await provider_obj.estimate(dataset, base_model=base_model)
    if (estimate.get("estimated_cost_usd") or 0) > 0 and not cost_approved:
        raise TrainingRunError(
            "Training cost requires explicit human approval (cost_approved=true)",
            gates=gates,
            payload=estimate,
        )
    result = await provider_obj.train(
        dataset,
        base_model=base_model,
        adapter_ref=adapter_ref,
        cost_approved=cost_approved,
    )
    if adapter_id is not None:
        row = await get_adapter(session, adapter_id)
        if row.corpus_snapshot_id != corpus.id:
            raise TrainingRunError(
                "Adapter is not bound to the requested corpus snapshot",
                gates=gates,
            )
        if row.status not in ("approved", "active"):
            raise TrainingRunError(
                f"Adapter is {row.status}; training requires an approved adapter",
                gates=gates,
            )
        row.adapter_ref = result.get("adapter_ref") or row.adapter_ref
        row.eval_metrics = {**(row.eval_metrics or {}), "training_run": result}
        row.reason_for_change = reason or row.reason_for_change
        row.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="adapter_train",
        endpoint="POST /v1/training/adapter/train",
        resource_type="adapter",
        resource_ids=[adapter_id] if adapter_id else [],
        details={
            "provider": provider,
            "corpus_version": corpus_version,
            "adapter_ref": result.get("adapter_ref"),
            "status": result.get("status"),
        },
    )
    return {
        "mode": "train",
        "provider": provider,
        "corpus_version": corpus_version,
        "gates": gates["gates"],
        "passed": gates["passed"],
        "eval_metrics": gates,
        "dataset": dataset_summary(dataset.records),
        "result": result,
    }


async def _assert_no_active_voice_session(session: AsyncSession) -> None:
    """Refuse training while a voice session is live (voice-priority law)."""

    result = await session.execute(
        select(VoiceSession).where(
            VoiceSession.ended_at.is_(None),
            VoiceSession.state.in_(_VOICE_ACTIVE_STATES),
        )
    )
    if result.scalar_one_or_none() is not None:
        raise TrainingRunError(
            "training refused: a voice session is active; wait for it to end"
        )


def _adapter_eval_metrics(adapter_ref: str | None) -> dict | None:
    """Load shipped eval numbers from a local completed run, if present."""

    if not adapter_ref:
        return None
    path = Path(adapter_ref).expanduser()
    eval_path = path / "eval.json"
    if not eval_path.exists():
        return None
    try:
        payload = json.loads(eval_path.read_text(encoding="utf-8"))
    except (TypeError, ValueError):
        return None
    return payload.get("evaluation") or payload


async def _latest_adapter(session: AsyncSession, name: str) -> AdapterRegistration | None:
    result = await session.execute(
        select(AdapterRegistration)
        .where(
            AdapterRegistration.name == name,
            AdapterRegistration.redacted.is_(False),
        )
        .order_by(AdapterRegistration.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _active_adapter(session: AsyncSession, name: str) -> AdapterRegistration | None:
    result = await session.execute(
        select(AdapterRegistration).where(
            AdapterRegistration.name == name,
            AdapterRegistration.is_current.is_(True),
            AdapterRegistration.redacted.is_(False),
        )
    )
    return result.scalar_one_or_none()


async def active_style_profile(session: AsyncSession) -> dict | None:
    """Return the active adapter's deterministic style profile, if any.

    Privacy gate: the profile is only applied while ``adapter_fine_tuning``
    consent is still active and an adapter has been explicitly activated.
    Revoking consent (or rolling back/deleting the adapter) immediately stops
    the profile from influencing responses.
    """

    if await active_consent(session, TRACK) is None:
        return None
    result = await session.execute(
        select(AdapterRegistration)
        .where(
            AdapterRegistration.is_current.is_(True),
            AdapterRegistration.redacted.is_(False),
            AdapterRegistration.status == "active",
        )
        .order_by(AdapterRegistration.updated_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    profile = (row.eval_metrics or {}).get("profile")
    if not isinstance(profile, dict) or not profile.get("profile_hash"):
        return None
    return profile


async def register(
    session: AsyncSession,
    *,
    name: str,
    provider: str,
    base_model: str | None,
    adapter_ref: str | None,
    corpus_version: int,
    actor: str,
    reason: str | None = None,
) -> AdapterRegistration:
    consent = await require_consent(session, TRACK)
    corpus = await get_snapshot(session, corpus_version)
    if corpus.redacted:
        raise ValueError(f"Corpus snapshot v{corpus_version} is redacted")
    eval_metrics = _eval_gates(list(corpus.entries or []))
    shipped = _adapter_eval_metrics(adapter_ref)
    if shipped is not None:
        eval_metrics["evaluation"] = shipped
    status = "approved" if eval_metrics["passed"] else "rejected"

    versions = (
        (
            await session.execute(
                select(AdapterRegistration.version).where(
                    AdapterRegistration.name == name
                )
            )
        )
        .scalars()
        .all()
    )
    latest = await _latest_adapter(session, name)
    row = AdapterRegistration(
        name=name,
        version=max(versions, default=0) + 1,
        is_current=False,
        status=status,
        provider=provider,
        base_model=base_model,
        adapter_ref=adapter_ref,
        corpus_snapshot_id=corpus.id,
        eval_metrics=eval_metrics,
        reason_for_change=reason or f"registered from corpus v{corpus_version}",
        consent_id=consent.id,
        supersedes_id=latest.id if latest is not None else None,
    )
    session.add(row)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="adapter_register",
        endpoint="POST /v1/training/adapter/register",
        resource_type="adapter",
        resource_ids=[row.id],
        details={
            "name": name,
            "version": row.version,
            "status": status,
            "corpus_snapshot_id": str(corpus.id),
            "gates": eval_metrics,
        },
    )
    return row


async def get_adapter(session: AsyncSession, adapter_id: UUID) -> AdapterRegistration:
    row = await session.get(AdapterRegistration, adapter_id)
    if row is None or row.redacted:
        raise KeyError(f"Adapter {adapter_id} not found")
    return row


async def list_adapters(session: AsyncSession) -> list[AdapterRegistration]:
    result = await session.execute(
        select(AdapterRegistration)
        .where(AdapterRegistration.redacted.is_(False))
        .order_by(AdapterRegistration.created_at.desc())
    )
    return list(result.scalars().all())


async def activate(
    session: AsyncSession,
    *,
    adapter_id: UUID,
    actor: str,
    reason: str,
) -> AdapterRegistration:
    await require_consent(session, TRACK)
    row = await get_adapter(session, adapter_id)
    if row.status != "approved":
        raise ValueError(f"Adapter is {row.status}, only approved adapters can activate")
    training_run = (row.eval_metrics or {}).get("training_run") or {}
    if training_run:
        if training_run.get("status") != "completed":
            raise ValueError(
                "activation refused: training_run status is "
                f"{training_run.get('status')!r}, expected 'completed'"
            )
        if training_run.get("real_weights") is not True:
            raise ValueError(
                "activation refused: the training run produced no real weights "
                "(degraded/simulated double)"
            )
    if row.corpus_snapshot_id is not None:
        corpus = await session.get(TrainingCorpusSnapshot, row.corpus_snapshot_id)
        if corpus is None or corpus.redacted:
            raise ValueError("activation refused: bound corpus snapshot is missing/redacted")
        rechecked = _eval_gates(list(corpus.entries or []))
        if not rechecked["passed"]:
            raise ValueError(
                "activation refused: re-verified eval gates failed on the bound corpus"
            )
    active = await _active_adapter(session, row.name)
    if active is not None and active.id != row.id:
        active.is_current = False
        active.status = "rolled_back"
    row.is_current = True
    row.status = "active"
    row.reason_for_change = reason
    row.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="adapter_activate_reverified",
        endpoint="POST /v1/training/adapter/activate",
        resource_type="adapter",
        resource_ids=[row.id],
        details={
            "name": row.name,
            "version": row.version,
            "gates_passed": True,
            "training_run_verified": bool(training_run),
            "real_weights": training_run.get("real_weights") if training_run else None,
        },
    )
    await log_access(
        session,
        actor=actor,
        action="adapter_activate",
        endpoint="POST /v1/training/adapter/activate",
        resource_type="adapter",
        resource_ids=[row.id],
        details={"name": row.name, "version": row.version},
    )
    return row


async def rollback(
    session: AsyncSession,
    *,
    adapter_id: UUID,
    actor: str,
    reason: str,
) -> AdapterRegistration:
    await require_consent(session, TRACK)
    row = await get_adapter(session, adapter_id)
    if row.supersedes_id is None:
        raise ValueError("Adapter has no prior version to roll back to")
    previous = await session.get(AdapterRegistration, row.supersedes_id)
    if previous is None or previous.redacted:
        raise KeyError("Prior adapter version not found")
    row.is_current = False
    row.status = "rolled_back"
    previous.is_current = True
    previous.status = "active"
    previous.reason_for_change = reason
    row.updated_at = utcnow()
    rollback_files: dict | None = None
    if row.adapter_ref:
        try:
            from app.training.lora import rollback as lora_rollback

            rollback_files = lora_rollback(row.adapter_ref)
        except Exception as exc:  # best-effort file rollback; DB is authoritative
            rollback_files = {"error": str(exc)}
    await log_access(
        session,
        actor=actor,
        action="adapter_rollback",
        endpoint="POST /v1/training/adapter/rollback",
        resource_type="adapter",
        resource_ids=[row.id, previous.id],
        details={
            "name": row.name,
            "from_version": row.version,
            "to_version": previous.version,
            "file_rollback": rollback_files,
        },
    )
    return previous


async def delete_all(session: AsyncSession, *, actor: str, reason: str) -> int:
    rows = list((await session.execute(select(AdapterRegistration))).scalars().all())
    for row in rows:
        row.redacted = True
        row.is_current = False
        row.status = "deleted"
        row.eval_metrics = {}
        row.reason_for_change = f"{reason} (redacted)"
        row.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="adapter_delete",
        endpoint="POST /v1/training/adapter/delete",
        resource_type="adapter",
        resource_ids=[r.id for r in rows],
        details={"redacted": len(rows), "reason": reason},
    )
    return len(rows)
