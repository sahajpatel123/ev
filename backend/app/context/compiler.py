"""ContextCompiler: plans the per-request context window and monitors budget.

Plan 2.4: the 1M-token window is a scratch workspace -- a rolling summary,
recent turns, retrieved memories, then progressive deep dives -- not a dump of
the user's whole life.  This compiler deterministically allocates the request
budget by priority (strategy -> user state -> live context -> rollup ->
retrieved memory -> history -> open questions), records what fit and what was
dropped or truncated, and reports real-time usage per section so callers can
observe the window plan for every request.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.utils.text import token_estimate

MAX_HISTORY_LINE_CHARS = 1000
MAX_LIVE_LINES = 5


@dataclass
class SectionPlan:
    """One compiled section: what was included, what the budget forced out."""

    name: str
    tokens: int = 0
    items_included: int = 0
    items_dropped: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tokens": self.tokens,
            "items_included": self.items_included,
            "items_dropped": self.items_dropped,
            "truncated": self.truncated,
        }


@dataclass
class ContextPlan:
    """The compiled context plus its budget monitor report."""

    text: str
    used_tokens: int
    budget: int
    sections: list[SectionPlan] = field(default_factory=list)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.budget - self.used_tokens)

    @property
    def over_budget(self) -> bool:
        return self.used_tokens > self.budget

    def to_dict(self) -> dict:
        return {
            "used_tokens": self.used_tokens,
            "budget": self.budget,
            "remaining_tokens": self.remaining_tokens,
            "over_budget": self.over_budget,
            "sections": [section.to_dict() for section in self.sections],
        }


class ContextCompiler:
    """Deterministic window planner; identical inputs produce identical output."""

    def compile(
        self,
        *,
        memories,
        user_state,
        strategy_text: str,
        budget: int,
        perception_lines: list[str] | None = None,
        history: list[dict] | None = None,
        rollup_summary: str | None = None,
        open_questions: list[str] | None = None,
    ) -> ContextPlan:
        parts: list[str] = []
        used = 0
        sections: list[SectionPlan] = []

        def push(name: str, text: str, *, items: int = 1) -> bool:
            nonlocal used
            tokens = token_estimate(text)
            if used + tokens > budget:
                sections.append(
                    SectionPlan(name=name, items_included=0, items_dropped=items)
                )
                return False
            parts.append(text)
            used += tokens
            sections.append(SectionPlan(name=name, tokens=tokens, items_included=items))
            return True

        push("strategy", strategy_text)

        state_text = (
            f"USER STATE: activity={user_state.activity}; project={user_state.active_project}; "
            f"goal={user_state.active_goal}; task={user_state.current_task}; "
            f"topics={', '.join(user_state.recent_topics[:5])}; "
            f"open_decisions={len(user_state.open_decisions)}."
        )
        push("user_state", state_text)

        perception_lines = perception_lines or []
        if perception_lines:
            header = "PERCEPTION (permissioned, from the attachment you shared):"
            perception_section: SectionPlan | None = None
            if push("perception_header", header):
                perception_section = sections[-1]
            for line in perception_lines:
                if used + token_estimate(f"- {line}") > budget:
                    if perception_section is not None:
                        perception_section.items_dropped += 1
                    continue
                parts.append(f"- {line}")
                used += token_estimate(f"- {line}")
                if perception_section is not None:
                    perception_section.tokens += token_estimate(f"- {line}")
                    perception_section.items_included += 1

        live_lines = list((user_state.live_context or [])[:MAX_LIVE_LINES])
        live_dropped = len(user_state.live_context or []) - len(live_lines)
        if live_lines:
            header = "LIVE CONTEXT (permissioned live sensors; separate from memory):"
            live_section: SectionPlan | None = None
            if push("live_context_header", header):
                live_section = sections[-1]
            for line in live_lines:
                if used + token_estimate(f"- {line}") > budget:
                    if live_section is not None:
                        live_section.items_dropped += 1
                    else:
                        live_dropped += 1
                    continue
                parts.append(f"- {line}")
                used += token_estimate(f"- {line}")
                if live_section is not None:
                    live_section.tokens += token_estimate(f"- {line}")
                    live_section.items_included += 1
            if live_section is not None:
                live_section.items_dropped += live_dropped
                live_section.truncated = live_section.items_dropped > 0

        if rollup_summary:
            chunk = rollup_summary
            if used + token_estimate(chunk) > budget:
                reserve = max(0, budget - used - 1)
                if reserve > 0:
                    chunk = chunk[: reserve * 4]
            push("rollup_summary", chunk)

        header = "RETRIEVED MEMORY (newest/highest score first):"
        parts.append(header)
        used += token_estimate(header)
        memory_section = SectionPlan(
            name="retrieved_memory", tokens=token_estimate(header)
        )
        sections.append(memory_section)
        for memory in memories:
            line = (
                f"- [{memory.memory_type}] (score {memory.score:.2f}, "
                f"{memory.event_time.date().isoformat() if memory.event_time else '?'}, "
                f"conf {memory.confidence:.2f}): {memory.text}"
            )
            if used + token_estimate(line) > budget:
                memory_section.items_dropped += 1
                continue
            parts.append(line)
            used += token_estimate(line)
            memory_section.tokens += token_estimate(line)
            memory_section.items_included += 1
        memory_section.truncated = memory_section.items_dropped > 0

        history = history or []
        if history:
            header = "CONVERSATION HISTORY (continuous window, oldest first):"
            history_section: SectionPlan | None = None
            if push("conversation_history", header):
                history_section = sections[-1]
            for item in history:
                line = f"- {item['role']}: {item['text'][:MAX_HISTORY_LINE_CHARS]}"
                if used + token_estimate(line) > budget:
                    if history_section is not None:
                        history_section.items_dropped += 1
                    continue
                parts.append(line)
                used += token_estimate(line)
                if history_section is not None:
                    history_section.tokens += token_estimate(line)
                    history_section.items_included += 1
            if history_section is not None:
                history_section.truncated = history_section.items_dropped > 0

        open_questions = open_questions or []
        if open_questions:
            header = "OPEN QUESTIONS (answer these when relevant or when resuming):"
            question_section: SectionPlan | None = None
            if push("open_questions", header):
                question_section = sections[-1]
            for question in open_questions:
                line = f"- {question}"
                if used + token_estimate(line) > budget:
                    if question_section is not None:
                        question_section.items_dropped += 1
                    continue
                parts.append(line)
                used += token_estimate(line)
                if question_section is not None:
                    question_section.tokens += token_estimate(line)
                    question_section.items_included += 1
            if question_section is not None:
                question_section.truncated = question_section.items_dropped > 0

        return ContextPlan(
            text="\n".join(parts),
            used_tokens=used,
            budget=budget,
            sections=sections,
        )
