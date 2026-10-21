"""S41: acknowledgement p95 through the real ASGI app is within 1500 ms.

The first SSE event is ``status/accepted``, emitted before the chat pipeline
runs. The pipeline is held open on purpose so a slow model cannot be counted
as the acknowledgement. The 1500 ms figure is the unchanged budget in
``app.ops.budgets`` (``chat_first_token``) and in S18.
"""

from __future__ import annotations

import app.ops.budgets as budgets
from app.scripts.latency_probe import ACK_BUDGET_MS, measure_ack_asgi


async def test_s41_ack_p95_through_real_asgi() -> None:
    assert budgets.LATENCY_BUDGETS_MS["chat_first_token"] == 1500
    assert ACK_BUDGET_MS == 1500
    measured = await measure_ack_asgi(20)
    assert measured["trials"] == 20, measured
    assert measured["p95_ms"] <= ACK_BUDGET_MS, measured
    # The pipeline was still blocked when each ack was observed.
    assert measured["pipeline_finished"] == 0, measured
