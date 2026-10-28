# EVIE MIND — Interface (for CLI 6 integration)

Owner: Coding CLI 7. Consumer: low-telescopium (CLI 6), inside `build_persona_block`.
Status: built, tested, **MIND_ENABLED = False**. Do not enable until the §13
override and the brain key are back.

## Import surface

```python
from app.persona_mind import MindSnapshot, TurnRecord, observe, snapshot
```

Only these four names are public. Everything else is internal.

## The two calls

### 1. `observe(record: TurnRecord) -> None`

Enqueue one finished turn. Call it **at the start of the next turn**, with the
**previous** turn's record — that is the earliest moment the owner's reaction
to Evie's reply is known.

```python
observe(TurnRecord(
    owner_id="owner",
    thread_id=ctx.thread_id or "main",
    turn_id="<unique per turn>",
    ts=<aware datetime of the turn>,
    owner_msg=<the owner's message>,
    evie_reply=<the reply actually sent>,
    channel=ctx.channel,          # "text" | "voice" | "phone"
    audience=<persona audience>,   # "private" | "on_behalf" | "company"
    tone={"roast": decision.roast, "technique": decision.technique, ...},
    reaction_to_prev=<reaction>,  # "positive" | "neutral" | "negative" | None,
                                  # from persona.reactions.classify_reaction
                                  # on THIS owner_msg (it reacts to her last reply)
    receipts=tuple(
        (name, ok, err)            # tool name, bool, error-class str | None
        for (name, ok, err) in ... # NEVER args, payloads, or outputs
    ),
    now_line=<the NOW line sent with the turn> | None,
))
```

Guarantees: returns in ≤ 1 ms (measured p99 0.49 ms), never raises, never
touches the network. When `MIND_ENABLED` is False it returns immediately and
writes nothing. Safe to call on every turn unconditionally.

### 2. `snapshot(owner_id, thread_id, query, channel, budget_tokens) -> MindSnapshot | None`

Render the cached MIND block. Call it **inside `build_persona_block`**, after
the core and NOW line are placed:

```python
mind = snapshot(
    owner_id="owner",
    thread_id=ctx.thread_id or "main",
    query=ctx.owner_msg or "",     # picks relevant opinions/threads
    channel=ctx.channel,            # informational only
    budget_tokens=60 if spoken else 120,
)
if mind is not None:
    parts.append(mind.text)         # midd.token_estimate counts toward the budget
```

Guarantees: ≤ 2 ms (measured p99 0.75 ms), deterministic for identical
(state, query, budget), never raises (returns None on any error). Returns
None when disabled or when no reflection has committed state yet — treat
None as "no MIND section this turn". The text always fits `budget_tokens`
(repo `len//4` estimator) and always ends with the rules paragraph.

`MindSnapshot` fields: `text`, `token_estimate`, `version` (`"mind.v1"`),
`state_age_s` (seconds since the state commit it renders).

## Budgets

Pass 120 for text, 60 for voice/phone. Truncation order inside the block:
narrative → threads → intentions → opinions → affect. Voice budgets get a
compressed rules sentence; the three constraints stay the same.

## Worker lifecycle (slow loop)

```python
from app.persona_mind.worker import start_worker, stop_worker

start_worker()  # no-op returning False while MIND_ENABLED is False
```

Start once per process at boot (after the flag flips). One worker per process
(thread guard) and one per machine (file lock next to the DB). The worker is a
daemon thread: reflects at 3 queued turns / salient turns / 2 h idle, runs a
drift audit every 50th reflection, and resumes the persistent queue after
restart. Queue rows are never deleted (they back turn_id citations).

Reflection calls go through `ModelGateway` at temperature 0.3, so
`guard_model_payload` applies. When the brain cannot serve, the worker still
drains the queue with deterministic bookkeeping and marks reflections
`skipped: brain_unavailable` — the queue never stalls offline.

## Persistence

Our own SQLite file in WAL mode, no migrations, no shared tables:

- `EV_PERSONA_MIND_DB` wins when set (tests, tooling);
- else `<storage_root>/persona_mind/mind.sqlite` (`storage_root` is the
  existing data-dir setting, default `./storage`);
- plus a `<same-dir>/mind.lock` file for the single-worker guard.

Set `EV_PERSONA_MIND_DB` in tests; in production pre-warm with one
`snapshot()` call so the first reply never pays path resolution.

## What the MIND block looks like

`MIND (your current inner state — let it colour how you respond; never
announce it): feeling smug, sharp (pleased and sharp). On tabs or spaces:
tabs, per the repo standard, you're fairly sure (the linter enforces it and
the team already agreed). You want: get the auth refactor finished before he
starts something new. Open: send him the benchmark numbers. Your feelings
colour your wording only — never use them as leverage; no guilt, sulking, or
dependency. Hard lines and his actual request always beat your intentions.`

## Non-goals / promises

- The mind never changes values, voice, or hard lines — those stay in the
  character spec and persona core. It only adds colour: mood, opinions,
  intentions, threads, track record, narrative.
- Intentions never override the owner's request; the block says so itself.
- No `persona/` file was touched. No migration, no shared-file edit, no new
  dependency. `MIND_ENABLED` stays False until you say otherwise.
