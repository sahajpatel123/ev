"""A bare "yes" answering Evie's live offer must not retrieve memories.

The owner asked for a mail search, Evie found it and asked "do you want me to
read out the full mail?", he said "yes" — and got a topic-free answer. The
utterance classifies as memory intent ``continuation``, which took the
unfiltered retrieval branch: the retriever's per-query min-max normalisation
guarantees its top candidate clears the continuation threshold, so whatever
the pool held was injected as if it were what he had just asked about.

The turn is an answer, not a recall request. Its referent is the offer (and
the turn ledger), which the prompt already carries.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cognitive.intent import pending_offer, set_pending_offer
from app.cognitive.session_store import current, reset_for_tests
from app.contracts import RetrievedMemory
from app.memory.select import explicit_recall_payload, select_context_memories
from app.utils.text import utcnow

_MAIL_OFFER = "I found the mail from Rahul. Do you want me to read out the full mail?"
_QUESTION = "what did the mail from Rahul say?"
_UNRELATED_ID = "mem-rowing"
_UNRELATED_TEXT = "The owner prefers rowing over running in the morning."


@pytest.fixture(autouse=True)
def _clean_session(isolated_storage):
    """The pending offer is durable JSON: never inherit another test's offer.

    Depends on the storage isolation so ``reset_for_tests`` can only ever
    touch the per-test directory, never the owner's live session file.
    """

    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def stub_retriever(monkeypatch: pytest.MonkeyPatch):
    """Stand-in for the real index: records every retrieval query."""

    class StubRetriever:
        instances: list[StubRetriever] = []

        def __init__(self, session: AsyncSession) -> None:
            self.session = session
            self.queries: list[str] = []
            type(self).instances.append(self)

        async def search(self, query: str, **_kwargs) -> list[RetrievedMemory]:
            self.queries.append(query)
            return [_unrelated()]

    monkeypatch.setattr("app.memory.select.Retriever", StubRetriever)
    return StubRetriever


def _unrelated() -> RetrievedMemory:
    return RetrievedMemory(
        memory_id=_UNRELATED_ID,
        text=_UNRELATED_TEXT,
        memory_type="fact",
        payload={},
        importance=0.8,
        confidence=0.9,
        event_time=utcnow(),
        privacy_level="normal",
        source_type="explicit",
        score=0.91,
        components={"semantic_raw": 0.9},
    )


def _arm_offer() -> None:
    set_pending_offer(
        current(),
        _MAIL_OFFER,
        action={"tool": "life.mail", "args": {"query": "search for Rahul in mail"}},
    )
    assert pending_offer(current()) is not None, "the offer under test was not armed"


async def test_bare_yes_answering_a_live_offer_injects_no_memories(
    db_session: AsyncSession, stub_retriever
) -> None:
    _arm_offer()

    intent, memories = await select_context_memories(db_session, "yes", k=8)

    assert intent == "continuation"
    assert memories == []
    assert stub_retriever.instances == [], "an answered offer must not search the index"


async def test_negative_reply_answering_a_live_offer_injects_no_memories(
    db_session: AsyncSession, stub_retriever
) -> None:
    _arm_offer()

    intent, memories = await select_context_memories(db_session, "no", k=8)

    assert intent == "continuation"
    assert memories == []
    assert stub_retriever.instances == []


async def test_bare_yes_without_a_live_offer_retrieves_as_before(
    db_session: AsyncSession, stub_retriever
) -> None:
    assert pending_offer(current()) is None

    intent, memories = await select_context_memories(db_session, "yes", k=8)

    assert intent == "continuation"
    assert [memory.memory_id for memory in memories] == [_UNRELATED_ID]
    assert stub_retriever.instances[0].queries == ["yes"]


async def test_a_real_question_with_a_live_offer_still_retrieves(
    db_session: AsyncSession, stub_retriever
) -> None:
    _arm_offer()

    intent, memories = await select_context_memories(db_session, _QUESTION, k=8)

    assert intent == "explicit_recall"
    assert [memory.memory_id for memory in memories] == [_UNRELATED_ID]
    assert stub_retriever.instances[0].queries == [_QUESTION]


async def test_a_long_affirmative_is_not_a_reply_and_still_retrieves(
    db_session: AsyncSession, stub_retriever
) -> None:
    """The reply grammar is short by contract; a sentence keeps its retrieval."""

    _arm_offer()
    text = "yes the mail from Rahul was long"

    _, memories = await select_context_memories(db_session, text, k=8)

    assert memories
    assert stub_retriever.instances[0].queries == [text]


async def test_unreadable_session_fails_open(db_session: AsyncSession, stub_retriever, monkeypatch) -> None:
    """An unreadable ledger must not silently mute recall for every turn."""

    def _unreadable():
        raise OSError("session ledger unreadable")

    monkeypatch.setattr("app.cognitive.session_store.current", _unreadable)

    _, memories = await select_context_memories(db_session, "yes", k=8)

    assert [memory.memory_id for memory in memories] == [_UNRELATED_ID]
    assert stub_retriever.instances[0].queries == ["yes"]


async def test_explicit_recall_prefetch_skips_an_offer_answer(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel prefetch hands the owner's raw words to explicit fusion."""

    _arm_offer()

    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("explicit fusion ran for an answered offer")

    monkeypatch.setattr("app.memory.recall.build_explicit_recall_payload", _must_not_run)

    pack = await explicit_recall_payload(db_session, "yes", k=4)

    assert pack["evidence"] == []
    assert pack["results"] == []
    assert pack["count"] == 0
    assert pack["grounding"] == "none"
    assert pack["skipped"] == "answers_live_offer"


async def test_explicit_recall_prefetch_delegates_a_real_question(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm_offer()
    seen: list[str] = []

    async def _fake(_session, query, **_kwargs):
        seen.append(query)
        return {"ok": True, "evidence": [{"text": _UNRELATED_TEXT}]}

    monkeypatch.setattr("app.memory.recall.build_explicit_recall_payload", _fake)

    pack = await explicit_recall_payload(db_session, _QUESTION, k=4)

    assert seen == [_QUESTION]
    assert pack["evidence"] == [{"text": _UNRELATED_TEXT}]
