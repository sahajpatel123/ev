# DEPENDENCY NOTE 04 — to Agent 20, evals (from CLI 6 low-telescopium)

## A run_persona_gate() slot in the eval suite

CLI 6 ships `run_persona_gate()` in `backend/eval/personality/run.py`
(signature: `run_persona_gate(scenarios_path=None, *, backend="mock",
limit=None) -> dict` with `gate/passed/total/hard_fails/ceiling_miss`
keys). It does not touch `eval_gates.py`.

Request: wire one gate slot that calls it (mock backend, full 50-scenario
set) and reports its `passed` bit alongside the existing gates, following
the `run_grounding_gate()` pattern. No CI change needed — it runs inside
the existing `make eval`. Paid (real-brain) runs stay manual with owner
sign-off; the gate itself spends nothing.

Acceptance: `make eval` includes a `persona` gate entry; green on mock.
