"""The real-brain harness, exercised with a scripted stand-in for the model (no spend)."""

from __future__ import annotations

import json

from hands_cug import brain
from hands_cug.core import SimEnv


class ScriptedBudget(brain.Budget):
    """Same loop and accounting as the real Budget; the provider call is scripted."""

    def __init__(self, script):
        self.key_remaining, self.configured, self.cap, self.spent, self.calls = 5.0, 10.0, 1.0, 0.0, 0
        self.model, self.effort, self.base, self.cost_per_call = "scripted", "medium", "", []
        self._script = list(script)

    def chat(self, messages):
        self.calls += 1
        self.cost_per_call.append(0.0004)
        self.spent += 0.0004
        return self._script.pop(0)


def _call(name, args, i=0):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def _say(text):
    return {"choices": [{"message": {"content": text}}]}


def test_the_brain_chooses_the_skill_and_the_task_oracle_judges_the_world():
    from hands_cug.tasks_sim import t2_countdown5

    env = SimEnv()
    b = ScriptedBudget([_call("skill_find", {"intent": "skip the ad"}), _call("skill_run", {"name": "web_media.skip_ad"}, 1),
                        _say("Skipped the ad.")])
    bh = brain.BrainHands(env.hands, b, "uh skip the add please")
    env.hands = bh
    v = t2_countdown5(env)
    assert v.ok and bh.chosen == ["web_media.skip_ad"] and bh.final_text == "Skipped the ad." and b.calls == 3


def test_a_wrong_skill_choice_fails_the_oracle_and_is_not_hidden():
    from hands_cug.tasks_sim import t2_countdown5

    env = SimEnv()
    b = ScriptedBudget([_call("skill_run", {"name": "web_media.pause"}), _say("Paused.")])
    env.hands = brain.BrainHands(env.hands, b, "skip the ad")
    v = t2_countdown5(env)
    assert not v.ok  # the ad was not skipped, whatever the brain said


def test_budget_cap_is_the_smaller_of_config_and_key_headroom_minus_reserve(monkeypatch):
    monkeypatch.setattr(brain, "headroom", lambda: {"ok": True, "remaining": 0.206, "limit": 5, "usage": 4.8})
    b = brain.Budget()
    assert b.cap == 0.0  # 0.206 - 0.25 reserve: the live assistant's credit is not spent on evaluation
    monkeypatch.setattr(brain, "headroom", lambda: {"ok": True, "remaining": 3.0, "limit": 5, "usage": 2.0})
    assert abs(brain.Budget().cap - 2.75) < 1e-9


def test_run_records_an_owner_action_when_there_is_nothing_to_spend(monkeypatch):
    monkeypatch.setattr(brain, "headroom", lambda: {"ok": True, "remaining": 0.1, "limit": 5, "usage": 4.9})
    out = brain.run({"media_t2"}, limit=3)
    assert "raise the OpenRouter key limit" in out["owner_action"] and out["tasks"] == []


def test_eligible_tasks_make_exactly_one_matching_skill_call():
    pool = brain.eligible({"media_controls"})
    assert pool and all(t.skill for t in pool)
