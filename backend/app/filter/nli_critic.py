"""Optional on-demand NLI critic for semantic claim grounding.

Each extracted claim is scored against the retrieved evidence that was
actually present in the context (``GroundingMaterial``) as entailed /
neutral / contradicted, using a small quantized MobileBERT MNLI cross-encoder
(``onnx/model_quantized.onnx``, ~26 MB). The critic lives on the same
ModelArbiter as every other EV model, declares its full ModelSpec, and is
evicted after every audit so it is never resident during a voice session.

Offline CI stays green with no weights: when the ONNX file or tokenizer are
absent, or ONNX Runtime is unavailable, the critic degrades to the lexical
grounding path and reports ``degraded=True``. Scores are never fabricated;
every number returned here comes from the model's own logits (or is absent).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.config import settings
from app.filter.envelope import Claim, GroundingMaterial
from app.ml import store
from app.ml.arbiter import ModelArbiter
from app.ml.registry import ModelRegistry, ModelSpec, ModelTier
from app.ml.settings import MLSettings, get_ml_settings

NLI_MODEL = "nli-mobilebert-mnli-q8"
NLI_SOURCE_URL = (
    "https://huggingface.co/Xenova/mobilebert-uncased-mnli/resolve/main/"
    "onnx/model_quantized.onnx"
)
NLI_TOKENIZER_URL = (
    "https://huggingface.co/Xenova/mobilebert-uncased-mnli/resolve/main/"
    "tokenizer.json"
)
# Seed entry: checksums must be pinned (and ``verified=True``) before
# ``python -m app.ml.cli pull`` will download. The loader only requires the
# files to exist, so a manually verified cache still works today.
NLI_ONNX_SHA256: str | None = None
NLI_TOKENIZER_SHA256: str | None = None

# MobileBERT MNLI config.json: 0=ENTAILMENT, 1=NEUTRAL, 2=CONTRADICTION.
ENTAIL_THRESHOLD = 0.6
CONTRADICT_THRESHOLD = 0.6

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def nli_critic_spec() -> ModelSpec:
    """Declarative model entry: on-demand tier, under the 70 MB budget."""

    return ModelSpec(
        name=NLI_MODEL,
        task="nli_grounding",
        source_url=NLI_SOURCE_URL,
        sha256=NLI_ONNX_SHA256,
        disk_mb=28,
        resident_mb=28,
        peak_mb=64,
        tier=ModelTier.ON_DEMAND,
        license=(
            "Apache-2.0 (MobileBERT base); Typeform MNLI fine-tune card does "
            "not state a license - recorded as Agent 16 compliance caveat"
        ),
        license_url="https://huggingface.co/Xenova/mobilebert-uncased-mnli",
        version="mobilebert-uncased-mnli (quantized)",
        verified=NLI_ONNX_SHA256 is not None,
    )


def create_nli_arbiter(ml_settings: MLSettings | None = None) -> ModelArbiter:
    """Arbiter with only the NLI critic registered (mirrors the reranker)."""

    ml_settings = ml_settings or get_ml_settings()
    registry = ModelRegistry(exclusive_limit_mb=ml_settings.ml_exclusive_limit_mb)
    registry.register(nli_critic_spec())
    return ModelArbiter(registry, ml_settings)


def _significant_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 4 or t.isdigit()}


def _evidence_candidates(
    claim_text: str,
    material: list[GroundingMaterial],
    *,
    top: int = 3,
) -> list[GroundingMaterial]:
    """Rank retrieved memories by lexical overlap so NLI sees the right premise."""

    claim_tokens = _significant_tokens(claim_text)
    scored: list[tuple[float, GroundingMaterial]] = []
    for mem in material:
        mem_tokens = _significant_tokens(mem.text)
        overlap = len(claim_tokens & mem_tokens) / max(1, len(claim_tokens))
        scored.append((overlap, mem))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [mem for _, mem in scored[:top]]


class NLICritic:
    """MobileBERT MNLI cross-encoder on the on-demand arbiter slot.

    ``evict_after_use=True`` (the default, from settings) releases the model
    after every audit: the arbiter no longer counts it as resident and the
    session/tokenizer handles are dropped so a voice session never finds it
    in memory.
    """

    name = "nli-critic"
    model_version = NLI_MODEL

    def __init__(
        self,
        *,
        max_length: int = 128,
        batch_size: int | None = None,
        ml_settings: MLSettings | None = None,
        arbiter: ModelArbiter | None = None,
        evict_after_use: bool | None = None,
    ) -> None:
        self.max_length = max_length
        self.batch_size = batch_size or settings.nli_critic_batch_size
        self.ml_settings = ml_settings or get_ml_settings()
        self.arbiter = arbiter or create_nli_arbiter(self.ml_settings)
        self.evict_after_use = (
            settings.nli_critic_evict_after_use
            if evict_after_use is None
            else evict_after_use
        )
        self._session: Any = None
        self._tokenizer: Any = None
        self._input_names: list[str] = []
        self._lock = threading.Lock()
        self._users = 0
        self._degraded = True

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _tokenizer_path(self) -> Path:
        return Path(self.ml_settings.ml_model_dir) / f"{NLI_MODEL}__tokenizer.json"

    def _ensure_ready(self) -> bool:
        if self._session is not None and self._tokenizer is not None:
            self._degraded = False
            return True
        with self._lock:
            if self._session is not None and self._tokenizer is not None:
                self._degraded = False
                return True
            try:
                self._load()
            except Exception:  # noqa: BLE001 - degraded fallback is the contract
                self._session = None
                self._tokenizer = None
                self._input_names = []
                self._degraded = True
                return False
            self._degraded = False
            return True

    def _load(self) -> None:
        try:
            import onnxruntime as ort  # noqa: F401
        except Exception as exc:
            raise RuntimeError("onnxruntime not installed; install the 'ml' extra") from exc
        try:
            from tokenizers import Tokenizer  # type: ignore[import-not-found]
        except Exception as exc:
            raise RuntimeError(
                "'tokenizers' not installed; lazy dependency requested from Agent 2"
            ) from exc

        spec = nli_critic_spec()
        model_path = store.target_path(self.ml_settings, spec)
        tokenizer_path = self._tokenizer_path()
        if not model_path.exists() or not tokenizer_path.exists():
            raise RuntimeError(
                f"NLI critic weights not cached ({model_path} / {tokenizer_path}); "
                "see docs/MODEL_BUDGET.md for the pinned download"
            )
        with self.arbiter.acquire(NLI_MODEL):
            session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
            tokenizer = Tokenizer.from_file(str(tokenizer_path))
            tokenizer.enable_truncation(max_length=self.max_length)
        self._session = session
        self._tokenizer = tokenizer
        self._input_names = [inp.name for inp in session.get_inputs()]

    def _score_pairs_sync(self, pairs: list[tuple[str, str]]) -> list[tuple[float, float, float]]:
        import numpy as np

        tokenizer = self._tokenizer
        was_padding = tokenizer.padding is not None
        tokenizer.enable_padding()
        try:
            encodings = tokenizer.encode_batch(pairs, is_pair=True)
        finally:
            if not was_padding:
                tokenizer.disable_padding()
        input_ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)
        feeds: dict[str, Any] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.asarray(
                [e.type_ids for e in encodings],
                dtype=np.int64,
            )
        outputs = self._session.run(None, feeds)
        logits = np.asarray(outputs[0])
        if logits.ndim == 1:
            logits = logits.reshape(1, -1)
        exp = np.exp(logits - logits.max(axis=1, keepdims=True))
        probs = exp / exp.sum(axis=1, keepdims=True)
        return [(float(row[0]), float(row[1]), float(row[2])) for row in probs]

    def audit_claims_semantic(
        self,
        claims: list[Claim],
        material: list[GroundingMaterial],
    ) -> tuple[list[Claim], dict]:
        """Score claims with NLI; return updated claims plus an audit dict.

        With no weights the audit is a deterministic no-op (``degraded=True``),
        so the lexical grounding path remains the offline guarantee.
        """

        started = time.perf_counter()
        if not claims:
            return claims, {
                "degraded": False,
                "model": self.model_version,
                "claims_scored": 0,
                "entailed": 0,
                "neutral": 0,
                "contradicted": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        if not material:
            return claims, {
                "degraded": False,
                "skipped": True,
                "reason": "no_grounding_material",
                "claims_scored": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }

        self._users += 1
        try:
            ready = self._ensure_ready()
            if not ready:
                return claims, {
                    "degraded": True,
                    "model": None,
                    "reason": "weights_or_runtime_unavailable",
                    "claims_scored": 0,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            pairs: list[tuple[str, str]] = []
            candidates_by_index: list[list[GroundingMaterial]] = []
            for claim in claims:
                candidates = _evidence_candidates(claim.text, material)
                candidates_by_index.append(candidates)
                if candidates:
                    pairs.append((candidates[0].text, claim.text))
                else:
                    pairs.append(("", claim.text))

            results: list[tuple[float, float, float]] = []
            for start in range(0, len(pairs), self.batch_size):
                chunk = pairs[start : start + self.batch_size]
                results.extend(self._score_pairs_sync(chunk))

            updated: list[Claim] = []
            counts = {"entailed": 0, "neutral": 0, "contradicted": 0}
            for claim, probs, candidates in zip(claims, results, candidates_by_index, strict=True):
                entail, neutral, contradict = probs
                best_mem = candidates[0] if candidates else None
                evidence = [best_mem.memory_id] if best_mem is not None else []
                if entail >= ENTAIL_THRESHOLD and entail >= contradict:
                    updated.append(
                        replace(
                            claim,
                            supported=True,
                            evidence=evidence,
                            score=round(entail, 4),
                            action="keep",
                        )
                    )
                    counts["entailed"] += 1
                elif contradict >= CONTRADICT_THRESHOLD and contradict >= entail:
                    updated.append(
                        replace(
                            claim,
                            supported=False,
                            evidence=evidence,
                            score=round(contradict, 4),
                            action="remove",
                        )
                    )
                    counts["contradicted"] += 1
                else:
                    updated.append(
                        replace(
                            claim,
                            supported=False,
                            evidence=[],
                            score=round(neutral, 4),
                            action="soften",
                        )
                    )
                    counts["neutral"] += 1
            return updated, {
                "degraded": False,
                "model": self.model_version,
                "claims_scored": len(updated),
                **counts,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            return claims, {
                "degraded": True,
                "model": self.model_version,
                "reason": f"nli_inference_error: {type(exc).__name__}",
                "claims_scored": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        finally:
            self._users = max(0, self._users - 1)
            if self.evict_after_use:
                self.release()

    def release(self) -> None:
        """Evict the model from the arbiter and drop loaded handles."""

        with self._lock:
            if self._users > 0:
                return
            if self.arbiter.is_resident(NLI_MODEL):
                with suppress(Exception):
                    self.arbiter.evict(NLI_MODEL, force=True)
            self._session = None
            self._tokenizer = None
            self._input_names = []
            self._degraded = True


async def run_nli_audit(
    claims: list[Claim],
    material: list[GroundingMaterial],
    *,
    critic: NLICritic | None = None,
) -> tuple[list[Claim], dict]:
    """Thread-safe async entry point used by ``run_output_filter``."""

    if not settings.nli_critic_enabled:
        return claims, {"degraded": True, "reason": "disabled"}
    active = critic or NLICritic()
    try:
        updated, info = await asyncio.to_thread(
            active.audit_claims_semantic,
            claims,
            material,
        )
        return updated, info
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        return claims, {
            "degraded": True,
            "reason": f"nli_audit_error: {type(exc).__name__}",
        }
