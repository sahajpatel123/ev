"""Anti-cheat allowlist: (file) or (file, line) entries, each with a dated reason.

Format: frozenset of "path" or "path:line". Empty today: the main tree is
clean for the gate patterns, so any new hit blocks until justified here.
"""

ALLOW: frozenset = frozenset({
    # Example shape (none active):
    # "backend/app/scripts/recovery_importer.py",  # is_test is a DB column, not a branch
})
