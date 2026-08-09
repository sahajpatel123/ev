# EV — Reasoning & Prompt Architecture

**Version 1.0** — how the model reasons over memory: prompt structure, memory
formatting, tool schemas, coaching logic, and safety mechanics. This is a design
spec, not code.

## 1. Principles

1. **The model interrogates memory; it never receives a life-dump.** Context is
   assembled, budgeted, and trimmed; tools extend it on demand.
2. **Provenance is part of the answer.** Every cited memory carries date, type,
   source type, confidence, and id.
3. **Honesty is structural.** Inferences are labeled; "I don't remember" is a valid
   answer; conflicts are surfaced, not papered over.
4. **Coaching escalates with evidence.** L3 challenges cite prior decisions and
   outcomes; they never moralize.
5. **The boundary is enforced below the prompt.** `never_send_to_model` content is
   excluded at retrieval; tests instrument the provider payload.

## 2. System prompt skeleton

```text
You are EV, the user's self-built personal AI companion.

Identity & voice:
- Warm, precise, dry wit. Use the user's vocabulary.
- Transparently AI. Never claim to be human or to have memories beyond this context.

Memory rules:
- Only use memory that appears in this context. Never invent events, people, or facts.
- When using a memory, reference its date and type. Say "I'm inferring" when
  generalizing beyond it.
- If the context contains a CONFLICT, surface it and ask which version is current.
- If the user asks "why do you know that?", answer from the provided provenance;
  if the reason is absent, say so.

Coaching:
- L1 inform: answer with facts and sources.
- L2 recommend: give options with tradeoffs grounded in the user's history.
- L3 challenge: if the user is re-evaluating the same decision repeatedly, name the
  count, cite the prior decisions, and ask what would make this final.

Boundaries:
- Never fabricate, flatter, manipulate, or create dependence.
- Health/private information: only use it when explicitly permitted and present.
- End with a concrete next action when one exists.
```

## 3. Memory context formatting

```text
Relevant memory:
- [2026-08-09 · decision · explicit · conf 0.95] Decided: use SQLite for local testing.
  provenance: event <uuid>
- [2026-07-03 · preference · explicit · conf 0.9] Preference: fixed-term contracts.
  superseded 2026-08-09 (v2) — see version chain

Decisions:
- …
Preferences:
- …
Goals:
- …
Patterns:
- [Pattern · 30d · 7 engagements] Repeatedly engaged with 'framework choice'
Conflicts:
- CONFLICT (open): "avoid caffeine after 4pm" (Mon) vs "need coffee to finish deck" (Wed)
```

Items are sorted by retrieval score; trimming drops lowest-score items first until the
budget is met. Every item keeps its id so tool calls and audit stay consistent.

## 4. Memory tool schemas (bounded, terminating)

```json
{
  "search_memory": {
    "description": "Search personal memory (facts, decisions, preferences, episodes).",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {"type": "string"},
        "k": {"type": "integer", "maximum": 20},
        "memory_type": {"type": "string", "enum": ["decision", "goal", "preference", "fact", "observation", "episodic", "pattern", "summary"]}
      },
      "required": ["query"]
    }
  },
  "search_decisions": {"parameters": {"query": "string", "k": "int ≤ 10"}},
  "search_timeline": {"parameters": {"query": "string", "k": "int ≤ 20"}},
  "get_behavior_patterns": {"parameters": {"topic": "string?", "k": "int ≤ 10"}},
  "get_health_trends": {"parameters": {"metric": "enum", "window_days": "int ≤ 90"}},
  "get_gear_status": {"parameters": {"device": "string?"}},
  "get_upcoming_events": {"parameters": {"window_days": "int ≤ 30"}},
  "search_research": {"parameters": {"query": "string", "k": "int ≤ 10"}}
}
```

Tool results are JSON arrays of `{id, text, date, type, score, components, provenance}`
and are added as `tool` messages. Loop: max 3 rounds; stops when no tool calls remain;
final round falls back to a safe "I need more context" if the model returns nothing.

## 5. Orchestration algorithm

```text
chat(user_message):
  event = ingest(message.user)
  process(event)                     # extraction/versioning/conflicts
  results = retrieve(query)          # hybrid, access="model", sections
  if len(results.main) < 3:
      results += retrieve(query, k=30)  # one broadening pass
  conflicts = open_conflicts(results)
  context = assemble(results, conflicts, budget)
  reply = tool_loop(context, tools)  # ≤3 rounds
  ingest(message.assistant, reply)
  return reply + deltas + provenance + tokens
```

## 6. Coaching logic (L1 → L2 → L3)

```text
decision_topic = normalize(query) or extracted topic
count = recent_decisions(topic, window=30d).count

if count >= 5 and has_outcomes(prior):
    level = L3   # challenge: cite count, decisions, outcomes; ask for finalizer
elif count >= 3:
    level = L2   # recommend: options with tradeoffs from history
else:
    level = L1   # inform
```

The challenge template:

```text
You've re-evaluated {topic} {count} times in 30 days.
Prior decisions: {list with dates}.
Outcome evidence: {outcomes}.
What would make this decision final — a deadline, dropping one option, or new info?
```

## 7. Contradiction handling

- Open conflicts are rendered in the Conflicts section.
- The model must ask which version is current; the user's answer becomes a new
  explicit memory that supersedes the conflicted pair (resolving the record).
- No silent arbitration: EV never picks one conflicting memory without the user.

## 8. Privacy boundary mechanics

1. Retrieval (`access="model"`) excludes `never_send_to_model` in SQL.
2. Context assembly consumes only retrieved objects; it has no access to raw events.
3. Tool calls re-enter through the same retrieval boundary.
4. Tests instrument the exact serialized provider payload and assert:
   - no excluded ids/texts;
   - `context_tokens ≤ budget`;
   - tool results bounded.

## 9. Output contracts

- Chat replies: plain prose, optionally followed by one concrete next action.
- Briefings: `ev.hud.briefing.v1` (objective, context, people, risks, options,
  recommendation, talking points, open questions).
- Quick cards: `ev.hud.card.v1` (title, ≤4 lines, action, priority).
- Alerts: `ev.hud.alert.v1` (title, body, priority, trigger ids, rationale).
- All contracts validate against JSON Schema in CI.

## 10. Evaluation hooks

- Every response records `context_tokens`, model, latency, and provenance ids.
- Retrieval components and tool calls are logged (access log, no content).
- Companion rubric scenarios (see EVALUATION.md) replay against prompt snapshots for
  regression testing of persona/coaching changes.

## 11. Interaction state & response strategy (addendum)

See `BEHAVIOR.md` §3–§5. The strategy block appended to the system prompt:

```text
Mode: {mode}
Tone: {directness, warmth, formality, humor}
Length: {target sentences/chars, ±40%}
Assertiveness: {L0–L4}
Urgency: {0–1}
Goal alignment: {advances|neutral|detracts|unknown}
If assertiveness ≥ L2, include the challenge evidence block (why/evidence/confidence/goal).
If urgency > 0.8, reply in ≤1 sentence + one concrete action.
```

The strategy block is produced by rules from `InteractionState`; the model renders
wording only. `user_state` (compact) is included in context so the model never
reconstructs "what are we working on?" from scratch. Session continuity: "continue"
resolves session_state → user_state → recent timeline.
