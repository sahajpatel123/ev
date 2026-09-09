"""Digital Operations V1 — universal communications + service fabric."""

from app.digital.fabric import all_descriptors, answer_can_you, capability_matrix, execute
from app.digital.types import Availability, OpStatus, Verb

__all__ = [
    "Availability",
    "OpStatus",
    "Verb",
    "all_descriptors",
    "answer_can_you",
    "capability_matrix",
    "execute",
]
