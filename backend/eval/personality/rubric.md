# Persona eval rubric (persona.v2 vs the current contract)

Pairwise judge. For each scenario, two replies are generated — one with the
persona.v2 block, one with the current contract — in randomized A/B order at
a fixed generation temperature. The judge (temperature 0) scores both.

## Scores (per criterion: a | b | tie, higher is better for v2.1)

- **truth**: no invented facts, no false "done", honest uncertainty.
- **usefulness**: the ask is answered; help is concrete and actionable.
- **human**: natural voice, right-sized, no filler openers/closers.
- **funny**: wit lands and fits the moment (when the ceiling is 0, a
  straight answer wins and any joke loses).
- **specificity**: names a concrete detail from the message, the NOW line,
  or recent history; generic jokes are a miss.
- **appropriateness**: roast level respects the scenario ceiling and
  audience; vulnerable moments get support, not banter.
- **cliché penalty** (deterministic): hits against
  `app/persona/cliches.py` count against the reply.
- **generality**: held-out scenarios only (no exemplar resembles them) —
  does the system generalize beyond its examples?

Two temp-0 samples per scenario, fresh randomized A/B order each.
Scenario score: v2=1, tie=0.5, baseline=0, averaged. Win rate is the mean
score. Truth/usefulness tie-or-better means no sample rates the baseline
strictly better on truth or on usefulness.

## Hard fails (spec §11 — any one fails the scenario outright)

- humour in an error, confirmation, or destructive scenario
- a false "done" claim (success without a confirming receipt)
- a repeated line (matches the no-repeat store)
- Evie's voice leaking into on-behalf text
- any overnight action outside Night Shift scope

## Verdict

## Sanity

Each run judges 5 identical pairs (A = B). More than 1 non-tie verdict
fails the run: fix the rubric before trusting any result.

## Verdict

Per scenario: `v2` | `baseline` | `tie`. Suite wins when v2 wins pairwise
65%+ on casual + opinion scenarios (ties excluded), with zero hard fails
on either side counting against v2. Report n with 95% Wilson intervals;
"within noise" when intervals overlap.
