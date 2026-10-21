"""Evie agent runtime — a general local agent for arbitrary owner goals.

The loop takes any goal, lets the configured brain plan tool calls (shell,
files, web, apps), executes them through policy-gated existing engines, and
stops with receipts instead of claims. Surfaces:

- ``python -m app.agent "goal"`` — local, no server needed.
- ``ev agent "goal"`` — client of the HTTP surface (``/v1/agent``).
- ``app.agent.service.SERVICE`` — the in-process run registry.

The kernel stays the conversational mind; this package is the doer.
"""

from app.agent.brain import (
    BrainChoice,
    brain_status,
    build_agent_provider,
    resolve_agent_brain,
)
from app.agent.loop import (
    AgentLimits,
    AgentResult,
    AgentStep,
    ApprovalGate,
    ApprovalRequest,
    ApproveGate,
    CallbackGate,
    CancelToken,
    DenyGate,
    QueueGate,
    run_agent,
)

__all__ = [
    "AgentLimits",
    "AgentResult",
    "AgentStep",
    "ApprovalGate",
    "ApprovalRequest",
    "ApproveGate",
    "BrainChoice",
    "CancelToken",
    "CallbackGate",
    "DenyGate",
    "QueueGate",
    "brain_status",
    "build_agent_provider",
    "resolve_agent_brain",
    "run_agent",
]
