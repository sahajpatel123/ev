"""F1 §32 parity: router shadow retrieval vs the direct search_memory path.

Both paths consume the SAME underlying retriever/recall infrastructure; the
router must be equal or better on explicit historical questions and never
worse on authority behavior. search_memory remains untouched (F4 removes
nothing).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.recall import build_explicit_recall_payload
from app.memory.select import select_context_memories
from app.memory.shadow import route_turn
from app.models import Memory
from app.utils.text import fingerprint, utcnow

# Small evaluation set: (question, target memory text, distractor count)
_EVAL_SET = [
    ("What did we decide about the IIT timeline?", "We decided the IIT attempt starts in December.", "decision", 8),
    ("Which model did I choose for the orchestrator?", "The orchestrator uses the broker pattern for tools.", "decision", 8),
    ("What do I prefer for morning workouts?", "The owner prefers rowing over running in the morning.", "preference", 8),
    ("Where were we on the memory system?", "The memory system shadows context per turn before injection.", "summary", 8),
    ("What was the name of that experiment?", "The experiment was called Project Ironwood.", "fact", 8),
]

_Distractors = [
    "Unrelated fact {i}: the sky was cloudy on day {i}.",
    "Preference {i}: likes notebook brand {i}.",
    "Episodic {i}: walked to the store on day {i}.",
    "Pattern {i}: tends to code late at night on day {i}.",
    "Summary {i}: week {i} was quiet.",
]


def _memory(text: str, *, mtype: str = "decision", importance: float = 0.85) -> Memory:
    now = utcnow()
    return Memory(
        memory_type=mtype,
        text=text,
        payload={},
        importance=importance,
        confidence=0.9,
        source_type="explicit",
        privacy_level="normal",
        event_time=now,
        valid_from=now,
        is_current=True,
        fingerprint=fingerprint({"seed": uuid4().hex}),
        embedding=None,
        embedding_model_version=None,
    )


@pytest.fixture(autouse=True)
def _restore_gate():
    previous = settings.memory_gate
    yield
    settings.memory_gate = previous


@pytest.mark.asyncio
async def test_router_parity_with_search_memory(db_session: AsyncSession) -> None:
    settings.memory_gate = "on"

    corpus = [
        _memory(target, mtype=mtype)
        for question, target, mtype, _ in _EVAL_SET
    ]
    for index, question_target in enumerate(_EVAL_SET):
        for j in range(question_target[3]):
            template = _Distractors[(index + j) % len(_Distractors)]
            corpus.append(
                _memory(
                    template.format(i=index * 10 + j),
                    mtype=template.split(" ")[0].lower().rstrip("0123456789"),
                    importance=0.4,
                )
            )
    for row in corpus:
        db_session.add(row)
    await db_session.commit()

    old_hits = 0
    new_hits = 0
    rows: list[dict] = []
    for question, target, _mtype, _ in _EVAL_SET:
        # OLD path: what search_memory runs today (explicit recall fusion).
        payload = await build_explicit_recall_payload(db_session, question, k=10)
        old_blob = " ".join(
            str(item.get("text") or "") for item in (payload.get("evidence") or [])
        )
        old_hit = target.lower() in old_blob.lower()

        # Chat-path selection (explicit selection used by /v1 chat compile).
        _intent, memories = await select_context_memories(db_session, question, k=10)
        select_hit = any(target.lower() in m.text.lower() for m in memories)

        # NEW path: router envelope.
        envelope = await route_turn(
            db_session, query=question, turn_id=f"parity-{uuid4().hex}"
        )
        new_blob = " ".join(item.text for item in (envelope.items if envelope else []))
        new_hit = target.lower() in new_blob.lower()

        old_hits += int(old_hit or select_hit)
        new_hits += int(new_hit)
        rows.append(
            {
                "question": question[:48],
                "old": old_hit or select_hit,
                "new": new_hit,
                "items": len(envelope.items) if envelope else 0,
            }
        )

    print(f"\n[parity] old={old_hits}/{len(_EVAL_SET)} new={new_hits}/{len(_EVAL_SET)}")
    for row in rows:
        print(f"[parity] {row}")

    # Router must be no worse than the legacy paths on factual recall.
    assert new_hits >= old_hits, f"router regressed: new={new_hits} old={old_hits}"
    # And must hit at least 4/5 on the seeded corpus.
    assert new_hits >= 4, f"router recall too low: {new_hits}/{len(_EVAL_SET)}"
