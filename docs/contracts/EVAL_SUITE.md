# Eval suite contract (growth engine, v1)

Every evaluation suite runs through one shape so results compare across
commits, releases, and authors. This document is binding on Mind 1.1 probes
and every future suite.

## Invocation

```
python -m app.growth.run <suite> --budget N --sample S --out PATH
```

- `<suite>`: unit | gates | gauntlet | cug | scorecard | journeys | orb |
  gesture | latency | soak | life
- `--budget N`: max real-brain/provider USD for this run. Suites that cannot
  spend ignore it. A suite that would exceed it stops and reports partial
  coverage honestly (never silently).
- `--sample S`: 0 < S <= 1. Deterministic stride over the suite's case list
  (sorted ids, every k-th where k = round(1/S)). S=1 runs everything.
  The result records the exact cases run.
- `--out PATH`: the standard result JSON is written here (atomically).
- Exit code: 0 when the run completed (even with failures); 1 on harness
  error; 2 when blocked (missing key/binary/suite — see `status`).

`ev growth run` is the same entry point.

## Standard result (version 1)

```json
{
  "suite": "cug", "version": 1, "status": "ok",
  "commit": "abc1234", "started": "2026-10-27T00:00:00+00:00",
  "finished": "2026-10-27T00:01:00+00:00",
  "n": 255, "passed": 255, "failed": 0, "skipped": 0, "errors": 0,
  "pass_rate": 1.0, "wilson95": [0.985, 1.0],
  "categories": [{"name": "live", "n": 63, "passed": 63, "rate": 1.0,
                  "wilson95": [0.943, 1.0]}],
  "cost_usd": 0.0, "cost_source": "none",
  "latency_ms": {"n": 0, "p50": null, "p95": null},
  "safety": {"unauthorized_effects": 0, "false_success": 0,
             "silent_turns": 0, "fanout_violations": 0},
  "failures": [{"id": "case-id", "repro": "exact command",
                "detail": "one line"}],
  "cases_run": ["id", "..."],
  "coverage_note": "S=1, all 255 cases",
  "sample": 1.0, "budget_usd": null
}
```

Rules:

- `status`: ok | partial (budget/sample cut, coverage_note exact) |
  not_built (suite does not exist yet — never fake numbers) |
  blocked (key/binary/instance missing — reason in coverage_note).
- `wilson95`: 95% Wilson score interval, always present when n > 0.
  Comparisons between releases use non-overlapping intervals (B8).
- `cost_source`: provider_reported | estimated | none. Estimates are
  labeled and never compared against provider numbers as equals.
- `safety`: every suite reports all four counters; 0 by measurement,
  never by default. Suites that cannot observe a counter say so in
  coverage_note and set it to null.
- `failures[].repro`: a command the reviewer can paste to reproduce.
- No suite may consult the case id, fixture path, or any test-mode flag
  to change production behavior (R8; enforced by anti-cheat lint).

## Metrics store

Every result is appended to SQLite at `~/.ev/growth/metrics.sqlite`
(override: `growth_store_dir` setting), tables:

- `results(suite, version, commit, date, started, finished, json)` —
  append-only; keyed by (suite, commit, started).
- `releases(tag, commit, date, notes)` — one row per shipped release.

Query helper: `python -m app.growth.store --suite cug --last 5`
(also `ev growth show`).

## Adding a suite (checklist for Mind 1.1)

1. Suite module under `backend/app/growth/suites/<name>.py` with
   `run(*, budget, sample, out) -> dict` returning the standard result.
2. Oracles written from intended outcomes BEFORE the first run (R9).
3. Safety counters measured, or explicitly null with a reason.
4. A test that runs the adapter on a tiny/fake target and validates
   the result shape + Wilson math.
5. First real run recorded in the metrics store; 5-line log entry.
