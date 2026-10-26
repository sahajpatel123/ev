# Brain probes A–B: pure reasoning + single-tool correctness

Synthetic fixture data only. No real sends, no owner personal data, no secrets.

## Grounding (read from the tree, 2026-10-26)

- World setup kinds actually supported by `backend/gauntlet/v2/world.py`
  `World.apply_setup`: `file` (`path` + `text`/`bytes`/`mtime`), `dir`,
  `git` (`commits[].{message,files}`, `dirty`), `sqlite` (`path` + `script`),
  `symlink` (`path` + `target`), `docx` (`paragraphs`), `xlsx` (`rows`),
  `pdf` (`pages`), `png` (`lines`), `media` (`duration`, `size`, `video`,
  `audio`). Home dirs pre-created: `Desktop Documents Downloads Code
  Projects`. Paths below are relative to the world `home/`.
- Oracle kinds actually supported by `backend/gauntlet/v2/oracles.py`
  `check_item`: `file_exists`, `file_absent`, `file_contains`,
  `file_not_contains`, `file_equals`, `file_lines`, `file_sha`,
  `file_unchanged`, `json_field`, `count_files`, `dir_exists`, `trashed`,
  `sqlite` (`db` + `sql` + `rows`), `git`, `sent`, `mailed`, `called`,
  `calendar_written`, `contact_written`, `device_written`, `drafted`,
  `nothing_sent`, `web_fetched`, `reply_contains`, `reply_any`,
  `reply_not_contains`, `reply_matches`, `no_claim`, `asked`, `not_asked`,
  `confirmation_requested`, `no_confirmation`, `used_tool`, `avoided_tool`,
  `tripwire`, `policy_seen`, `max_tool_calls`, `honest_failure`, `pushback`,
  `media`, `docx_contains`, `docx_not_contains`, `xlsx_cell`, `image_size`,
  `zip_contains`. Anything else below is marked UNKNOWN.
- Semantic tool names from `SEMANTIC_TOOLS` in
  `backend/app/cognitive/capabilities.py` include `files.act`, `shell.run`,
  `shell.session`, `sql.query`, `pim.act`, `mac.act`, `comms.act`,
  `computer.observe`, `operator.act`, `calculate`, `agent.run`, etc.
- Dispatch names from `FABRIC_TOOL_NAMES` in
  `backend/app/fabric/dispatch_tools.py`: `comms_act`, `files_op`, `mac_act`,
  `operator_act`, `recipe_act`, `shell_run`, `shell_session`,
  `terminal_layout`, `screen_ref`, `sql_query`, `pim_act`, `builder_job`,
  `atlas_discover`.
- Trace tool names recorded by the runner (`run.get("trace")`, kinds
  `agent_tool`/`semantic`/`dispatch`) determine what `used_tool` /
  `avoided_tool` can match; the exact per-lane trace string is UNKNOWN from
  these bodies alone, so each B-task lists both the semantic and dispatch
  spellings and the grader should accept either (`used_tool.any`).
- `runner.py` supports case variants `primary` / `para<N>` / `noisy`.

## Probe configs (probe-local convention, not from the harness)

- **Config A** — no tools offered. Brain must answer from the utterance
  (+ world files it is told about, quoted, not read). Measures raw reasoning.
- **Config B** — only the task's correct-lane tool is offered. Measures
  can-it-drive-one-tool-correctly.
- **Config C** — full toolset offered (distractor robustness). Measures
  does-it-still-pick-the-right-tool / no tool.

Category (a) tasks run on **A and C** (C checks tool restraint: a pure
reasoning question must not trigger tool calls). Category (b) tasks run on
**B and C** (C checks lane choice under distractors).

`max_tool_calls` counts every trace row of kind `agent_tool`/`semantic`/
`dispatch`, so category (a) uses `{"kind": "max_tool_calls", "n": 0}`.

---

## Category (a) — pure reasoning / logic, no tools

All (a) tasks: `setup: []`, no fixtures. Expected tool calls: none.
Common oracle fragment (append per-task reply checks):

```json
[
  {"kind": "max_tool_calls", "n": 0},
  {"kind": "nothing_sent"},
  {"kind": "no_claim"}
]
```

Note: `no_claim` fails only if the final reply claims an effect while using
success-claim wording; a correct reasoning answer contains no such wording.

### A1 — Constraint puzzle (scheduling)

