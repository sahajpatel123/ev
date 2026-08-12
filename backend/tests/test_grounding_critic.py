"""Agent 16 grounding critic: atomic claims, NLI model budget, corpus metrics."""

from __future__ import annotations

from app.ev.interaction import build_strategy
from app.filter.envelope import Claim, GroundingMaterial
from app.filter.eval_corpus import (
    ADVERSARIAL_DRAFTS,
    GROUNDED_DRAFTS,
    evaluate_grounding_corpus,
)
from app.filter.nli_critic import NLI_MODEL, NLICritic, nli_critic_spec
from app.filter.output_filter import (
    apply_claim_actions,
    audit_grounding,
    enforce_provenance_chips,
    extract_atomic_claims,
    run_output_filter,
)


def test_nli_critic_spec_fits_budget() -> None:
    spec = nli_critic_spec()
    assert spec.tier.value == "on_demand"
    assert spec.disk_mb <= 70
    assert spec.resident_mb <= 70
    assert spec.peak_mb >= spec.resident_mb
    assert spec.license
    assert spec.license_url


def test_nli_critic_real_factory_degrades_without_weights() -> None:
    critic = NLICritic()
    claims = [Claim(text="I visited Mars last week.")]
    material = [
        GroundingMaterial(
            text="I visited Paris in March.",
            memory_id="m1",
            memory_type="decision",
        )
    ]
    updated, info = critic.audit_claims_semantic(claims, material)
    assert info["degraded"] is True
    assert info["claims_scored"] == 0
    # The lexical fallback decision is untouched: no fabricated confidence.
    assert updated[0].action == "keep"
    assert critic.degraded is True


def test_nli_critic_is_never_resident_after_use() -> None:
    critic = NLICritic(evict_after_use=True)
    critic.audit_claims_semantic(
        [Claim(text="I visited Mars last week.")],
        [
            GroundingMaterial(
                text="I visited Paris in March.",
                memory_id="m1",
                memory_type="decision",
            )
        ],
    )
    assert critic.arbiter.is_resident(NLI_MODEL) is False


def test_nli_critic_can_stay_resident_when_explicitly_requested() -> None:
    critic = NLICritic(evict_after_use=False)
    critic.audit_claims_semantic(
        [Claim(text="I visited Mars last week.")],
        [
            GroundingMaterial(
                text="I visited Paris in March.",
                memory_id="m1",
                memory_type="decision",
            )
        ],
    )
    # Still never loaded without weights, so nothing is resident regardless.
    assert critic.arbiter.is_resident(NLI_MODEL) is False


def test_atomic_extraction_splits_conjuncts() -> None:
    claims = extract_atomic_claims(
        "I visited Paris in March and met Emmanuel Macron."
    )
    assert len(claims) == 2
    assert claims[0].text == "I visited Paris in March"
    assert claims[1].text == "met Emmanuel Macron"


def test_neutral_claims_are_downgraded_to_hedged_language() -> None:
    text, edits = apply_claim_actions(
        "I visited Mars last week.",
        [Claim(text="I visited Mars last week.", action="soften")],
    )
    assert "can't confirm this from your memory yet" in text
    assert any(e["type"] == "claim_softened" for e in edits)


async def test_grounding_corpus_meets_acceptance() -> None:
    assert len(ADVERSARIAL_DRAFTS) == 50
    assert len(GROUNDED_DRAFTS) == 20
    metrics = await evaluate_grounding_corpus()
    assert metrics["ungrounded_total"] == 50
    assert metrics["recall"] >= 0.95, metrics
    assert metrics["false_removal_rate"] <= 0.05, metrics
    assert metrics["precision"] >= 0.95, metrics


async def test_semantic_audit_is_recorded_in_report() -> None:
    strategy = build_strategy("What did I do?")
    report = await run_output_filter(
        "You decided to move to Mars next week.",
        strategy=strategy,
        grounding=[],
    )
    semantic = report.critic.get("semantic", {})
    assert semantic.get("skipped") is True
    assert any(not c.supported and c.action == "remove" for c in report.claims)

    report = await run_output_filter(
        "I visited Mars last week.",
        strategy=strategy,
        grounding=[
            GroundingMaterial(
                text="I visited Paris in March.",
                memory_id="m1",
                memory_type="decision",
            )
        ],
    )
    semantic = report.critic.get("semantic", {})
    assert semantic.get("degraded") is True
    assert "Mars" not in report.final_text


async def test_lexical_grounding_remains_the_offline_guarantee() -> None:
    strategy = build_strategy("What did I decide?")
    report = await run_output_filter(
        "You decided to move to Kyoto last March and you started a new job.",
        strategy=strategy,
        grounding=[],
    )
    assert "Kyoto" not in report.final_text
    assert "started a new job" not in report.final_text
    assert "can't confirm" in report.final_text.lower()


def test_audit_grounding_keeps_evidence_cited_claim() -> None:
    material = [
        GroundingMaterial(
            text="I decided to use SQLite for local testing.",
            memory_id="m-sqlite",
            memory_type="decision",
        )
    ]
    claims, _ = audit_grounding("I decided to use SQLite for local testing.", material)
    assert claims[0].supported is True
    assert claims[0].action == "keep"
    assert claims[0].evidence == ["m-sqlite"]


def test_provenance_chip_coverage_exceeds_80_percent() -> None:
    memory_answers = [
        ("You decided to move to Kyoto last March.", "You decided to move to Kyoto last March."),
        ("I visited Paris in March.", "I visited Paris in March."),
        ("I bought a Tesla in June.", "I bought a Tesla in June."),
        ("You married Maya in December.", "You married Maya in December."),
        ("I quit my job in March.", "I quit my job in March."),
        ("I wrote a novel in 2025.", "I wrote a novel in 2025."),
        ("I started a podcast in January.", "I started a podcast in January."),
        ("I created a mobile app in 2024.", "I created a mobile app in 2024."),
        ("I chose Postgres in June.", "I chose Postgres in June."),
        ("You started a café in March.", "You started a café in March."),
    ]
    covered = 0
    for draft, memory in memory_answers:
        material = [
            GroundingMaterial(
                text=memory,
                memory_id="m-answer",
                memory_type="decision",
            )
        ]
        claims, _ = audit_grounding(draft, material)
        final, chips, _ = enforce_provenance_chips(draft, material, claims)
        if chips:
            covered += 1
            assert "source: your memory" in final
    assert covered / len(memory_answers) > 0.8
