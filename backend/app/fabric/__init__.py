"""Fabric package exports."""
from .policy import decide, check_capability, contract_version, latency_budget_ms, journal
from .files import create, overwrite, delete, bulk, undo, read_file, list_files
from .gaps import report_gap, record_gap, closest_alternative
from .grants import grant, revoke, check_grant, active_grants

__all__ = ["decide", "check_capability", "contract_version", "latency_budget_ms", "journal",
           "create", "overwrite", "delete", "bulk", "undo", "read_file", "list_files",
           "report_gap", "record_gap", "closest_alternative", "grant", "revoke",
           "check_grant", "active_grants", "run_turn"]


def __getattr__(name: str):
    if name == "run_turn":
        from .run_turn import run_turn
        return run_turn
    raise AttributeError(name)