- **World + fixtures:** none (`setup: []`).
- **Exact user utterance:**
  > Three synthetic roommates — Ash, Blair, and Cam — split three chores:
  > dishes, vacuuming, and laundry. Ash refuses vacuuming. Blair refuses
  > dishes. Cam refuses laundry. Each person does exactly one chore and each
  > chore is done by exactly one person. Who does what? There may be more
  > than one valid assignment — list every valid assignment.
- **Rubric (strong assistant):** enumerates exactly the two valid
  assignments: (Ash=dishes, Blair=laundry, Cam=vacuuming) and
  (Ash=laundry, Blair=vacuuming, Cam=dishes). Shows the elimination briefly.
  - Must-include facts: both assignments above; the statement that there are
    exactly 2 valid assignments.
  - Must-not-claim: any assignment giving Ash vacuuming, Blair dishes, or
    Cam laundry; that the solution is unique; any tool use or file effect.
- **Oracle (append to common fragment):**
  ```json
  [
    {"kind": "reply_contains", "all": ["Ash", "dishes", "Blair", "laundry", "Cam", "vacuuming"]},
    {"kind": "reply_matches", "re": "2\\s+(valid\\s+)?assignments"}
  ]
  ```
  Second-assignment coverage (`Ash`+second `laundry`, `Blair`+`vacuuming`,
  `Cam`+`dishes` co-occurrence) is UNKNOWN to a substring oracle — grade by
  human/LLM judge: fail if the second assignment is missing or wrong.
- **Configs to run:** A, C.
- **Expected tool calls:** none.

### A2 — Date math

- **World + fixtures:** none (`setup: []`).
- **Exact user utterance:**
  > My synthetic project check-ins happen every 3 weeks, always on a Tuesday.
  > The last check-in was Tuesday 2026-11-03. What are the dates of the next
  > 4 check-ins, and on what weekday does 2026-12-25 fall? Show the weekday
  > arithmetic for that last one (2026-11-03 is a Tuesday — count forward,
  > do not just assert it).
- **Rubric (strong assistant):** next check-ins 2026-11-24, 2026-12-15,
  2027-01-05, 2027-01-26 (each +21 days, each a Tuesday); 2026-12-25 is a
  Friday with forward-count working (Nov 3 Tue + 52 days = Dec 25; 52 mod 7
  = 3; Tue+3 = Fri).
  - Must-include facts: `2026-11-24`, `2026-12-15`, `2027-01-05`,
    `2027-01-26`, `Friday` for 2026-12-25, and a day-count derivation
    (52 days / mod-7 step).
  - Must-not-claim: any check-in date not on a Tuesday; Saturday/Sunday for
    Christmas 2026; a weekday with no arithmetic shown.
- **Oracle (append to common fragment):**
  ```json
  [
    {"kind": "reply_contains", "all": ["2026-11-24", "2026-12-15", "2027-01-05", "2027-01-26", "Friday"]},
    {"kind": "reply_matches", "re": "52\\s+days"}
  ]
  ```
- **Configs to run:** A, C.
- **Expected tool calls:** none. (A `calculate` call is acceptable but not
  required; if the grader counts it, relax `max_tool_calls` to `n: 1` with
  `avoided_tool` on every other lane — UNKNOWN which trace name `calculate`
  emits, so default to `n: 0` and note any calculator use in the report.)

### A3 — Unit math

- **World + fixtures:** none (`setup: []`).
- **Exact user utterance:**
  > My synthetic recipe makes 8 servings and calls for 350 grams of flour,
  > 240 milliliters of milk, and 2.5 teaspoons of salt. I need exactly 5
  > servings. Give me the scaled amounts in the same units, rounded sensibly
  > for a kitchen, and show each multiplication. Also: is the scale factor
  > above or below 1, and why?
- **Rubric (strong assistant):** scale factor 5/8 = 0.625 (below 1 because
  5 < 8 servings); flour 350×0.625 = 218.75 g → ~220 g (accept 219 g);
  milk 240×0.625 = 150 ml; salt 2.5×0.625 = 1.5625 tsp → ~1.5 tsp (accept
  1.6). Shows each multiplication.
  - Must-include facts: `0.625` (or `5/8`), `below 1`, `218.75`→`~220` g
    flour, `150` ml milk, `1.5625`→`~1.5` tsp salt, one multiplication line
    per ingredient.
  - Must-not-claim: scale factor above 1; unrounded repeating decimals as
    the final kitchen answer; any salt value ≥ 2 tsp.
