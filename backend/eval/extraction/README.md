# Extraction evaluation corpora

Every `*.json` file in this directory is a labeled extraction corpus. The
harness (`tests/test_extraction_quality.py`) scores all of them together and
reports precision/recall per memory type in **three rows**: rule-only,
rule+LLM enrichment (perfect, triaged oracle), and the delta — so the owner
can see exactly what enrichment spend buys.

## Seed corpus

`seed_captures.json` is a CI-safe synthetic corpus (49 captures) covering the
rule-based extractor's supported patterns. It keeps CI meaningful on laptops
with no model and no personal data.

## Real 100-capture set (needed for acceptance)

Drop a file such as `real_captures.json` here with the same schema:

```json
{
  "name": "real-captures-v1",
  "synthetic": false,
  "license": "owner-consented",
  "captures": [
    {
      "id": "r-001",
      "text": "The exact capture text the user said/typed.",
      "expected_memory_types": ["decision"],
      "expected_entities": [{"name": "Postgres", "entity_type": "project"}],
      "expected_temporal": false
    }
  ]
}
```

Field rules:

- `expected_memory_types`: one or more of `decision`, `preference`, `goal`,
  `fact`, `observation` (episodic is not scored).
- `expected_entities`: optional; `entity_type` is one of `person`, `place`,
  `project`, `topic`, `other`.
- `expected_temporal`: optional boolean; set `true` when the capture contains
  a temporal expression that must resolve to a real timestamp.

The harness prints a per-file table plus a combined table. Acceptance gates:
overall precision ≥ 0.85, overall recall ≥ 0.75, entity recall ≥ 0.75, and all
marked temporal expressions resolved.

## Enrichment economics

`measure_enrichment_economics` (in `app/memory/llm_extractor.py`) reports
calls-per-100-captures and an estimated monthly cost from triage + batching.
Assumptions are configurable: `EV_LLM_EXTRACTION_BATCH_SIZE`,
`EV_LLM_EXTRACTION_TOKENS_PER_CALL`, `EV_LLM_EXTRACTION_COST_PER_M_TOKEN`, and
`EV_LLM_EXTRACTION_MONTHLY_CAPTURES`.
