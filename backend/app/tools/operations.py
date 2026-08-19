"""Fixed-argument software operations exposed to the owner-trusted API.

The sandbox is a containment boundary, not a capability allowlist.  Keep the
user-facing command surface explicit: callers select an operation name and
never supply a command string or arguments for it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NamedOperation:
    name: str
    command: str
    description: str


NAMED_OPERATIONS: dict[str, NamedOperation] = {
    "workspace_smoke_test": NamedOperation(
        name="workspace_smoke_test",
        command='python3 -c "print(\'EV workspace smoke test passed\')"',
        description="Run the fixed smoke test in the approved sandbox workspace.",
    ),
}


def resolve_operation(name: str) -> NamedOperation | None:
    """Resolve one allowlisted operation name, without accepting arguments."""

    return NAMED_OPERATIONS.get(name.strip())


def operation_for_command(command: str) -> NamedOperation | None:
    """Return the allowlisted operation for an exact fixed command."""

    for operation in NAMED_OPERATIONS.values():
        if operation.command == command:
            return operation
    return None
