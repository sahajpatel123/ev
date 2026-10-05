"""Regression tests for proactive calibration queries.

proactive_tuning() runs on every chat turn. It must aggregate the
predictions table in SQL instead of materializing every row: production
holds ~70k prediction rows (~1GB), and the unscoped ORM scan cost 20+s
per turn.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import event

from app.db import engine
from app.ev.calibration import proactive_tuning
from app.models import Prediction, ResponseLog


def _prediction(*, outcome="pending", reviewed=False, kind="test"):
    return Prediction(
        kind=kind,
        text="sense prediction",
        confidence=0.7,
        basis_ids=[],
        rationale="why",
        outcome=outcome,
        reviewed_at=(
            datetime(2026, 1, 1, tzinfo=UTC) if reviewed else None
        ),
        details={},
    )


async def test_prediction_accuracy_counts_reviewed_only(db_session):
    db_session.add_all(
        [
            _prediction(outcome="correct", reviewed=True),
            _prediction(outcome="correct", reviewed=True),
            _prediction(outcome="incorrect", reviewed=True),
            _prediction(outcome="pending", reviewed=False),
            _prediction(outcome="correct", reviewed=False),
        ]
    )
    await db_session.commit()

    tuning = await proactive_tuning(db_session)

    assert tuning.prediction_accuracy == round(2 / 3, 3)


async def test_prediction_accuracy_none_when_nothing_reviewed(db_session):
    db_session.add(_prediction(outcome="pending", reviewed=False))
    await db_session.commit()

    tuning = await proactive_tuning(db_session)

    assert tuning.prediction_accuracy is None


async def test_tuning_issues_bounded_queries(db_session):
    db_session.add_all(
        [_prediction(outcome="correct", reviewed=True) for _ in range(50)]
    )
    db_session.add(
        ResponseLog(
            request_text="q",
            reply_text="a",
            mode="chat",
            strategy={"challenge": True},
        )
    )
    await db_session.commit()

    statements = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _count)
    try:
        await proactive_tuning(db_session)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _count)

    selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
    # One aggregate over predictions + one small scan over response_log.
    # Must not grow with the predictions row count.
    assert len(selects) <= 3
    assert not any("predictions" in s and "COUNT" not in s.upper() for s in selects)
