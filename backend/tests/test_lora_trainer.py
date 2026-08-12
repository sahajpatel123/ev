"""FORGE acceptance tests: MLX LoRA provider, eval harness, privacy, rollback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FilterLedger, ResponseLog
from app.training import adapter as adapter_service
from app.training import corpus as corpus_service
from app.training.eval import (
    evaluate_models,
    held_out_split,
    hud_conformance,
    overfit_report,
    tool_call_validity,
)
from app.training.lora import LoRASettings, MLXLoRAProvider
from app.training.lora_runner import (
    _parse_loss_lines,
    _to_chat_rows,
    base_directory_hash,
    finalize_adapter,
    rollback_adapter,
    sha256_file,
)


def _dataset(pairs: list[tuple[str, str]], *, version: int = 1) -> adapter_service.TrainingDataset:
    records = [
        {
            "kind": "response",
            "input": instruction,
            "output": reply,
            "signals": {"mode": "casual", "was_correction": i % 2 == 0},
            "source": f"response_log:r{i}",
        }
        for i, (instruction, reply) in enumerate(pairs)
    ]
    for record in records:
        record["hash"] = corpus_service._entry_hash(record)
    return adapter_service.TrainingDataset(
        corpus_version=version,
        records=records,
        jsonl=corpus_service.to_jsonl(records),
        content_hash="test-content-hash",
    )


def _provider(
    tmp_path: Path,
    *,
    min_pairs: int = 1,
    force_double: bool = True,
    train_mode: str = "sft",
) -> MLXLoRAProvider:
    settings = LoRASettings(
        output_root=tmp_path,
        min_owner_pairs=min_pairs,
        force_double=force_double,
        train_mode=train_mode,
    )
    return MLXLoRAProvider(settings)


def _preference_dataset(version: int = 1) -> adapter_service.TrainingDataset:
    records = [
        {
            "kind": "preference",
            "prompt": "",
            "chosen": "That may have happened.",
            "rejected": "That definitely happened.",
            "signals": {"stage": "output"},
            "source": f"filter_ledger:f{i}",
        }
        for i in range(2)
    ]
    for record in records:
        record["hash"] = corpus_service._entry_hash(record)
    return adapter_service.TrainingDataset(
        corpus_version=version,
        records=records,
        jsonl=corpus_service.to_jsonl(records),
        content_hash="pref-hash",
    )


def test_mlx_lora_provider_is_registered_factory_entry() -> None:
    provider = adapter_service.get_training_provider("mlx-lora")
    assert isinstance(provider, MLXLoRAProvider)
    assert provider.name == "mlx-lora"
    assert provider.supports_remote is False
    assert provider.staged is True


def test_to_chat_rows_builds_messages() -> None:
    rows = _to_chat_rows(
        [
            {"instruction": "Fix that.", "output": "Fixed."},
            {"instruction": "", "output": "skipped"},
        ]
    )
    assert rows == [
        {
            "messages": [
                {"role": "user", "content": "Fix that."},
                {"role": "assistant", "content": "Fixed."},
            ]
        }
    ]


def test_parse_loss_lines_handles_sft_and_dpo_formats(tmp_path: Path) -> None:
    status = tmp_path / "status.jsonl"
    status.write_text(
        "\n".join(
            [
                json.dumps({"line": "Iter 5: Train loss 3.732, Learning Rate 1e-4"}),
                json.dumps({"line": "Step 5/8 | Loss: 0.7090 | batch_size: 1"}),
                json.dumps({"line": "Iter 10: Val loss 0.113, Val took 1.1s"}),
            ]
        )
    )
    train, val = _parse_loss_lines(status)
    assert train == [3.732, 0.709]
    assert val == [0.113]


def test_finalize_refuses_empty_adapter(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "COMPLETE").write_text("completed")
    (run_dir / "adapter").mkdir()
    with pytest.raises(RuntimeError, match="empty adapter"):
        finalize_adapter(run_dir, target_root=tmp_path / "adapters", name="evie-v1")


async def test_estimate_is_local_deterministic_and_costs_zero(tmp_path: Path) -> None:
    provider = _provider(tmp_path, min_pairs=1)
    dataset = _dataset([("Fix that", "Fixed."), ("Shorten it", "Done.")])

    plan = await provider.estimate(dataset, base_model="Qwen/Qwen3-0.6B")

    assert plan["provider"] == "mlx-lora"
    assert plan["estimated_cost_usd"] == 0.0
    assert plan["owner_pairs"] == 2
    assert plan["estimated_peak_mb"] <= 3500
    assert plan["tier"] == "exclusive"
    again = await provider.estimate(dataset, base_model="Qwen/Qwen3-0.6B")
    assert again == plan


async def test_estimate_refuses_below_min_owner_pairs(tmp_path: Path) -> None:
    provider = _provider(tmp_path, min_pairs=200)
    dataset = _dataset([("Fix that", "Fixed.")])

    with pytest.raises(adapter_service.TrainingRunError, match="requires at least 200"):
        await provider.estimate(dataset)


async def test_train_degraded_double_never_claims_real_weights(tmp_path: Path) -> None:
    provider = _provider(tmp_path, min_pairs=1, force_double=True)
    dataset = _dataset([("Fix that", "Fixed."), ("Shorten it", "Done.")])

    result = await provider.train(dataset, base_model="Qwen/Qwen3-0.6B")

    assert result["status"] == "completed"
    assert result["degraded"] is True
    assert result["simulated"] is True
    assert result["real_weights"] is False
    adapter_ref = Path(result["adapter_ref"])
    assert (adapter_ref / "COMPLETE").exists()
    assert (adapter_ref / "adapter" / "manifest.json").exists()
    assert (adapter_ref / "eval.json").exists()
    assert (adapter_ref / "losses.jsonl").exists()
    manifest = json.loads((adapter_ref / "adapter" / "manifest.json").read_text())
    assert manifest["degraded"] is True
    assert manifest["real_weights"] is False
    evaluation = json.loads((adapter_ref / "eval.json").read_text())["evaluation"]
    assert evaluation["measured"] is False
    assert evaluation["win_rate"] is None
    losses = [
        json.loads(line) for line in (adapter_ref / "losses.jsonl").read_text().splitlines()
    ]
    assert any(row["split"] == "train" for row in losses)
    assert any(row["split"] == "val" for row in losses)


async def test_train_refuses_below_min_owner_pairs(tmp_path: Path) -> None:
    provider = _provider(tmp_path, min_pairs=200, force_double=True)
    dataset = _dataset([("Fix that", "Fixed.")])

    with pytest.raises(adapter_service.TrainingRunError, match="requires at least 200"):
        await provider.train(dataset)
    assert not (tmp_path / "runs").exists()


async def test_dpo_estimate_and_double_train(tmp_path: Path) -> None:
    provider = _provider(tmp_path, min_pairs=1, force_double=True, train_mode="dpo")
    dataset = _preference_dataset()

    plan = await provider.estimate(dataset)
    assert plan["owner_pairs"] == 2
    assert plan["estimated_cost_usd"] == 0.0

    result = await provider.train(dataset)
    assert result["status"] == "completed"
    assert result["degraded"] is True
    assert result["real_weights"] is False


async def test_dpo_refuses_without_preference_rows(tmp_path: Path) -> None:
    provider = _provider(tmp_path, min_pairs=1, force_double=True, train_mode="dpo")
    dataset = _dataset([("Fix that", "Fixed.")])
    with pytest.raises(adapter_service.TrainingRunError, match="preference pairs"):
        await provider.train(dataset)


def test_rollback_removes_active_pointer_and_proves_base_byte_identical(
    tmp_path: Path, monkeypatch
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "weights.safetensors").write_bytes(b"BASE-WEIGHTS-V1")
    digest = base_directory_hash(base)
    assert digest

    adapter = tmp_path / "adapters" / "evie-v1"
    adapter.mkdir(parents=True)
    (adapter / "COMPLETE").write_text("completed")
    (adapter / "adapter").mkdir()
    (adapter / "adapter" / "adapter_model.safetensors").write_bytes(b"LORA-ADAPTER")
    (adapter / "manifest.json").write_text(
        json.dumps(
            {
                "base_sha256_before": digest,
                "base_sha256_after": digest,
                "real_weights": True,
            }
        )
    )
    active_root = tmp_path / "active"
    active_root.mkdir()
    (active_root / "active").write_text(str(adapter))

    monkeypatch.setenv("EV_TRAINING_LORA_OUTPUT_ROOT", str(tmp_path))
    from app.training import lora as lora_service

    result = lora_service.rollback(str(adapter))
    assert result["rolled_back"] is True
    assert result["active_removed"] is True
    assert result["base_byte_identical"] is True
    assert not (active_root / "active").exists()
    # The base itself is untouched, byte for byte.
    assert sha256_file(base / "weights.safetensors") == sha256_file(base / "weights.safetensors")


def test_rollback_detects_base_change(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "weights.safetensors").write_bytes(b"BASE-V1")
    before = base_directory_hash(base)
    (base / "weights.safetensors").write_bytes(b"BASE-V2")
    after = base_directory_hash(base)
    assert before != after

    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "COMPLETE").write_text("completed")
    (adapter / "manifest.json").write_text(
        json.dumps({"base_sha256_before": before, "base_sha256_after": after})
    )
    result = rollback_adapter(adapter, active_root=tmp_path / "active")
    assert result["base_byte_identical"] is False


def test_held_out_split_is_deterministic_and_covers_40_of_200() -> None:
    records = [
        {"source": f"response_log:{i}", "hash": f"h{i:04d}", "instruction": f"p{i}"}
        for i in range(200)
    ]
    first = held_out_split(records, eval_fraction=0.2, seed=42)
    second = held_out_split(list(reversed(records)), eval_fraction=0.2, seed=42)
    assert first == second
    assert len(first[0]) + len(first[1]) == 200
    assert len(first[1]) == 40


def test_eval_harness_reports_win_rate_tool_and_hud() -> None:
    prompts = ["One", "Two"]
    references = ["Direct answer.", '{"schema_version":"ev.hud.card.v1","generated_at":"2026-01-01","title":"EV"}']

    def base_predict(prompt: str) -> str:
        return "Maybe " + prompt

    def adapter_predict(prompt: str) -> str:
        return references[prompts.index(prompt)]

    result = evaluate_models(
        prompts,
        references,
        base_predict=base_predict,
        adapter_predict=adapter_predict,
        profile={"prefer_direct": True, "word_count_targets": {"casual": 5}},
    )
    assert result["win_rate"] >= 0.5
    assert "deterministic judge" in result["method"]

    tool = tool_call_validity(
        ['{"name":"search_web","arguments":{"q":"ev"}}', "no tool"]
    )
    assert tool["tool_calls"] == 1
    assert tool["validity"] == 1.0

    hud = hud_conformance(references)
    assert hud["hud_blocks"] == 1
    assert hud["conformance"] == 1.0

    overfit = overfit_report([2.0, 1.5, 1.0], [2.1, 2.0, 2.2])
    assert overfit["overfit_detected"] is True
    clean = overfit_report([2.0, 1.5, 1.0], [2.1, 1.9, 1.1])
    assert clean["overfit_detected"] is False


async def test_corpus_export_formats_never_leak_secrets_or_excluded_rows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await client.post("/v1/training/consent", json={"track": "training_corpus"})
    db_session.add(
        ResponseLog(
            request_text="My key is sk-1234567890abcdefghijklmnop for the vault.",
            reply_text="Understood, key stored safely.",
            mode="casual",
            strategy={"tool_calls": [{"name": "store_secret", "arguments": {"vault": "key"}}]},
            provenance_ids=[],
            context_tokens=10,
            was_correction=True,
        )
    )
    db_session.add(
        FilterLedger(
            request_id="req-1",
            stage="output",
            action="soften",
            name="grounding",
            detail={},
            draft="That definitely happened.",
            final_text="That may have happened.",
            iterations=1,
        )
    )
    await db_session.commit()
    await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": "Never send this secret plan to a model.",
            "privacy_level": "never_send_to_model",
        },
    )
    build = await client.post("/v1/training/corpus/build")
    assert build.status_code == 201, build.text
    version = build.json()["snapshot"]["version"]

    for fmt in ("canonical", "sft", "preference", "tool"):
        resp = await client.get(f"/v1/training/corpus/{version}/dataset?format={fmt}")
        assert resp.status_code == 200, resp.text
        body = " ".join(line for line in resp.text.splitlines())
        assert "sk-1234567890abcdefghijklmnop" not in body
        assert "Never send this secret plan" not in body

    tool = await client.get(f"/v1/training/corpus/{version}/dataset?format=tool")
    tool_records = [json.loads(line) for line in tool.text.splitlines() if line.strip()]
    assert any(
        record.get("tool_calls") and record["tool_calls"][0]["name"] == "store_secret"
        for record in tool_records
    )

    sft = await client.get(f"/v1/training/corpus/{version}/dataset?format=sft")
    sft_records = [json.loads(line) for line in sft.text.splitlines() if line.strip()]
    assert any(
        (record.get("signals") or {}).get("tool_teaching") is True
        and "store_secret" in record.get("output", "")
        for record in sft_records
    )

    pref = await client.get(f"/v1/training/corpus/{version}/dataset?format=preference")
    pref_records = [json.loads(line) for line in pref.text.splitlines() if line.strip()]
    assert any(
        record["rejected"] == "That definitely happened."
        and record["chosen"] == "That may have happened."
        for record in pref_records
    )
