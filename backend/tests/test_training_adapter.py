"""Tests for the versioned adapter registry with eval gates (Training track 7.3)."""

from __future__ import annotations

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdapterRegistration, ResponseLog, VoiceSession
from app.training import adapter as adapter_service


async def grant_adapter_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/training/consent",
        json={"track": "adapter_fine_tuning"},
    )
    assert resp.status_code == 201, resp.text


async def build_corpus(
    client: AsyncClient,
    db_session: AsyncSession,
    *,
    with_correction: bool = True,
) -> int:
    await client.post(
        "/v1/training/consent",
        json={"track": "training_corpus"},
    )
    if with_correction:
        db_session.add(
            ResponseLog(
                request_text="Fix that: I actually prefer PostgreSQL.",
                reply_text="Corrected to PostgreSQL.",
                mode="casual",
                strategy={},
                provenance_ids=[],
                context_tokens=10,
                was_correction=True,
            )
        )
        await db_session.commit()
    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 201, resp.text
    return resp.json()["snapshot"]["version"]


class _FakeProvider(adapter_service.WeightTrainingProvider):
    """Deterministic in-memory provider for contract tests."""

    name = "fake-provider"
    supports_remote = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    async def estimate(
        self, dataset: adapter_service.TrainingDataset, *, base_model: str | None = None
    ) -> dict:
        return {"provider": self.name, "estimated_cost_usd": 0.0}

    async def train(
        self,
        dataset: adapter_service.TrainingDataset,
        *,
        base_model: str | None = None,
        adapter_ref: str | None = None,
        cost_approved: bool = False,
    ) -> dict:
        self.calls.append(
            {"dataset": dataset, "base_model": base_model, "cost_approved": cost_approved}
        )
        return {
            "provider": self.name,
            "adapter_ref": "fake://adapter-v1",
            "status": "completed",
        }


def _register(fake: _FakeProvider) -> None:
    adapter_service.register_training_provider(fake.name, lambda: fake)


def _unregister(fake: _FakeProvider) -> None:
    adapter_service.PROVIDER_REGISTRY.pop(fake.name, None)


async def test_adapter_register_requires_consent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session)
    resp = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-v1-lora", "corpus_version": version},
    )
    assert resp.status_code == 403
    assert "adapter_fine_tuning" in resp.json()["detail"]


async def test_adapter_register_runs_eval_gates_and_versions(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)

    resp = await client.post(
        "/v1/training/adapter/register",
        json={
            "name": "evie-v1-lora",
            "provider": "local-lora",
            "base_model": "deepseek-v4-flash-0731",
            "adapter_ref": "adapters/evie-v1",
            "corpus_version": version,
        },
    )
    assert resp.status_code == 201, resp.text
    first = resp.json()
    assert first["version"] == 1
    assert first["status"] == "approved"
    assert first["is_current"] is False
    assert first["eval_metrics"]["passed"] is True
    assert first["eval_metrics"]["gates"]["corrections_present"] is True

    resp = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-v1-lora", "corpus_version": version},
    )
    assert resp.status_code == 201
    second = resp.json()
    assert second["version"] == 2
    assert second["supersedes_id"] == first["id"]

    resp = await client.post(
        "/v1/training/adapter/activate",
        json={"adapter_id": second["id"], "reason": "new voice/style adapter"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_current"] is True
    assert resp.json()["status"] == "active"

    resp = await client.post(
        "/v1/training/adapter/rollback",
        json={"adapter_id": second["id"], "reason": "regression"},
    )
    assert resp.status_code == 200, resp.text
    rolled_back = resp.json()
    assert rolled_back["id"] == first["id"]
    assert rolled_back["is_current"] is True
    assert rolled_back["status"] == "active"

    resp = await client.get("/v1/training/adapter")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_adapter_register_rejects_empty_corpus(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session, with_correction=False)
    await grant_adapter_consent(client)
    resp = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-empty-lora", "corpus_version": version},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["status"] == "rejected"
    assert payload["eval_metrics"]["passed"] is False


async def test_adapter_delete_redacts_all(client: AsyncClient, db_session: AsyncSession) -> None:
    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)
    resp = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-delete-lora", "corpus_version": version},
    )
    assert resp.status_code == 201

    resp = await client.post("/v1/training/adapter/delete")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 1

    rows = list((await db_session.execute(select(AdapterRegistration))).scalars().all())
    assert len(rows) == 1
    assert rows[0].redacted is True
    assert rows[0].status == "deleted"
    assert rows[0].eval_metrics == {}


