"""Fabric package exports."""
from .files import bulk, create, delete, list_files, overwrite, read_file, undo
from .gaps import closest_alternative, record_gap, report_gap
from .grants import active_grants, check_grant, grant, revoke
from .policy import check_capability, contract_version, decide, journal, latency_budget_ms

__all__ = ["decide", "check_capability", "contract_version", "latency_budget_ms", "journal",
           "create", "overwrite", "delete", "bulk", "undo", "read_file", "list_files",
           "report_gap", "record_gap", "closest_alternative", "grant", "revoke",
           "check_grant", "active_grants", "run_turn"]


def __getattr__(name: str):
    if name == "run_turn":
        from .run_turn import run_turn
        return run_turn
    raise AttributeError(name)
