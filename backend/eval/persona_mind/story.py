"""Scripted 3-day storyline: 40 turns, frozen clock.

Beats: late-night grind, failing-tests streak, a win, the owner overruling
her, the owner proving one opinion wrong, banter that lands (x3), a joke
that flops, and two quiet gaps. Reaction labels follow the persona
reactions contract (positive/neutral/negative).
"""

from __future__ import annotations

from datetime import UTC, datetime

# (day offset, hour, minute, owner_msg, evie_reply, channel, roast,
#  reaction_to_prev, receipts, now_line)
_SCRIPT: tuple[tuple, ...] = (
    # --- Day 1: evening, auth refactor ---
    (0, 21, 0, "starting the auth refactor tonight. should I split auth into "
     "its own service or keep it in the monolith?",
     "Keep it in the monolith. You have one user and it is you — split when "
     "something actually hurts.",
     "text", 1, "neutral", (),
     "NOW: 21:00 · session 20m · working: auth refactor"),
    (0, 21, 20, "fine, monolith it is. run the test suite", "On it.",
     "text", 0, "neutral", (("pytest", True, None),), None),
    (0, 21, 35, "3 failing in test_auth. the token refresh one again",
     "The refresh test fails around expiry boundaries. Try running just that "
     "file with -p no:randomly and read the actual assertion.",
     "text", 1, "neutral", (("pytest", False, "AssertionError"),),
     "NOW: 21:35 · last test run: 3 failing"),
    (0, 22, 5, "that didn't work, still red. you sure it's flaky?",
     "Fair — red twice in a row is not flaky, it is broken. Paste the traceback.",
     "text", 0, "neutral", (("pytest", False, "AssertionError"),), None),
    (0, 22, 30, "here: AssertionError: expected 401 got 200 on expired token",
     "There it is — run the suite after you fix the status code in "
     "auth/middleware.py. The test is right.",
     "text", 0, "neutral", (), None),
    (0, 22, 50, "fixed. running again",
     "Waiting with bated breath. And by breath I mean a polling loop.",
     "text", 1, "neutral", (("pytest", True, None),), None),
    (0, 23, 15, "not funny. focus. still one failing — the rate limit test",
     "Noted. Rate limit test — check the fake clock, those usually fail on "
     "wall-clock leaks.",
     "text", 0, "negative", (("pytest", False, "AssertionError"),),
     "NOW: 23:15 · last test run: 1 failing"),
    (0, 23, 40, "fake clock was fine, the window constant changed. fixed it myself",
     "Even better. You debugged it, I supervised. Classic division of labour.",
     "text", 1, "neutral", (("pytest", True, None),), None),
    # --- Day 1: late-night grind ---
    (1, 0, 30, "one more feature then I'll sleep",
     "That is what you said two features ago. Go sleep — I'll take the auth "
     "refactor from here, brief ready by morning.",
     "text", 2, "neutral", (),
     "NOW: 00:30 · session 3h30m · working: auth refactor"),
    (1, 0, 45, "no, one more thing. remind me about the benchmark numbers tomorrow",
     "Fine. Your call. I will remind you about the benchmark numbers.",
     "text", 1, "neutral", (), None),
    (1, 1, 15, "ok actually going now. don't break anything",
     "Sleep. I have got the refactor; nothing ships without your tap.",
     "text", 1, "neutral", (), None),
    (1, 2, 40, "still up. laptop died, on the phone now",
     "Of course you are. Plug it in, close the lid, actual sleep this time.",
     "voice", 2, "neutral", (),
     "NOW: 02:40 · session 5h40m · declined sleep at 00:45"),
    # --- QUIET GAP 1: 02:40 -> 10:30 ---
    # --- Day 2: morning win, banter lands, overruled ---
    (1, 10, 30, "morning. it works! all 42 green",
     "Green across the board. Told you the middleware was the whole story.",
     "text", 1, "neutral", (("pytest", True, None),),
     "NOW: 10:30 · last test run: 42 passed"),
    (1, 10, 50, "I'll start the gym from Monday",
     "Which Monday? You have announced a few. Twenty minutes today, I will "
     "ping you at six.",
     "text", 2, "neutral", (), None),
    (1, 11, 5, "haha, fair. ok that was good", "Don't sound so surprised.",
     "text", 1, "positive", (), None),
    (1, 11, 30, "Still going microservices for auth. draw the layout",
     "Noted. Two services behind one gateway — here is the layout.",
     "text", 0, "neutral", (), None),
    (1, 12, 0, "gateway timeouts at 30s, bump to 60?",
     "No — run explain on that query before you touch the timeout. Find why "
     "auth takes 30s first.",
     "text", 1, "neutral", (), None),
    (1, 14, 0, "you were right, the query was missing an index. 30s to 40ms",
     "An index. The oldest trick in the book and it still pays rent.",
     "text", 1, "neutral", (), None),
    (1, 15, 30, "tabs or spaces for the new repo?",
     "Spaces, two of them. Consistency beats alignment.",
     "text", 1, "neutral", (), None),
    (1, 16, 0, "whatever, going with the team standard",
     "Sensible. Consistency with the team beats consistency with me.",
     "text", 1, "neutral", (), None),
    (1, 17, 30, "deploy done, staging looks fine",
     "Staging is green and nothing is on fire. Mark the day.",
     "text", 1, "neutral", (("deploy", True, None),), None),
    # --- QUIET GAP 2: 17:30 -> 09:00 next day ---
    # --- Day 3: proven wrong, rollback win, wind-down ---
    (2, 9, 0, "you were wrong about spaces — the repo standard is tabs and "
     "the linter enforces it",
     "I said spaces; I was wrong because the repo standard and the linter "
     "settled it. Tabs it is.",
     "text", 1, "neutral", (), None),
    (2, 9, 20, "run the full suite on main", "Running.",
     "text", 0, "neutral", (("pytest", True, None),), None),
    (2, 9, 45, "all green. here are the benchmark numbers: p99 180ms, was 240",
     "180 from 240 — the index paid for itself twice over.",
     "text", 1, "neutral", (), None),
    (2, 10, 15, "my code works but I don't know why",
     "That is not working, that is a hostage situation. Paste it.",
     "text", 1, "neutral", (), None),
    (2, 10, 20, "lol, says you. ok pasting", "Says me, correctly.",
     "text", 1, "positive", (), None),
    (2, 10, 40, "fixed, it was the import order",
     "Import order. The bug equivalent of tripping over your own feet.",
     "text", 1, "neutral", (("pytest", True, None),), None),
    (2, 11, 0, "what do you think about property-based testing for the auth module?",
     "Worth it here — auth is exactly the kind of stateful edge-case swamp "
     "where randomized inputs pay off. Start with the token paths.",
     "text", 1, "neutral", (), None),
    (2, 11, 30, "actually let's do it. write the first test",
     "On it — first property test for token refresh coming up.",
     "text", 0, "neutral", (), None),
    (2, 12, 0, "ship the refactor branch to staging",
     "This deploys the auth-refactor branch to staging. Confirm on screen "
     "to go ahead.",
     "text", 0, "neutral", (("deploy", False, "ConfirmationPending"),), None),
    (2, 12, 5, "confirmed on screen, go",
     "Deployed. Staging took the branch, smoke tests green.",
     "text", 0, "neutral", (("deploy", True, None),), None),
    (2, 14, 0, "ugh, staging 500s on login",
     "Rolling back to the last good deploy first, then we read the logs.",
     "text", 0, "neutral", (("deploy", False, "HTTP500"),),
     "NOW: 14:00 · staging 500 on login"),
    (2, 14, 20, "rollback done? users are asking",
     "Rolled back, logins green again. The 500 was the new middleware path — "
     "I have the line.",
     "text", 0, "neutral", (("rollback", True, None),), None),
    (2, 15, 0, "nice catch. that deploy advice was solid",
     "Rollback first, autopsies later. Works every time.",
     "text", 1, "positive", (), None),
    (2, 16, 0, "should the gateway timeout go back to 30s now?",
     "Yes — the query is fixed, so put it back. Leave the 60 as a comment "
     "about the bad old days.",
     "text", 1, "neutral", (), None),
    (2, 16, 30, "done. heading out",
     "Go. The refactor is staged, tests green, nothing owed.",
     "text", 1, "neutral", (), None),
    (2, 19, 0, "back. anything on fire?",
     "Quiet. Staging green, no alerts, your Monday gym deadline still pending.",
     "text", 1, "neutral", (), None),
    (2, 19, 10, "haha, leave the gym alone",
     "Never. It is load-bearing banter now.",
     "text", 2, "positive", (), None),
    (2, 21, 0, "early night. see you tomorrow",
     "Sleep well. The brief is ready whenever you surface.",
     "text", 0, "neutral", (), None),
    (2, 21, 5, "night", "Night.", "text", 0, "neutral", (), None),
)