async def test_adapter_dry_run_validates_without_external_call(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)
    fake = _FakeProvider("fake-dry-run")
    _register(fake)
    try:
        resp = await client.post(
            "/v1/training/adapter/dry-run",
            json={"corpus_version": version, "provider": fake.name},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "dry_run"
        assert body["passed"] is True
        assert body["gates"]["correction_rate"] >= 0.1
        assert body["dataset"]["record_count"] >= 1
        assert body["plan"]["estimated_cost_usd"] == 0.0
        assert fake.calls == []
    finally:
        _unregister(fake)


async def test_adapter_train_calls_provider_and_persists_ref(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)
    registered = await client.post(
        "/v1/training/adapter/register",
        json={
            "name": "evie-train-contract",
            "provider": "local-lora",
            "base_model": "base-model",
            "corpus_version": version,
        },
    )
    assert registered.status_code == 201, registered.text
    adapter_id = registered.json()["id"]

    fake = _FakeProvider("fake-train")
    _register(fake)
    try:
        resp = await client.post(
            "/v1/training/adapter/train",
            json={
                "corpus_version": version,
                "provider": fake.name,
                "adapter_id": adapter_id,
                "cost_approved": True,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "train"
        assert body["result"]["adapter_ref"] == "fake://adapter-v1"
        assert fake.calls and fake.calls[0]["cost_approved"] is True
        assert fake.calls[0]["dataset"].record_count >= 1

        row = (
            await db_session.execute(
                select(AdapterRegistration).where(
                    AdapterRegistration.id == UUID(adapter_id)
                )
            )
        ).scalar_one()
        assert row.adapter_ref == "fake://adapter-v1"
        assert row.eval_metrics["training_run"]["status"] == "completed"
    finally:
        _unregister(fake)


async def test_adapter_train_requires_cost_approval(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)

    class _CostProvider(_FakeProvider):
        async def estimate(
            self, dataset: adapter_service.TrainingDataset, *, base_model: str | None = None
        ) -> dict:
            return {"provider": self.name, "estimated_cost_usd": 1.25}

    fake = _CostProvider("fake-cost")
    _register(fake)
    try:
        resp = await client.post(
            "/v1/training/adapter/train",
            json={"corpus_version": version, "provider": fake.name},
        )
        assert resp.status_code == 422, resp.text
        assert "cost" in resp.json()["detail"].lower()
        assert fake.calls == []
    finally:
        _unregister(fake)


async def test_adapter_train_remote_requires_env_gate(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)

    class _RemoteProvider(_FakeProvider):
        supports_remote = True

    fake = _RemoteProvider("fake-remote")
    _register(fake)
    monkeypatch.setenv("EV_ALLOW_REMOTE_TRAINING", "false")
    try:
        resp = await client.post(
            "/v1/training/adapter/train",
            json={"corpus_version": version, "provider": fake.name, "cost_approved": True},
        )
        assert resp.status_code == 422, resp.text
        assert "EV_ALLOW_REMOTE_TRAINING" in resp.json()["detail"]
        assert fake.calls == []
    finally:
        _unregister(fake)


async def test_adapter_train_rejects_failed_gates(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session, with_correction=False)
    await grant_adapter_consent(client)
    fake = _FakeProvider("fake-reject")
    _register(fake)
    try:
        resp = await client.post(
            "/v1/training/adapter/train",
            json={"corpus_version": version, "provider": fake.name, "cost_approved": True},
        )
        assert resp.status_code == 422, resp.text
        assert fake.calls == []
    finally:
        _unregister(fake)


async def test_adapter_train_refuses_during_voice_session(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)
    db_session.add(
        VoiceSession(
            device_id="test-device",
            wake_word="evie",
            state="awake",
        )
    )
    await db_session.commit()
    fake = _FakeProvider("fake-voice")
    _register(fake)
    try:
        resp = await client.post(
            "/v1/training/adapter/train",
            json={"corpus_version": version, "provider": fake.name, "cost_approved": True},
        )
        assert resp.status_code == 422, resp.text
        assert "voice session" in resp.json()["detail"].lower()
        assert fake.calls == []
    finally:
        _unregister(fake)


async def test_adapter_activate_requires_completed_real_training_run(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)
    resp = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-real-gate", "corpus_version": version},
    )
    assert resp.status_code == 201, resp.text
    adapter_id = resp.json()["id"]

    row = (
        await db_session.execute(
            select(AdapterRegistration).where(AdapterRegistration.id == UUID(adapter_id))
        )
    ).scalar_one()
    row.eval_metrics = {
        **(row.eval_metrics or {}),
        "training_run": {"status": "running", "real_weights": True},
    }
    await db_session.commit()

    resp = await client.post(
        "/v1/training/adapter/activate",
        json={"adapter_id": adapter_id, "reason": "should refuse unfinished run"},
    )
    assert resp.status_code == 422, resp.text
    assert "completed" in resp.json()["detail"]

    row.eval_metrics = {
        **(row.eval_metrics or {}),
        "training_run": {"status": "completed", "real_weights": False},
    }
    await db_session.commit()
    resp = await client.post(
        "/v1/training/adapter/activate",
        json={"adapter_id": adapter_id, "reason": "should refuse simulated run"},
    )
    assert resp.status_code == 422, resp.text
    assert "no real weights" in resp.json()["detail"]

    row.eval_metrics = {
        **(row.eval_metrics or {}),
        "training_run": {
            "status": "completed",
            "real_weights": True,
            "eval": {"win_rate": 0.6, "loss_curves": {"train": [1.0], "val": [1.1]}},
        },
    }
    await db_session.commit()
    resp = await client.post(
        "/v1/training/adapter/activate",
        json={"adapter_id": adapter_id, "reason": "real completed run"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"


async def test_adapter_train_staged_provider_refuses_without_local_target(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    """Staged weight training refuses until a servable local target exists."""

    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)

    class _StagedProvider(_FakeProvider):
        staged = True

    fake = _StagedProvider("staged-provider")
    _register(fake)
    try:
        resp = await client.post(
            "/v1/training/adapter/train",
            json={"corpus_version": version, "provider": fake.name, "cost_approved": True},
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"].lower()
        assert "staged" in detail
        assert "no servable local inference target" in detail
        assert fake.calls == []

        monkeypatch.setattr(adapter_service.settings, "chat_provider", "local")
        monkeypatch.setattr(
            adapter_service.settings, "local_model_base_url", "http://localhost:11434/v1"
        )
        resp = await client.post(
            "/v1/training/adapter/train",
            json={"corpus_version": version, "provider": fake.name, "cost_approved": True},
        )
        assert resp.status_code == 200, resp.text
        assert fake.calls and fake.calls[0]["cost_approved"] is True
    finally:
        _unregister(fake)