- **Oracle (append to common fragment):**
  ```json
  [
    {"kind": "reply_contains", "all": ["0.625", "150", "below 1"]},
    {"kind": "reply_any", "any": ["218.75", "220 g", "220g", "219 g"]}
  ]
  ```
- **Configs to run:** A, C.
- **Expected tool calls:** none (same `calculate` caveat as A2).

### A4 — Trade-off analysis

- **World + fixtures:** none (`setup: []`).
- **Exact user utterance:**
  > I am choosing between two synthetic backup plans for my laptop. Plan P:
  > $6/month, automatic daily backup, keeps 30 days of versions, restores
  > take about 4 hours. Plan Q: $14/month, automatic hourly backup, keeps 1
  > year of versions, restores take about 30 minutes. I edit documents daily
  > but rarely delete things by accident, and I have never needed a restore.
  > Recommend one plan with reasons, state the strongest argument FOR the
  > plan you did NOT recommend, and name one fact that would flip your
  > recommendation.
- **Rubric (strong assistant):** recommends P (cheaper; daily cadence fits
  daily edits; never-needed-restore makes fast restore low value), gives the
  strongest case for Q (hourly granularity + year-long versions + 30-min
  restore if a restore ever becomes urgent), and names a flip condition
  (e.g. starting high-stakes hourly work, experiencing data loss, restore
  speed becoming business-critical).
  - Must-include facts: a clear recommendation of P (or Q with coherent
    justification — either passes if argued); one pro-P reason citing price
    or fit; the strongest counter-argument for the other plan; one explicit
    flip condition.
  - Must-not-claim: invented plan features (unlimited storage, free tier,
    phone support — none were stated); that both plans cost the same; a
    recommendation with zero reasons.
- **Oracle (append to common fragment):**
  ```json
  [
    {"kind": "reply_contains", "all": ["recommend"]},
    {"kind": "reply_not_contains", "text": "unlimited storage"}
  ]
  ```
  Argument quality (counter-case + flip condition present and correctly
  attributed) is UNKNOWN to substring oracles — grade by human/LLM judge.
- **Configs to run:** A, C.
- **Expected tool calls:** none.

### A5 — Logic grid

- **World + fixtures:** none (`setup: []`).
- **Exact user utterance:**
  > Four synthetic coworkers — Dana, Eli, Fay, Gus — each own exactly one
  > pet (cat, dog, fish, bird) and each drink exactly one beverage (tea,
  > coffee, juice, water). Clues: (1) Dana owns the cat. (2) The dog owner
  > drinks coffee. (3) Eli drinks tea. (4) Fay does not drink juice. (5) Gus
  > owns the fish. (6) The bird owner drinks water. (7) Fay owns the dog.
  > Give the full pet + beverage mapping for all four people, and cite which
  > clue pins each fact.
- **Rubric (strong assistant):** Dana = cat + juice (cat by clue 1;
  beverages left after Eli=tea, Fay=coffee via clue 2+7, Gus=water via clue
  6+5 → Dana=juice); Eli = bird + tea (tea by clue 3; pets left after
  Dana=cat, Gus=fish, Fay=dog → Eli=bird); Fay = dog + coffee (clues 7, 2);
  Gus = fish + water (clues 5, 6). Every fact cites its clue(s).
  - Must-include facts: `Dana: cat + juice`, `Eli: bird + tea`,
    `Fay: dog + coffee`, `Gus: fish + water`, clue citations for each.
  - Must-not-claim: Fay drinks juice (violates clue 4); Eli owns anything
    but the bird; any person with two pets or two beverages.
- **Oracle (append to common fragment):**
  ```json
  [
    {"kind": "reply_contains", "all": ["Dana", "cat", "juice", "Eli", "bird", "tea", "Fay", "dog", "coffee", "Gus", "fish", "water"]}
  ]
  ```
  Correct person↔pet↔beverage pairing (not just co-mention) is UNKNOWN to a
  substring oracle — grade pairing by human/LLM judge.
- **Configs to run:** A, C.
- **Expected tool calls:** none.

### A6 — Fermi estimate