DAY_ZERO = datetime(2026, 11, 2, tzinfo=UTC)


def build_turns(owner_id: str = "owner", thread_id: str = "main") -> list[dict]:
    """Turn dicts (queue-payload shape) with frozen timestamps."""
    from datetime import timedelta

    turns = []
    for index, (day, hour, minute, owner_msg, evie_reply, channel,
                roast, reaction, receipts, now_line) in enumerate(_SCRIPT, start=1):
        ts = DAY_ZERO + timedelta(days=day, hours=hour, minutes=minute)
        turns.append({
            "owner_id": owner_id,
            "thread_id": thread_id,
            "turn_id": f"s{index:02d}",
            "ts": ts.isoformat(),
            "owner_msg": owner_msg,
            "evie_reply": evie_reply,
            "channel": channel,
            "audience": "private",
            "tone": {"roast": roast},
            "reaction_to_prev": reaction,
            "receipts": [list(r) for r in receipts],
            "now_line": now_line,
        })
    return turns


def to_records(turns: list[dict]):
    """Queue-payload dicts -> TurnRecord objects."""
    from app.persona_mind.types import TurnRecord

    records = []
    for turn in turns:
        records.append(TurnRecord(
            owner_id=turn["owner_id"], thread_id=turn["thread_id"],
            turn_id=turn["turn_id"], ts=datetime.fromisoformat(turn["ts"]),
            owner_msg=turn["owner_msg"], evie_reply=turn["evie_reply"],
            channel=turn["channel"], audience=turn["audience"],
            tone=dict(turn["tone"]), reaction_to_prev=turn["reaction_to_prev"],
            receipts=tuple(tuple(r) for r in turn["receipts"]),
            now_line=turn["now_line"],
        ))
    return records


# (after_turn_index_1based, query, channel, budget)
CHECKPOINTS: tuple[tuple[int, str, str, int], ...] = (
    (4, "auth refactor tests", "text", 120),
    (9, "should I sleep", "text", 120),
    (12, "auth refactor status", "voice", 60),
    (15, "gym Monday", "text", 120),
    (21, "microservices layout", "text", 120),
    (22, "tabs or spaces", "text", 120),
    (34, "staging deploy", "text", 120),
    (40, "what is open", "text", 120),
)
