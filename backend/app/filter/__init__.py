"""EVIE Intelligence Filter runtime.

The filter is the core intelligence layer: every inbound utterance is gated and
compiled before the provider, every outbound draft is validated, grounded,
persona-checked, safety-scanned, and refined, and every meaningful decision is
recorded in the filter ledger. None of this behavior lives in the provider, so
swapping the model never changes who EVIE is or what she guarantees.
"""

from app.filter.critic import CriticProvider, CriticRevision, GatewayCritic
from app.filter.input_filter import InputDecision, InputFilter
from app.filter.ledger import ledger_aggregate, list_ledger, record_decision
from app.filter.output_filter import OutputReport, run_output_filter

__all__ = [
    "CriticProvider",
    "CriticRevision",
    "GatewayCritic",
    "InputDecision",
    "InputFilter",
    "OutputReport",
    "ledger_aggregate",
    "list_ledger",
    "record_decision",
    "run_output_filter",
]