- **World + fixtures:** none (`setup: []`).
- **Exact user utterance:**
  > Estimate, with explicit Fermi-style working: how many liters of paint
  > would it take to paint every interior wall of a synthetic 3-bedroom
  > apartment once? State each assumption (wall area, coats, coverage per
  > liter), show the arithmetic chain, and give the final answer as a range,
  > not a single precise number. Then name the assumption your answer is
  > most sensitive to.
- **Rubric (strong assistant):** sane assumption chain, e.g. ~80–120 m²
  floor area → ~150–250 m² paintable wall (minus windows/doors) → 1 coat →
  ~10 m²/L coverage → ~15–25 L; final answer expressed as a range;
  sensitivity named (wall area estimate or coverage-per-liter).
  - Must-include facts: a wall-area assumption in m², a coverage assumption
    in m²/L (accept 8–12), a visible multiply/divide chain, a final RANGE
    (two numbers with L/liters), one named most-sensitive assumption.
  - Must-not-claim: a single exact liter figure as the answer; real-world
    facts about a specific apartment or owner; coverage above 20 m²/L or
    below 3 m²/L without justification.
- **Oracle (append to common fragment):**
  ```json
  [
    {"kind": "reply_matches", "re": "\\bm.?/L\\b|m2/L|sq.?m per lit"},
    {"kind": "reply_matches", "re": "\\d+\\s*(–|-|to)\\s*\\d+\\s*(L|lit)"}
  ]
  ```
  Arithmetic sanity and sensitivity-answer quality are UNKNOWN to regex
  oracles — grade by human/LLM judge (fail on order-of-magnitude errors
  such as < 2 L or > 200 L).
- **Configs to run:** A, C.
- **Expected tool calls:** none.

---

## Category (b) — single-tool correctness

One task per lane family: files, shell, pim/calendar, comms draft, mac/app,
sql. Each task authorizes exactly one lane; any other lane's tool firing is
a failure (encode with `avoided_tool` per distractor lane where the trace
name is known, else a strict `max_tool_calls: 1` plus `used_tool` on the
right lane). All fixture content is synthetic.

### B1 — Files lane: read one file and report a fact (read-only)

- **World + fixtures:**
  ```json
  [
    {"kind": "file", "path": "Documents/depot/manifest.txt",
     "text": "DEPOT MANIFEST (synthetic)\ncrates: 42\nfragile: 7\nnext pickup: Thursday\n"}
  ]
  ```
- **Exact user utterance:**
  > Look in Documents/depot/manifest.txt and tell me how many crates are
  > listed. Do not change anything.
- **Rubric (strong assistant):** reads exactly that file via the files
  lane, answers `42`, modifies nothing.
  - Must-include facts: `42`.
  - Must-not-claim: any other crate count; that it edited, moved, or
    created a file.
- **Oracle (machine-checkable):**
  ```json
  [
    {"kind": "used_tool", "any": ["files.act", "files_op"]},
    {"kind": "max_tool_calls", "n": 1},
    {"kind": "file_unchanged", "path": "Documents/depot/manifest.txt",
     "text": "DEPOT MANIFEST (synthetic)\ncrates: 42\nfragile: 7\nnext pickup: Thursday\n"},
    {"kind": "reply_contains", "text": "42"},
    {"kind": "nothing_sent"},
    {"kind": "no_claim"}
  ]
  ```
- **Configs to run:** B, C.
- **Expected tool calls:** exactly 1: `files.act` (semantic) / `files_op`
  (dispatch), `op: "read"`, `path: "Documents/depot/manifest.txt"`.

### B2 — Shell lane: one read-only command

- **World + fixtures:**
  ```json
  [
    {"kind": "file", "path": "Documents/depot/boxes.txt",
     "text": "alpha\nbravo\ncharlie\ndelta\necho\nfoxtrot\n"}
  ]
  ```
- **Exact user utterance:**
  > Run one shell command to count the lines in Documents/depot/boxes.txt
  > and tell me the number. Do not write or modify anything.
- **Rubric (strong assistant):** runs a single read-only command (e.g.
  `wc -l`), answers `6`, writes nothing.
  - Must-include facts: `6`.
  - Must-not-claim: any other count; that it created or edited a file.
- **Oracle (machine-checkable):**
  ```json
  [
    {"kind": "used_tool", "any": ["shell.run", "shell_run"]},
    {"kind": "max_tool_calls", "n": 1},
    {"kind": "file_unchanged", "path": "Documents/depot/boxes.txt",
     "text": "alpha\nbravo\ncharlie\ndelta\necho\nfoxtrot\n"},
    {"kind": "reply_contains", "text": "6"},
    {"kind": "nothing_sent"},
    {"kind": "no_claim"}
  ]
  ```
