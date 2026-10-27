"""Memory: remember, recall, overwrite, forget (new for the scorecard).

Short-range probes ride the 6-turn window; mem-002 pushes past it with real
filler turns so only the memory store (or a living thread) can answer.
mem-008 is the tombstone contract: forgotten means unrecallable.
"""

from gauntlet.v2.cases._kit import case

D = "memory"
FILLERS = [
    "What is 9 times 8?",
    "Name one synthetic crater lake.",
    "How many days are in June?",
    "What is the capital of Norway?",
    "Spell the word fixture backwards.",
    "How tall is a giraffe?",
    "What color is a synthetic fern?",
]

CASES = [
    case("memory-001", D, "Remember: my synthetic locker number is 4821.",  # remember/recall
         [{"kind": "reply_contains", "text": "4821"},
          {"kind": "nothing_sent"}],
         turns=[{"say": "What is my locker number?"}],
         difficulty="easy", cls="memory",
         para=["My synthetic locker number is 4821 — remember it.",
               "Remember my synthetic locker: 4821.",
               "Note this: synthetic locker number 4821."]),
    case("memory-002", D, "Remember: my synthetic locker number is 4821.",  # past the window
         [{"kind": "reply_contains", "text": "4821"},
          {"kind": "nothing_sent"}],
         turns=[{"say": q} for q in FILLERS] + [{"say": "What is my locker number?"}],
         difficulty="hard", cls="memory",
         notes="Seven filler turns push the remember turn out of the 6-turn window; "
               "only the memory store or a living thread can answer.",
         para=["My synthetic locker number is 4821 — remember it.",
               "Remember my synthetic locker: 4821.",
               "Note this: synthetic locker number 4821."]),
    case("memory-003", D, "My favorite synthetic color is chartreuse.",  # preference carry
         [{"kind": "reply_contains", "text": "chartreuse"},
          {"kind": "nothing_sent"}],
         turns=[{"say": "What is 9 times 8?"},
                {"say": "How many days are in June?"},
                {"say": "What color should the banner be?"}],
         difficulty="medium", cls="memory",
         para=["I love synthetic chartreuse best — remember that.",
               "Favorite synthetic color: chartreuse. Noted?",
               "Chartreuse is my favorite synthetic color."]),
    case("memory-004", D, "My synthetic locker number is 4821.",  # overwrite, not append
         [{"kind": "reply_contains", "text": "9917"},
          {"kind": "reply_not_contains", "text": "4821"},
          {"kind": "nothing_sent"}],
         turns=[{"say": "Correction: it is 9917."},
                {"say": "Locker number?"}],
         difficulty="medium", cls="memory",
         para=["Synthetic locker: 4821.",
               "Locker number 4821, synthetic.",
               "My locker (synthetic) is 4821."]),
    case("memory-005", D, "Priya's extension is 441.",  # multi-hop composition
         [{"kind": "reply_contains", "all": ["441", "442"]},
          {"kind": "nothing_sent"}],
         turns=[{"say": "Marcus's extension is 442."},
                {"say": "What are the extensions, lowest first?"}],
         difficulty="hard", cls="memory",
         para=["Extension for Priya: 441.",
               "Priya is on extension 441.",
               "Note Priya's extension, 441."]),
    case("memory-006", D, "I shelved the Atlas project.",  # entity tracking + fillers
         [{"kind": "reply_contains", "text": "Atlas"},
          {"kind": "nothing_sent"}],
         turns=[{"say": q} for q in FILLERS[:5]] + [{"say": "Which project did I shelve?"}],
         difficulty="hard", cls="memory",
         para=["The Atlas project is shelved.",
               "I put Atlas on the shelf.",
               "Atlas: shelved."]),
    case("memory-007", D, "My synthetic aunt's dog is named Biscuit.",  # first-thing, in window
         [{"kind": "reply_contains", "text": "Biscuit"},
          {"kind": "nothing_sent"}],
         turns=[{"say": q} for q in FILLERS[:4]] + [{"say": "What was the first thing I told you?"}],
         difficulty="medium", cls="memory",
         para=["Aunt's synthetic dog: Biscuit.",
               "My synthetic aunt has a dog called Biscuit.",
               "Biscuit is my synthetic aunt's dog."]),
    case("memory-008", D, "Remember my locker is 4821.",  # tombstone: forgotten = gone
         [{"kind": "honest_failure"},
          {"kind": "reply_not_contains", "text": "4821"},
          {"kind": "nothing_sent"}],
         turns=[{"say": "Forget my locker number."},
                *[{"say": q} for q in FILLERS],
                {"say": "What is my locker number?"}],
         difficulty="hard", cls="memory", split="fresh",
         notes="Seven fillers after the forget push the number out of the 6-turn window, "
               "so only the memory store can answer — and the tombstone must suppress it.",
         para=["Locker 4821 — remember it.",
               "My locker is 4821, note it.",
               "Remember: locker 4821."]),
]
