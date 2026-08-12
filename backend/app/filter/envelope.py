"""Data structures shared by the Intelligence Filter stages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime


def compute_envelope_hash(
    *,
    message: str,
    context: str,
    strategy: dict,
    privacy_level: str,
    speaker_method: str,
    media_refs: list[dict] | None = None,
) -> str:
    """Stable fingerprint of everything that crossed the input boundary."""

    payload = {
        "message": message,
        "context": context,
        "strategy": strategy,
        "privacy_level": privacy_level,
        "speaker_method": speaker_method,
        "media_refs": media_refs or [],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class SpeakerIdentity:
    actor_id: str
    verified: bool = True
    confidence: float = 1.0
    method: str = "auth_token"  # auth_token | voiceprint | challenge
    detail: str | None = None


@dataclass
class FilterFlag:
    """One auditable filter decision."""

    stage: str
    name: str
    severity: str  # info | low | medium | high | block
    detail: str = ""
    action: str = "allow"  # allow | flag | block | redact | soften | repair | refine

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "name": self.name,
            "severity": self.severity,
            "detail": self.detail,
            "action": self.action,
        }


@dataclass
class GroundingMaterial:
    """Memory content the output filter may use to verify claims."""

    text: str
    memory_id: str
    memory_type: str
    source_event_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    event_time: datetime | None = None
    privacy_level: str = "normal"


@dataclass
class Claim:
    """One extracted claim and the outcome of its grounding audit."""

    text: str
    kind: str = "personal"
    supported: bool = False
    evidence: list[str] = field(default_factory=list)
    score: float = 0.0
    action: str = "keep"  # keep | soften | remove

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "kind": self.kind,
            "supported": self.supported,
            "evidence": self.evidence,
            "score": round(self.score, 4),
            "action": self.action,
        }


@dataclass
class OutputReport:
    """Full audit trail for one output-filter run."""

    draft: str
    final_text: str
    edits: list[dict] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    structural: dict = field(default_factory=dict)
    persona: dict = field(default_factory=dict)
    safety: dict = field(default_factory=dict)
    critic: dict = field(default_factory=dict)
    iterations: int = 0
    passed: bool = True
    flags: list[FilterFlag] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "draft": self.draft,
            "final_text": self.final_text,
            "edits": self.edits,
            "claims": [c.to_dict() for c in self.claims],
            "structural": self.structural,
            "persona": self.persona,
            "safety": self.safety,
            "critic": self.critic,
            "iterations": self.iterations,
            "passed": self.passed,
            "flags": [f.to_dict() for f in self.flags],
        }