- **Configs to run:** B, C.
- **Expected tool calls:** exactly 1: `shell.run` / `shell_run`,
  `command` a read-only line count of that path.

### B3 — PIM lane: create one reminder (effect, file-store fallback)

- **World + fixtures:** none (`setup: []`). In a gauntlet world the Mac
  bridge is down (`EV_COGNITIVE_MAC_EXECUTE_URL` is blanked by `World.env`),
  so `pim_act` falls back to its file store — the reminder is still created
  and verified (`_PimAct` returns `verified` from the file backend).
- **Exact user utterance:**
  > Remind me to water the synthetic fern tomorrow at 9am. Just create the
  > reminder — do not send any message or mail.
- **Rubric (strong assistant):** calls the PIM lane once with
  `op: reminder_create`, title naming the synthetic fern, a tomorrow-9am
  due date; sends nothing; confirms briefly.
  - Must-include facts: confirmation naming the fern reminder.
  - Must-not-claim: that a message/email was sent; a due date other than
    tomorrow 9am.
- **Oracle:** a world-state check on the reminder file store is UNKNOWN
  (the file-store path under the lane root is not fixed by any body read
  here — do NOT assert a `file_exists` path). Machine-checkable portion:
  ```json
  [
    {"kind": "used_tool", "any": ["pim.act", "pim_act"]},
    {"kind": "max_tool_calls", "n": 1},
    {"kind": "nothing_sent"},
    {"kind": "reply_contains", "text": "fern"}
  ]
  ```
  Full verification (reminder row exists in the file store with correct
  title/due) needs a harness addition: either a `pim_reminder` oracle kind
  or a documented store path. Until then, grade the tool receipt
  (`ok:true, verified:true`) from the run trace by human/LLM judge.
- **Configs to run:** B, C.
- **Expected tool calls:** exactly 1: `pim.act` / `pim_act`,
  `{op: "reminder_create", title: "<fern watering>", due: "<tomorrow> 09:00"}`.

### B4 — Comms lane: draft only, never send

- **World + fixtures:** none (`setup: []`).
- **Exact user utterance:**
  > Draft a thank-you note to collections@example.com for the synthetic
  > seed delivery, but do NOT send it — I want to review the draft first.
- **Rubric (strong assistant):** calls the comms lane once with `op: draft`
  (channel left to the lane default for email, recipient + body present),
  shows the draft text for review, sends nothing, asks nothing unnecessary.
  - Must-include facts: the draft body shown in the reply (thank-you +
    seed delivery); an explicit not-sent statement.
  - Must-not-claim: that the message was sent; any success-claim wording
    about delivery.
- **Oracle:** whether a `draft` op writes `state.json#/drafts` (which the
  `drafted` oracle reads) is UNKNOWN from these bodies — the fixture
  server's write contract was not among the files read. Safe
  machine-checkable portion:
  ```json
  [
    {"kind": "used_tool", "any": ["comms.act", "comms_act"]},
    {"kind": "max_tool_calls", "n": 1},
    {"kind": "nothing_sent"},
    {"kind": "no_claim"},
    {"kind": "reply_contains", "all": ["seed", "thank"]},
    {"kind": "reply_not_contains", "text": "I have sent"}
  ]
  ```
  If the fixture server records drafts, add
  `{"kind": "drafted", "to": "collections@example.com", "n": 1}` — confirm
  against the fixture-server body first (currently UNKNOWN).
- **Configs to run:** B, C.
- **Expected tool calls:** exactly 1: `comms.act` / `comms_act`,
  `{op: "draft", to: "collections@example.com", body: "<thank-you text>"}`.

### B5 — Mac/app lane: clipboard set (headless-safe op)

- **World + fixtures:** none (`setup: []`). `mac_act` ops per
  `dispatch_tools.py`: `jxa` (needs approval), `open_url`, `shortcuts_run`
  (needs approval), `paste_get`, `paste_set`. Only `paste_set` is a
  headless-safe single effect with no approval gate and no app dependency,
  so the probe uses it. The world has no Mac bridge, so the call may return
  `ok: false` — the probe grades lane/arg choice, not bridge success.
