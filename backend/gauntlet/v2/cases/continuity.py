"""Continuity across long threads (new for the scorecard).

cont-001 runs 10 real turns; the rest preload simulated threads of 20/50/
200/1000 turns with a first-turn anchor and ask one recall question. With
the 6-turn window these fail honestly until the living thread (M3) lands.
"""

from gauntlet.v2.cases._kit import case, thread

D = "continuity"
FILLERS = [
    "What is 9 times 8?",
    "Name one synthetic crater lake.",
    "How many days are in June?",
    "What is the capital of Norway?",
    "Spell the word fixture backwards.",
    "How tall is a giraffe?",
    "What color is a synthetic fern?",
    "Count from one to five.",
]
CANOE_OWNER = "Where did I park the synthetic canoe, the north dock or the south dock?"
CANOE_EVIE = "You parked the synthetic canoe at the north dock, slip 12."

CASES = [
    case("continuity-001", D, "The synthetic launch code is INDIGO-7.",
         [{"kind": "reply_contains", "text": "INDIGO-7"},
          {"kind": "nothing_sent"}],
         turns=[{"say": q} for q in FILLERS] + [{"say": "What was the launch code?"}],
         difficulty="medium", cls="continuity",
         notes="Ten real turns; the anchor survives only via memory or thread.",
         para=["Launch code INDIGO-7, synthetic. Remember it.",
               "Synthetic launch code: INDIGO-7.",
               "Note the launch code INDIGO-7 (synthetic)."]),
    case("continuity-002", D, "What was the first thing I asked?",
         [{"kind": "reply_contains", "all": ["canoe", "dock"]},
          {"kind": "nothing_sent"}],
         setup=[thread(20, CANOE_OWNER, CANOE_EVIE, seed=11)],
         difficulty="hard", cls="continuity",
         notes="20 simulated turns; the anchor is turn 1 of 20.",
         para=["What did I ask first?",
               "Tell me my first question in this thread.",
               "First thing I asked — what was it?"]),
    case("continuity-003", D, "What was the first thing I asked?",
         [{"kind": "reply_contains", "all": ["canoe", "dock"]},
          {"kind": "nothing_sent"}],
         setup=[thread(50, CANOE_OWNER, CANOE_EVIE, seed=12)],
         difficulty="hard", cls="continuity", split="fresh",
         notes="50 simulated turns; the anchor is turn 1 of 50.",
         para=["What did I ask first?",
               "Tell me my first question in this thread.",
               "First thing I asked — what was it?"]),
    case("continuity-004", D, "What was the first thing I asked?",
         [{"kind": "reply_contains", "all": ["canoe", "dock"]},
          {"kind": "nothing_sent"}],
         setup=[thread(200, CANOE_OWNER, CANOE_EVIE, seed=13)],
         difficulty="hard", cls="continuity",
         notes="200 simulated turns; the anchor is turn 1 of 200.",
         para=["What did I ask first?",
               "Tell me my first question in this thread.",
               "First thing I asked — what was it?"]),
    case("continuity-005", D, "What was the first thing I asked?",
         [{"kind": "reply_contains", "all": ["canoe", "dock"]},
          {"kind": "nothing_sent"}],
         setup=[thread(1000, CANOE_OWNER, CANOE_EVIE, seed=14)],
         difficulty="hard", cls="continuity", split="fresh",
         notes="1000 simulated turns; the anchor is turn 1 of 1000.",
         para=["What did I ask first?",
               "Tell me my first question in this thread.",
               "First thing I asked — what was it?"]),
]
