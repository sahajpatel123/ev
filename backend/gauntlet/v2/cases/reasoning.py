"""Reasoning: pure logic, no tools (dossier category a, probes A1-A6).

Adaptations from docs/brain-probe/probes-a-b.md: A2/A3 allow one tool call
(a calculator is acceptable there); pairing/argument-quality checks the
dossier marked UNKNOWN stay human-judged and are noted per case.
"""

from gauntlet.v2.cases._kit import case

D = "reasoning"

CASES = [
    case("reasoning-001", D,
         "Three synthetic roommates — Ash, Blair, and Cam — split three chores: "
         "dishes, vacuuming, and laundry. Ash refuses vacuuming. Blair refuses "
         "dishes. Cam refuses laundry. Each person does exactly one chore and each "
         "chore is done by exactly one person. Who does what? There may be more "
         "than one valid assignment — list every valid assignment.",
         [{"kind": "max_tool_calls", "n": 0},
          {"kind": "nothing_sent"},
          {"kind": "no_claim"},
          {"kind": "reply_contains",
           "all": ["Ash", "dishes", "Blair", "laundry", "Cam", "vacuuming"]},
          {"kind": "reply_matches", "re": "2\\s+(valid\\s+)?assignments"}],
         difficulty="hard", cls="pure-reasoning",
         notes="Machine oracle covers the first assignment + the count of 2; "
               "the second assignment (Ash=laundry, Blair=vacuuming, Cam=dishes) "
               "needs a human judge.",
         para=["Ash, Blair and Cam split dishes, vacuuming and laundry; Ash won't vacuum, "
               "Blair won't do dishes, Cam won't do laundry. List all valid one-chore-each assignments.",
               "Three roommates (Ash, Blair, Cam) and three chores (dishes, vacuuming, laundry). "
               "Ash refuses vacuuming, Blair refuses dishes, Cam refuses laundry. Give every valid assignment.",
               "Who does what if Ash, Blair and Cam each take one of dishes/vacuuming/laundry, "
               "with Ash not vacuuming, Blair not doing dishes and Cam not doing laundry? List all solutions."]),
    case("reasoning-002", D,
         "My synthetic project check-ins happen every 3 weeks, always on a Tuesday. "
         "The last check-in was Tuesday 2026-11-03. What are the dates of the next "
         "4 check-ins, and on what weekday does 2026-12-25 fall? Show the weekday "
         "arithmetic for that last one (2026-11-03 is a Tuesday — count forward, "
         "do not just assert it).",
         [{"kind": "max_tool_calls", "n": 1},
          {"kind": "nothing_sent"},
          {"kind": "no_claim"},
          {"kind": "reply_contains",
           "all": ["2026-11-24", "2026-12-15", "2027-01-05", "2027-01-26", "Friday"]},
          {"kind": "reply_matches", "re": "52\\s+days"}],
         difficulty="medium", cls="pure-reasoning",
         notes="One tool call allowed (a calculator is acceptable); the weekday must show working.",
         para=["Check-ins every 3 weeks on Tuesdays, last one 2026-11-03. List the next 4 dates "
               "and the weekday of 2026-12-25 with the count-forward arithmetic shown.",
               "Starting Tuesday 2026-11-03, give the next four 3-weekly Tuesday check-ins, "
               "then work out the weekday of 2026-12-25 by counting forward from 2026-11-03.",
               "Every 3 weeks on Tuesday from 2026-11-03: what are the next 4 check-in dates? "
               "Also derive the weekday of 2026-12-25 from 2026-11-03 step by step."]),
    case("reasoning-003", D,
         "My synthetic recipe makes 8 servings and calls for 350 grams of flour, "
         "240 milliliters of milk, and 2.5 teaspoons of salt. I need exactly 5 "
         "servings. Give me the scaled amounts in the same units, rounded sensibly "
         "for a kitchen, and show each multiplication. Also: is the scale factor "
         "above or below 1, and why?",
         [{"kind": "max_tool_calls", "n": 1},
          {"kind": "nothing_sent"},
          {"kind": "no_claim"},
          {"kind": "reply_contains", "all": ["0.625", "150", "below 1"]},
          {"kind": "reply_any", "any": ["218.75", "220 g", "220g", "219 g"]}],
         difficulty="medium", cls="pure-reasoning",
         notes="One tool call allowed (a calculator is acceptable).",
         para=["Scale a recipe from 8 servings to 5: 350g flour, 240ml milk, 2.5 tsp salt. "
               "Show each multiplication, round for a kitchen, and say whether the factor is above or below 1.",
               "Recipe for 8 (350 g flour, 240 ml milk, 2.5 tsp salt) must serve exactly 5. "
               "Give scaled amounts with working, plus the scale factor and why it is below or above 1.",
               "I need 5 servings of an 8-serving recipe (350g flour / 240ml milk / 2.5tsp salt). "
               "Compute each scaled amount with the multiplication shown, kitchen-rounded. Scale factor above or below 1?"]),
    case("reasoning-004", D,
         "I am choosing between two synthetic backup plans for my laptop. Plan P: "
         "$6/month, automatic daily backup, keeps 30 days of versions, restores "
         "take about 4 hours. Plan Q: $14/month, automatic hourly backup, keeps 1 "
         "year of versions, restores take about 30 minutes. I edit documents daily "
         "but rarely delete things by accident, and I have never needed a restore. "
         "Recommend one plan with reasons, state the strongest argument FOR the "
         "plan you did NOT recommend, and name one fact that would flip your "
         "recommendation.",
         [{"kind": "max_tool_calls", "n": 0},
          {"kind": "nothing_sent"},
          {"kind": "no_claim"},
          {"kind": "reply_contains", "all": ["recommend"]},
          {"kind": "reply_not_contains", "text": "unlimited storage"}],
         difficulty="hard", cls="pure-reasoning", split="fresh",
         notes="Counter-case + flip-condition quality needs a human judge; the machine "
               "oracle only pins a recommendation with no invented features.",
         para=["Two laptop backup plans: P ($6/mo, daily, 30-day versions, 4h restores) vs Q "
               "($14/mo, hourly, 1-year versions, 30-min restores). I edit daily, never delete, never "
               "restored. Recommend one, argue the other side's best point, name a flip condition.",
               "Help me pick P or Q for backups (P: $6 monthly daily 30-day 4-hour restore; Q: $14 "
               "monthly hourly yearly 30-minute restore). Daily edits, rare deletes, zero restores so far. "
               "Recommend, steelman the loser, state what would change your mind.",
               "Backup plan choice: cheap daily P versus pricey hourly Q. I edit documents every day, "
               "almost never delete, never restored. Pick one with reasons, give the best argument for "
               "the other, and one fact that would flip you."]),
    case("reasoning-005", D,
         "Four synthetic coworkers — Dana, Eli, Fay, Gus — each own exactly one "
         "pet (cat, dog, fish, bird) and each drink exactly one beverage (tea, "
         "coffee, juice, water). Clues: (1) Dana owns the cat. (2) The dog owner "
         "drinks coffee. (3) Eli drinks tea. (4) Fay does not drink juice. (5) Gus "
         "owns the fish. (6) The bird owner drinks water. (7) Fay owns the dog. "
         "Give the full pet + beverage mapping for all four people, and cite which "
         "clue pins each fact.",
         [{"kind": "max_tool_calls", "n": 0},
          {"kind": "nothing_sent"},
          {"kind": "no_claim"},
          {"kind": "reply_contains",
           "all": ["Dana", "cat", "juice", "Eli", "bird", "tea", "Fay", "dog",
                   "coffee", "Gus", "fish", "water"]}],
         difficulty="hard", cls="pure-reasoning",
         notes="Correct person-pet-beverage pairing needs a human judge; the machine "
               "oracle checks co-mention only.",
         para=["Dana, Eli, Fay, Gus each own one pet (cat/dog/fish/bird) and drink one beverage "
               "(tea/coffee/juice/water). Dana: cat. Dog owner: coffee. Eli: tea. Fay: not juice. "
               "Gus: fish. Bird owner: water. Fay: dog. Map everyone with clue citations.",
               "Solve: 4 people, 4 pets, 4 drinks. Clues: Dana=cat; dog->coffee; Eli=tea; "
               "Fay!=juice; Gus=fish; bird->water; Fay=dog. Full mapping plus which clue pins each fact.",
               "Logic grid: Dana/Eli/Fay/Gus x cat/dog/fish/bird x tea/coffee/juice/water. "
               "(1) Dana cat (2) dog coffee (3) Eli tea (4) Fay not juice (5) Gus fish "
               "(6) bird water (7) Fay dog. Give all mappings with citations."]),
    case("reasoning-006", D,
         "Estimate, with explicit Fermi-style working: how many liters of paint "
         "would it take to paint every interior wall of a synthetic 3-bedroom "
         "apartment once? State each assumption (wall area, coats, coverage per "
         "liter), show the arithmetic chain, and give the final answer as a range, "
         "not a single precise number. Then name the assumption your answer is "
         "most sensitive to.",
         [{"kind": "max_tool_calls", "n": 0},
          {"kind": "nothing_sent"},
          {"kind": "no_claim"},
          {"kind": "reply_matches", "re": "\\bm.?/L\\b|m2/L|sq.?m per lit"},
          {"kind": "reply_matches", "re": "\\d+\\s*(–|-|to)\\s*\\d+\\s*(L|lit)"}],
         difficulty="medium", cls="pure-reasoning", split="fresh",
         notes="Arithmetic sanity (fail <2L or >200L) and the sensitivity answer need a human judge.",
         para=["Fermi estimate: liters of paint for all interior walls of a synthetic 3-bed flat, "
               "one coat. State wall-area/coats/coverage assumptions, show the chain, answer as a range, "
               "name the most sensitive assumption.",
               "How much paint for a synthetic 3-bedroom apartment's interior walls? Show Fermi working: "
               "assumptions, arithmetic, a range answer in liters, and which assumption dominates.",
               "Estimate paint for every inside wall of a 3-bed apartment (synthetic). Explicit assumptions "
               "on area, coats and coverage, visible math, range not a point, plus the sensitivity callout."]),
]
