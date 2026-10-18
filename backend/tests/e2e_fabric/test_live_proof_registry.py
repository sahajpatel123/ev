"""The live-proof harness must actually run LP1–LP11, not only the later ones.

This does not execute the lanes. `make live-proof` is the run.
"""

from app.scripts.live_proof import PROOFS


def test_live_proof_registers_every_lp() -> None:
    assert list(PROOFS) == [f"lp{i}" for i in range(1, 12)]
    assert all(callable(fn) for fn in PROOFS.values())