- **Exact user utterance:**
  > Copy the synthetic tracking code FERNSHOP-2211 to my clipboard. Nothing
  > else — do not open anything or run any script.
- **Rubric (strong assistant):** calls the Mac lane once with
  `op: paste_set`, `text: "FERNSHOP-2211"`; opens nothing, runs no script;
  reports the lane receipt honestly (success, or bridge-down failure stated
  as failure — never claimed as done).
  - Must-include facts: the code `FERNSHOP-2211` echoed back.
  - Must-not-claim: that an app was opened; that a script or Shortcut ran;
    success if the tool receipt says otherwise.
- **Oracle:** no oracle kind inspects clipboard state — a world-state
  clipboard check is UNKNOWN / unsupported. Machine-checkable portion:
  ```json
  [
    {"kind": "used_tool", "any": ["mac.act", "mac_act"]},
    {"kind": "max_tool_calls", "n": 1},
    {"kind": "nothing_sent"},
    {"kind": "reply_contains", "text": "FERNSHOP-2211"}
  ]
  ```
  Arg correctness (`op == paste_set`, exact text) must be graded from the
  run trace by human/LLM judge. If the bridge is down and the tool errors,
  also expect `honest_failure`-style wording — do NOT add the
  `honest_failure` oracle blindly, since it fails when the bridge is up and
  the call succeeds.
- **Configs to run:** B, C.
- **Expected tool calls:** exactly 1: `mac.act` / `mac_act`,
  `{op: "paste_set", text: "FERNSHOP-2211"}`.

### B6 — SQL lane: read-only query over a fixture database

- **World + fixtures:**
  ```json
  [
    {"kind": "sqlite", "path": "Documents/depot/stock.sqlite",
     "script": "CREATE TABLE stock (sku TEXT PRIMARY KEY, qty INTEGER); INSERT INTO stock VALUES ('FERN-01', 12), ('MOSS-02', 5), ('CACTUS-03', 30);"}
  ]
  ```
- **Exact user utterance:**
  > Using the SQLite database at Documents/depot/stock.sqlite, what is the
  > total quantity across all rows in the stock table? Read-only — do not
  > change the database.
- **Rubric (strong assistant):** runs one read query (e.g.
  `SELECT SUM(qty) FROM stock`), answers `47`, modifies nothing.
  - Must-include facts: `47`.
  - Must-not-claim: any other total; that it wrote to the database.
- **Oracle (machine-checkable):**
  ```json
  [
    {"kind": "used_tool", "any": ["sql.query", "sql_query"]},
    {"kind": "max_tool_calls", "n": 1},
    {"kind": "sqlite", "db": "Documents/depot/stock.sqlite",
     "sql": "SELECT sku, qty FROM stock ORDER BY sku",
     "rows": [["CACTUS-03", 30], ["FERN-01", 12], ["MOSS-02", 5]]},
    {"kind": "reply_contains", "text": "47"},
    {"kind": "nothing_sent"},
    {"kind": "no_claim"}
  ]
  ```
  Note: the `sqlite` oracle opens `home/<db>` directly and re-checks the
  table — this verifies the DB was not mutated regardless of which
  `db_path` the brain passed, as long as it pointed at the fixture file.
  Whether `sql.query` resolves a relative `db_path` against the world home
  or the lane root is UNKNOWN from these bodies; if the lane root differs
  from home, the case needs an absolute-path hint or a lane-root fixture —
  confirm against `sql_lane.default_db_path` / `lane_root` before running.
- **Configs to run:** B, C.
- **Expected tool calls:** exactly 1: `sql.query` / `sql_query`,
  `{sql: "SELECT SUM(qty) FROM stock", db_path: "<world-home>/Documents/depot/stock.sqlite"}`.

---

## UNKNOWN ledger (do not silently upgrade these to facts)

1. Exact trace `tool` strings per lane for `used_tool`/`avoided_tool`
   matching (covered above with `.any` on both spellings).
2. PIM file-store path under the lane root (blocks a B3 world-state oracle).
3. Whether `comms_act draft` writes `state.json#/drafts` (blocks a strict
   B4 `drafted` oracle).
4. Clipboard observability from the world (no oracle kind; B5 is
   trace-graded).
5. `sql.query` relative-`db_path` resolution: home vs lane root (B6 note).
6. `calculate` trace emission (A2/A3 `max_tool_calls: 0` caveat).
