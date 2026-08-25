"""G2 P0.2 — EVENT STREAM DELIVERY ORDER (stream_seq).

Separates SEMANTIC TIME from STREAM POSITION:

  occurred_at  = when the semantic event happened (immutable truth)
  stream_seq   = when the event entered THIS canonical delivery lineage
                 (server-assigned, monotonically increasing, immutable)

Backfill assigns positions deterministically by the prior semantic order
(occurred_at, id). The durable single-row allocator (event_stream_position)
then hands out every future position — including late-arriving / recovered /
imported events — so authentic history can never fall behind an
already-issued cursor.

Legacy G2 prototype cursors (epoch-less or epoch|timestamp|uuid shapes)
are rejected with CURSOR_FORMAT_UPGRADE -> fresh bootstrap. Physical G2
sync clients are not yet established, making this a clean cutover.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p2q3r4s5t6u7"
down_revision = "o1p2q3r4s5t6"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {
        c["name"] for c in sa.inspect(bind).get_columns(table)
    }


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "events" not in tables:
        return

    if not _has_column(bind, "events", "stream_seq"):
        op.add_column("events", sa.Column("stream_seq", sa.BigInteger(), nullable=True))

    # Deterministic backfill: prior semantic order defines initial positions.
    bind.execute(
        sa.text(
            """
            WITH ordered AS (
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY occurred_at ASC, id ASC
                ) AS rn
                FROM events
                WHERE stream_seq IS NULL
            )
            UPDATE events e
            SET stream_seq = o.rn
            FROM ordered o
            WHERE e.id = o.id AND e.stream_seq IS NULL
            """
        )
    )

    existing_ix = {
        ix["name"] for ix in sa.inspect(bind).get_indexes("events")
    }
    if "ix_events_stream_seq" not in existing_ix:
        op.create_index(
            "ix_events_stream_seq", "events", ["stream_seq"], unique=True
        )

    # Durable monotonic allocator (PART 2): single-row counter updated inside
    # each inserting transaction. Row locking serializes concurrency; the
    # counter seeds past every backfilled position.
    if "event_stream_position" not in tables:
        op.create_table(
            "event_stream_position",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("last_seq", sa.BigInteger(), nullable=False),
        )
    max_row = bind.execute(
        sa.text("SELECT COALESCE(MAX(stream_seq), 0) FROM events")
    ).scalar_one()
    bind.execute(
        sa.text(
            """
            INSERT INTO event_stream_position (id, last_seq)
            VALUES (1, :seed)
            ON CONFLICT (id) DO UPDATE
              SET last_seq = GREATEST(event_stream_position.last_seq, EXCLUDED.last_seq)
            """
        ),
        {"seed": int(max_row)},
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "event_stream_position" in tables:
        op.drop_table("event_stream_position")
    if "events" in tables:
        ix_names = {ix["name"] for ix in sa.inspect(bind).get_indexes("events")}
        if "ix_events_stream_seq" in ix_names:
            op.drop_index("ix_events_stream_seq", table_name="events")
        if _has_column(bind, "events", "stream_seq"):
            op.drop_column("events", "stream_seq")
