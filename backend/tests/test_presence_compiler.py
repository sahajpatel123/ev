"""Presence compiler hermetic tests (Goal Presence OS V1).

Offline: no network, no keys. The provider factory is monkeypatched in every
test. The fixed-JSON provider doubles below exercise validation *plumbing*
only — they are not proof that Spark itself produces good graphs.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PresenceContract
from app.presence import service as presence
from app.presence.compiler import GRAPH_SCHEMA, SparkUnavailable, compile_graph


def _graph_payload(nodes: list[dict[str, Any]]) -> str:
    return json.dumps({"nodes": nodes})


def _provider_with(text: str) -> Any:
    class _Prov:
        async def chat_structured(self, messages: Any, **kwargs: Any) -> Any:
            assert kwargs.get("schema_name") == "presence_graph"
            assert kwargs.get("schema") == GRAPH_SCHEMA
            return SimpleNamespace(text=text)

    return _Prov()


@pytest.mark.asyncio
async def test_missing_key_raises_without_network(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)

    def _explode() -> Any:
        raise AssertionError("no network attempt when key is missing")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", _explode)
    row = await presence.create_contract(db_session, objective="Watch the build")
    with pytest.raises(SparkUnavailable, match="META_MODEL_API_KEY"):
        await compile_graph(db_session, row)


@pytest.mark.asyncio
async def test_validation_rejects_bad_nodes_plumbing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plumbing double, not a Spark proof: fixed result.text exercises validation.
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_model", lambda: "muse-spark-test")
    payload = _graph_payload(
        [
            {
                "node_id": "good",
                "kind": "CORE_READ",
                "target": "CORE",
                "effect": "read status",
                "risk": "R1",
                "depends_on": [],
                "verification": "row present",
            },
            {"node_id": "bad-kind", "kind": "FLY_JETPACK", "target": "CORE", "risk": "R1"},
            {"node_id": "bad-target", "kind": "CORE_READ", "target": "MOON", "risk": "R1"},
            {
                "node_id": "bad-dep",
                "kind": "CORE_READ",
                "target": "CORE",
                "risk": "R1",
                "depends_on": ["nope"],
            },
            {"node_id": "", "kind": "CORE_READ", "target": "CORE", "risk": "R1"},
            {"node_id": "bad-risk", "kind": "CORE_READ", "target": "CORE", "risk": "R99"},
        ]
    )
    monkeypatch.setattr(
        "app.gateway.muse_spark.muse_spark_provider",
        lambda: _provider_with(payload),
    )
    row = await presence.create_contract(db_session, objective="Compile me")
    out = await compile_graph(db_session, row)

    assert [n["node_id"] for n in out["nodes"]] == ["good"]
    assert out["model"] == "muse-spark-test"
    joined = " ".join(out["warnings"])
    assert "bad-kind" in joined and "bad-target" in joined and "bad-dep" in joined

    db_session.expunge(row)
    fresh = await db_session.get(PresenceContract, row.id)
    assert fresh is not None
    stored = [n.get("node_id") for n in (fresh.graph or {}).get("nodes") or []]
    assert stored == ["good"]


@pytest.mark.asyncio
async def test_ceiling_warning_keeps_node(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plumbing double, not a Spark proof: fixed result.text exercises the ceiling path.
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_model", lambda: "muse-spark-test")
    payload = _graph_payload(
        [
            {
                "node_id": "risky",
                "kind": "CLOUD_JOB",
                "target": "CLOUD",
                "effect": "fan out",
                "risk": "R3",
                "depends_on": [],
                "verification": "job id",
            }
        ]
    )
    monkeypatch.setattr(
        "app.gateway.muse_spark.muse_spark_provider",
        lambda: _provider_with(payload),
    )
    row = await presence.create_contract(db_session, objective="Risky thing", risk_ceiling="R1")
    out = await compile_graph(db_session, row)

    assert [n["node_id"] for n in out["nodes"]] == ["risky"]
    assert any("above ceiling" in w for w in out["warnings"])

    db_session.expunge(row)
    fresh = await db_session.get(PresenceContract, row.id)
    assert fresh is not None
    stored = [n.get("node_id") for n in (fresh.graph or {}).get("nodes") or []]
    assert stored == ["risky"]


def test_graph_schema_matches_contract_vocab() -> None:
    from app.presence.compiler import _SPARK_SYSTEM, _validate
    from app.presence.contract import NodeKind, NodeTarget

    # Upstream validator rejects large enum sets, so the wire schema keeps
    # kind/target as plain strings; the closed vocabularies live in the
    # planner prompt and in _validate. Both must cover every enum value.
    for k in NodeKind:
        assert k.value in _SPARK_SYSTEM
    for t in NodeTarget:
        assert t.value in _SPARK_SYSTEM
    nodes, warnings = _validate(
        {"nodes": [
            {"node_id": "bad-kind", "kind": "NOPE", "target": "CORE"},
            {"node_id": "bad-target", "kind": "CORE_READ", "target": "MOON"},
            {"node_id": "ok", "kind": "CORE_READ", "target": "CORE"},
        ]},
        risk_ceiling="R4",
    )
    assert [n["node_id"] for n in nodes] == ["ok"]
    assert len(warnings) == 2
