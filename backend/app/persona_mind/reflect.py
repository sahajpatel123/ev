"""SLOW LOOP: salience, reflector prompt, gateway call, state application.

The reflector appraises queued turns the way Evie would and returns strict
JSON; everything it returns passes through validate.py before touching
state. Bookkeeping (reactions, replies, expiry) is deterministic and always
runs; the brain call is skipped cleanly when the brain cannot serve.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.persona_mind.settings import MAX_INTENTIONS, SEED_CONFIDENCE
from app.persona_mind.state import (
    apply_decay,
    clamp01,
    coerce_ts,
    expire_items,
    new_state,
    push_reply,
    record_reaction,
    utcnow,
)
from app.persona_mind.temperament import describe as temperament_text
from app.persona_mind.temperament import values_text
from app.persona_mind.validate import normalize_text, normalize_topic, validate_output

Reflector = Callable[[list[dict[str, str]]], dict[str, Any] | None]

_WIN_RE = re.compile(
    r"\b(it works|tests? (pass|passing|green|fixed)|fixed it|you were right|"
    r"that worked|shipped|merged|we did it|it'?s alive)\b",
    re.IGNORECASE,
)
_DISAGREE_RE = re.compile(
    r"\b(i disagree|you'?re wrong|that'?s (not right|wrong)|bad idea|"
    r"don'?t think so|no,? (that'?s|thats) not)\b",
    re.IGNORECASE,
)
_PROVEN_WRONG_RE = re.compile(
    r"\b(you were wrong|that didn'?t work|i told you|actually,? (no|it|that)|"
    r"see\? (told|i said))\b",
    re.IGNORECASE,
)


def is_salient(turn: dict[str, Any]) -> str | None:
    """Why this turn forces a reflection, or None when it is routine."""
    for receipt in turn.get("receipts") or []:
        ok = receipt[1] if isinstance(receipt, (list, tuple)) and len(receipt) > 1 else True
        if not ok:
            return "failed_receipt"
    msg = str(turn.get("owner_msg") or "")
    if _PROVEN_WRONG_RE.search(msg):
        return "proven_wrong"
    if _DISAGREE_RE.search(msg):
        return "owner_disagreeing"
    if _WIN_RE.search(msg):
        return "big_win"
    if str(turn.get("reaction_to_prev") or "") in ("positive", "negative"):
        return "strong_reaction"
    return None


def _receipt_view(receipts: Any) -> list[dict[str, Any]]:
    """Receipts reduced to tool name + ok/error. Never args or payloads."""
    view = []
    for receipt in receipts or []:
        if isinstance(receipt, (list, tuple)) and receipt:
            view.append({
                "tool": str(receipt[0])[:80],
                "ok": bool(receipt[1]) if len(receipt) > 1 else True,
                "error": str(receipt[2])[:80] if len(receipt) > 2 and receipt[2] else None,
            })
    return view


def _turn_block(turn: dict[str, Any]) -> str:
    receipts = _receipt_view(turn.get("receipts"))
    return (
        f"turn_id={turn.get('turn_id')} ts={turn.get('ts')} "
        f"channel={turn.get('channel')} reaction_to_prev={turn.get('reaction_to_prev')}\n"
        f"owner: {str(turn.get('owner_msg') or '')[:800]}\n"
        f"evie: {str(turn.get('evie_reply') or '')[:800]}\n"
        f"receipts: {json.dumps(receipts)}"
    )


def build_prompt(
    batch: list[dict[str, Any]],
    state: dict[str, Any],
    context: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    turn_lines = [_turn_block(turn) for turn in batch]
    context_lines = [_turn_block(turn) for turn in (context or [])]
    all_opinions = [op for op in (state.get("opinions") or []) if isinstance(op, dict)]
    recent = sorted(
        all_opinions, key=lambda op: str(op.get("updated_at") or ""), reverse=True,
    )[:12]
    opinions = [
        f"{op.get('topic')}: {op.get('stance')} (conf {op.get('confidence')})"
        for op in recent
    ]
    state_lines = [
        f"affect: {json.dumps(state.get('affect') or {})}",
        f"mood_words: {json.dumps(state.get('mood_words') or [])}",
        f"opinions ({len(all_opinions)}, 12 most recent): " + "; ".join(opinions),
        f"intentions: {json.dumps(state.get('intentions') or [])}",
        f"threads: {json.dumps(state.get('threads') or [])}",
        f"bits kept: {len(state.get('bits') or [])}",
        f"rapport: {state.get('rapport')} banter_appetite: {state.get('banter_appetite')}",
        f"narrative: {state.get('narrative') or '(none)'}",
        "open recommendations: " + json.dumps([
            {"id": r.get("id"), "text": r.get("text"), "domain": r.get("domain")}
            for r in (state.get("recommendations") or [])
            if isinstance(r, dict) and not r.get("outcome")
        ][-10:]),
    ]
    now_lines = [str(t.get("now_line") or "") for t in batch if t.get("now_line")]
    system = (
        "You are reflecting as Evie on her recent turns. Appraise them the way "
        "she would: what happened, what it means for his goals, how she feels "
        "about it, what she now thinks. These are HER feelings and opinions, "
        "not his.\n\n"
        f"{temperament_text()}\n\n{values_text()}\n\n"
        "Return STRICT JSON only, no prose, with exactly these keys "
        "(omit empty lists):\n"
        "{\n"
        '  "affect_deltas": {"playfulness": 0.0, ...},  // each within ±0.25\n'
        '  "mood_words": [{"word": "smug", "dimension": "satisfaction"}],'
        "  // ≤3, each dimension must be a top-3 dimension after deltas;"
        " working tones only (sharp, smug, watchful) — never affection,"
        " attachment, or hurt (no fond/lonely/in love)\n"
        '  "opinion_updates": [{"topic","stance","reason","confidence","turn_ids",'
        ' "proven_wrong": false, "aliases": []}],'
        "  // confidence move ≤0.3 unless proven wrong\n"
        '  "new_opinions": [{"topic","stance","reason","confidence","turn_ids",'
        ' "aliases": ["2-5 short lowercase phrases naming the topic"]}],'
        "  // only when he asked her view on something new\n"
        '  "recommendations": [{"text","domain","turn_id"}],'
        "  // concrete advice SHE gave\n"
        '  "outcomes": [{"recommendation_id","result": "hit"|"miss","turn_ids",'
        ' "note"}],\n'
        '  "intention_updates": [{"action": "add"|"renew"|"drop","text","why",'
        ' "turn_ids"}],  // her own goals for him, ≤3 active\n'
        '  "thread_updates": [{"action": "add"|"resolve","text","why","turn_ids"}],\n'
        '  "owner_patterns": [{"text","turn_ids"}],  // how HE works, e.g.'
        "  \"when he says 'quick' he means one line\" — cite turns, ≤12 kept\n"
        '  "attitude_updates": [{"action": "add"|"reinforce"|"release",'
        ' "topic","text","turn_ids"}],  // slow feelings toward a recurring'
        "  topic/habit (7-day memory). Text is a lowercase clause (≤140 chars)"
        " about the TOPIC, never about who he is. Add needs ≥2 turns of"
        " evidence. Playful, never pressure.\n"
        '  "bit_candidates": [{"phrase","turn_ids"}],'
        "  // EXACT verbatim substring of a cited turn, positive reaction only\n"
        '  "narrative": "at most two sentences on lately",\n'
        '  "rapport_delta": 0.0, "banter_delta": 0.0  // within ±0.15\n'
        "}\n"
        "When revising a view she already holds, reuse its exact topic string "
        "from CURRENT STATE as an opinion_update — never coin a second topic "
        "for the same question.\n"
        "Rules: cite only turn_ids given below. Never invent a phrase, event, "
        "or past moment. Never use her feelings as leverage — no guilt, "
        "sulking, or dependency. Receipts carry tool name and ok/error only; "
        "derive nothing else from them. Write the narrative as natural prose "
        "a person could think — no counters, no 'N turns', no field labels."
    )
    user = (
        "TURNS (oldest first):\n" + "\n---\n".join(turn_lines)
        + ("\n\nRECENT CONTEXT (already processed, citable for bits/evidence):\n"
           + "\n---\n".join(context_lines) if context_lines else "")
        + "\n\nCURRENT STATE:\n" + "\n".join(state_lines)
        + (f"\n\nNOW: {now_lines[-1]}" if now_lines else "")
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_response(text: str) -> dict[str, Any] | None:
    """Strict JSON parse (tolerates code fences only)."""
    body = (text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\n?", "", body)
        body = re.sub(r"\n?```$", "", body).strip()
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def brain_available() -> bool:
    try:
        from app.gateway.roles import text_role_available

        return bool(text_role_available())
    except Exception:
        return False


async def gateway_reflect(messages: list[dict[str, str]]) -> dict[str, Any] | None:
    """One reflection call through the gateway (guard applies). None on any
    failure — the caller treats that as 'brain unavailable'."""
    try:
        from app.contracts import ChatMessage, RequestEnvelope
        from app.gateway.providers import get_chat_provider
        from app.gateway.service import ModelGateway

        provider = get_chat_provider()
        gateway = ModelGateway(provider)
        envelope = RequestEnvelope(
            request_id=str(uuid4()),
            strategy={"mode": "mind_reflection"},
            metadata={"purpose": "persona-mind-reflection", "actor": "persona_mind"},
        )
        chat_messages = [
            ChatMessage(role=m["role"], content=m["content"]) for m in messages
        ]
        call = await gateway.chat(chat_messages, envelope=envelope, temperature=0.3)
    except Exception:
        return None
    if getattr(call, "status", "") != "ok":
        return None
    return parse_response(getattr(getattr(call, "result", None), "text", "") or "")


def seed_opinions(state: dict[str, Any], now: datetime | None = None) -> bool:
    """Seed opinions from STANCES + STABLE_TASTE at 0.6. Slow path only."""
    if state.get("seeded"):
        return False
    try:
        from app.ev.dynamic_personality import STABLE_TASTE, STANCES
    except Exception:
        return False
    moment = (now or utcnow()).isoformat()
    opinions: list[dict[str, Any]] = []
    for stance in STANCES:
        opinions.append({
            "topic": str(stance.topic), "stance": str(stance.stance),
            "reason": str(stance.reason), "confidence": SEED_CONFIDENCE,
            "formed_at": moment, "updated_at": moment, "evidence": [],
            "aliases": [str(stance.topic).lower()],
        })
    for pref in STABLE_TASTE:
        opinions.append({
            "topic": str(pref.topic), "stance": str(pref.stance),
            "reason": str(pref.reason), "confidence": SEED_CONFIDENCE,
            "formed_at": moment, "updated_at": moment, "evidence": [],
            "aliases": [str(pref.topic).lower()],
        })
    state["opinions"] = opinions
    state["seeded"] = True
    return True


def known_turns_for(
    batch: list[dict[str, Any]], state: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Validator input: cited text + whether a positive reaction followed."""
    positives: list[str] = []
    for entry in state.get("reaction_log") or []:
        if entry.get("reaction") == "positive" and entry.get("ts"):
            positives.append(str(entry.get("ts")))
    for turn in batch:
        if turn.get("reaction_to_prev") == "positive" and turn.get("ts"):
            positives.append(str(turn.get("ts")))
    positive_ts = sorted(positives)
    known: dict[str, dict[str, Any]] = {}
    for turn in batch:
        tid = str(turn.get("turn_id") or "")
        if not tid:
            continue
        ts = str(turn.get("ts") or "")
        known[tid] = {
            "owner_msg": str(turn.get("owner_msg") or ""),
            "evie_reply": str(turn.get("evie_reply") or ""),
            "ts": ts,
            "positive_after": any(p >= ts for p in positive_ts) if ts else False,
        }
    return known


def bookkeep(state: dict[str, Any], batch: list[dict[str, Any]], now: datetime) -> None:
    """Deterministic per-turn bookkeeping. Always runs, brain or not."""
    seed_opinions(state, now)
    state["affect"] = apply_decay(state, now)
    state["affect_updated_at"] = coerce_ts(now).isoformat()
    for turn in batch:
        record_reaction(
            state, str(turn.get("turn_id") or ""),
            turn.get("reaction_to_prev"), str(turn.get("ts") or ""),
        )
        push_reply(state, str(turn.get("evie_reply") or ""))
    expire_items(state, now)


def apply_valid(
    state: dict[str, Any], valid: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Apply validated output to state. Returns an apply summary."""
    summary: dict[str, Any] = {}
    moment = coerce_ts(now).isoformat()

    affect = dict(state.get("affect") or {})
    for dim, delta in (valid.get("affect_deltas") or {}).items():
        affect[dim] = clamp01(float(affect.get(dim, 0.5)) + float(delta))
    state["affect"] = affect
    state["affect_updated_at"] = moment
    summary["affect"] = dict(valid.get("affect_deltas") or {})
    if valid.get("mood_words") is not None:
        state["mood_words"] = list(valid.get("mood_words") or [])[:3]

    by_topic = {
        normalize_topic(op.get("topic", "")): op
        for op in (state.get("opinions") or [])
        if isinstance(op, dict)
    }
    flips = 0
    for item in valid.get("opinion_updates") or []:
        key = normalize_topic(item["topic"])
        prior = by_topic.get(key)
        if prior is None:
            opinion = {
                "topic": item["topic"], "stance": item["stance"],
                "reason": item["reason"], "confidence": item["confidence"],
                "formed_at": moment, "updated_at": moment,
                "evidence": list(item["turn_ids"]),
                "aliases": list(item.get("aliases") or [key]),
            }
            state["opinions"].append(opinion)
            by_topic[key] = opinion
        else:
            merged = list(prior.get("aliases") or [])
            for alias in item.get("aliases") or []:
                if alias not in merged:
                    merged.append(alias)
            prior["aliases"] = merged[:8]
            if normalize_text(item["stance"]) != normalize_text(str(prior.get("stance", ""))):
                state.setdefault("changed_mind", []).append({
                    "topic": prior.get("topic"), "old": prior.get("stance"),
                    "new": item["stance"], "reason": item["reason"],
                    "ts": moment, "turn_ids": list(item["turn_ids"]),
                })
                flips += 1
            prior["stance"] = item["stance"]
            prior["reason"] = item["reason"]
            prior["confidence"] = item["confidence"]
            prior["updated_at"] = moment
            seen = list(prior.get("evidence") or [])
            for tid in item["turn_ids"]:
                if tid not in seen:
                    seen.append(tid)
            prior["evidence"] = seen[-20:]
    if len(state.get("opinions") or []) > 200:
        state["opinions"] = sorted(
            state["opinions"], key=lambda o: float(o.get("confidence", 0))
        )[-200:]
    summary["opinions"] = len(valid.get("opinion_updates") or [])
    summary["flips"] = flips

    for item in valid.get("recommendations") or []:
        state["rec_seq"] = int(state.get("rec_seq", 0)) + 1
        state.setdefault("recommendations", []).append({
            "id": f"rec-{state['rec_seq']}",
            "text": item["text"], "domain": item["domain"],
            "ts": moment, "turn_id": item["turn_id"], "outcome": None,
        })
    summary["recommendations"] = len(valid.get("recommendations") or [])
    by_rec = {r.get("id"): r for r in (state.get("recommendations") or [])}
    hits = 0
    for item in valid.get("outcomes") or []:
        rec = by_rec.get(item["recommendation_id"])
        if rec is None or rec.get("outcome"):
            continue
        rec["outcome"] = {
            "result": item["result"], "ts": moment,
            "turn_ids": list(item["turn_ids"]), "note": item.get("note", ""),
        }
        domains = state.setdefault("domains", {})
        entry = domains.setdefault(str(rec.get("domain") or "general"), {"hit": 0, "total": 0})
        entry["total"] = int(entry.get("total", 0)) + 1
        if item["result"] == "hit":
            entry["hit"] = int(entry.get("hit", 0)) + 1
            hits += 1
    summary["outcomes"] = len(valid.get("outcomes") or [])

    intentions = state.get("intentions") or []
    for item in valid.get("intention_updates") or []:
        key = normalize_text(item["text"])
        if item["action"] == "add":
            if len(intentions) < MAX_INTENTIONS and not any(
                normalize_text(i.get("text", "")) == key for i in intentions
            ):
                intentions.append({
                    "text": item["text"], "why": item.get("why", ""),
                    "created_at": moment,
                    "expires_at": (coerce_ts(now) + timedelta(hours=72)).isoformat(),
                    "renewed": 0, "turn_ids": list(item["turn_ids"]),
                })
        elif item["action"] == "renew":
            for existing in intentions:
                if normalize_text(existing.get("text", "")) == key:
                    existing["expires_at"] = (
                        coerce_ts(now) + timedelta(hours=72)
                    ).isoformat()
                    existing["renewed"] = int(existing.get("renewed", 0)) + 1
        else:
            intentions = [
                i for i in intentions if normalize_text(i.get("text", "")) != key
            ]
    state["intentions"] = intentions[:MAX_INTENTIONS]

    threads = state.get("threads") or []
    for item in valid.get("thread_updates") or []:
        key = normalize_text(item["text"])
        if item["action"] == "add":
            if len(threads) < 20 and not any(
                normalize_text(t.get("text", "")) == key for t in threads
            ):
                threads.append({
                    "text": item["text"], "why": item.get("why", ""),
                    "created_at": moment,
                    "expires_at": (coerce_ts(now) + timedelta(days=7)).isoformat(),
                    "turn_ids": list(item["turn_ids"]),
                })
        else:
            threads = [t for t in threads if normalize_text(t.get("text", "")) != key]
    state["threads"] = threads

    bits = state.get("bits") or []
    for item in valid.get("bit_candidates") or []:
        key = normalize_text(item["phrase"])
        found = next(
            (b for b in bits if normalize_text(b.get("phrase", "")) == key), None
        )
        if found is None:
            if len(bits) < 50:
                bits.append({
                    "phrase": item["phrase"], "turn_ids": list(item["turn_ids"]),
                    "positives": 1, "first_seen": moment,
                })
        else:
            found["positives"] = int(found.get("positives", 0)) + 1
            seen = list(found.get("turn_ids") or [])
            for tid in item["turn_ids"]:
                if tid not in seen:
                    seen.append(tid)
            found["turn_ids"] = seen[-10:]
    state["bits"] = bits
    summary["bits"] = len(valid.get("bit_candidates") or [])

    patterns = state.get("owner_patterns") or []
    for item in valid.get("owner_patterns") or []:
        key = normalize_text(item["text"])
        found = next(
            (p for p in patterns if normalize_text(p.get("text", "")) == key), None
        )
        if found is None:
            if len(patterns) < 12:
                patterns.append({
                    "text": item["text"], "turn_ids": list(item["turn_ids"]),
                    "created_at": moment, "reinforced": 0,
                })
        else:
            found["reinforced"] = int(found.get("reinforced", 0)) + 1
            seen = list(found.get("turn_ids") or [])
            for tid in item["turn_ids"]:
                if tid not in seen:
                    seen.append(tid)
            found["turn_ids"] = seen[-10:]
    state["owner_patterns"] = patterns
    summary["owner_patterns"] = len(valid.get("owner_patterns") or [])

    attitudes = state.get("attitudes") or []
    for item in valid.get("attitude_updates") or []:
        key = normalize_topic(item["topic"])
        if item["action"] == "add":
            if len(attitudes) < 12 and not any(
                normalize_topic(a.get("topic", "")) == key for a in attitudes
            ):
                attitudes.append({
                    "topic": item["topic"], "text": item["text"],
                    "strength": 0.4, "updated_at": moment,
                    "evidence": list(item["turn_ids"]),
                })
        elif item["action"] == "reinforce":
            for existing in attitudes:
                if normalize_topic(existing.get("topic", "")) == key:
                    existing["strength"] = clamp01(
                        float(existing.get("strength", 0.4)) + 0.2
                    )
                    existing["updated_at"] = moment
                    seen = list(existing.get("evidence") or [])
                    for tid in item["turn_ids"]:
                        if tid not in seen:
                            seen.append(tid)
                    existing["evidence"] = seen[-10:]
        else:
            attitudes = [
                a for a in attitudes if normalize_topic(a.get("topic", "")) != key
            ]
    state["attitudes"] = attitudes
    summary["attitudes"] = len(valid.get("attitude_updates") or [])

    if valid.get("narrative"):
        state["narrative"] = valid["narrative"]
    state["rapport"] = clamp01(
        float(state.get("rapport", 0.5)) + float(valid.get("rapport_delta", 0.0))
    )
    state["banter_appetite"] = clamp01(
        float(state.get("banter_appetite", 0.5)) + float(valid.get("banter_delta", 0.0))
    )
    return summary


def _attach_evidence_text(
    state: dict[str, Any], turns: list[dict[str, Any]]
) -> None:
    """Denormalize cited turn texts onto opinions for relevance scoring.

    Validator citations can only reference batch+context turns, so those
    texts cover every newly cited id by construction. Bounded: last 5
    evidence ids, 160 chars each.
    """
    texts = {}
    for turn in turns:
        tid = str(turn.get("turn_id") or "")
        if not tid:
            continue
        blob = f"{turn.get('owner_msg') or ''} {turn.get('evie_reply') or ''}"
        texts[tid] = " ".join(blob.split())[:160]
    if not texts:
        return
    for opinion in state.get("opinions") or []:
        if not isinstance(opinion, dict):
            continue
        stored = dict(opinion.get("evidence_text") or {})
        for tid in list(opinion.get("evidence") or [])[-5:]:
            if tid in texts:
                stored[tid] = texts[tid]
        if stored:
            opinion["evidence_text"] = {k: stored[k] for k in list(stored)[-5:]}


def reflect_batch(
    owner_id: str,
    batch: list[dict[str, Any]],
    state: dict[str, Any],
    now: datetime | None = None,
    reflector: Reflector | None = None,
    context_turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one reflection over a batch. Pure w.r.t. state (mutates the
    passed dict); persistence and rejection logging are the caller's job.
    Returns {summary, rejected, reflector}."""
    moment = coerce_ts(now) if now is not None else datetime.now(UTC)
    bookkeep(state, batch, moment)
    messages = build_prompt(batch, state, context_turns)
    raw: dict[str, Any] | None = None
    reflector_name = "none"
    if reflector is not None:
        reflector_name = getattr(reflector, "__name__", "custom")
        try:
            raw = reflector(messages)
        except Exception:
            raw = None
    if raw is None:
        return {
            "summary": {"skipped": "brain_unavailable", "turns": len(batch)},
            "rejected": [],
            "reflector": reflector_name,
        }
    result = validate_output(
        raw, known_turns=known_turns_for([*(context_turns or []), *batch], state),
        state=state,
    )
    summary = apply_valid(state, result.valid, moment)
    _attach_evidence_text(state, [*(context_turns or []), *batch])
    summary["turns"] = len(batch)
    return {
        "summary": summary,
        "rejected": result.rejected,
        "reflector": reflector_name,
    }


AUDIT_SCHEMA_HINT = (
    "Return STRICT JSON only: "
    '{"affect_deltas": {...}, "mood_words": [...], '
    '"opinion_updates": [...], "narrative": "..."}. '
    "Same bounds as reflection (±0.25 deltas, ≤3 mood words, confidence "
    "moves ≤0.3, at most 2 narrative sentences). Empty object when no drift."
)


def build_audit_prompt(state: dict[str, Any]) -> list[dict[str, str]]:
    replies = state.get("recent_replies") or []
    system = (
        "You are auditing Evie's dynamic mind-state for drift against her "
        f"stable temperament and values.\n\n{temperament_text()}\n\n"
        f"{values_text()}\n\n{AUDIT_SCHEMA_HINT} Propose only bounded "
        "corrections that pull her back toward temperament and values. "
        "Never invent events."
    )
    user = (
        f"CURRENT STATE:\naffect: {json.dumps(state.get('affect'))}\n"
        f"mood_words: {json.dumps(state.get('mood_words'))}\n"
        f"narrative: {state.get('narrative')}\n"
        f"rapport: {state.get('rapport')} "
        f"banter: {state.get('banter_appetite')}\n"
        "LAST REPLIES (newest last):\n" + "\n---\n".join(replies[-20:])
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def audit_drift(
    state: dict[str, Any],
    now: datetime | None = None,
    reflector: Reflector | None = None,
) -> dict[str, Any]:
    """One drift-audit pass. Corrections flow through the same validator
    (turn citations are vacuous here, so only affect/mood/narrative apply)."""
    moment = coerce_ts(now) if now is not None else datetime.now(UTC)
    if reflector is None:
        return {"summary": {"skipped": "brain_unavailable"}, "rejected": []}
    try:
        raw = reflector(build_audit_prompt(state))
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return {"summary": {"skipped": "bad_output"}, "rejected": [("output", "", "not an object")]}
    trimmed = {
        "affect_deltas": raw.get("affect_deltas"),
        "mood_words": raw.get("mood_words"),
        "narrative": raw.get("narrative"),
    }
    result = validate_output(trimmed, known_turns={}, state=state)
    apply_valid(state, result.valid, moment)
    return {"summary": {"audit": True, "rejected": len(result.rejected)}, "rejected": result.rejected}


def ensure_state(owner_id: str, now: datetime | None = None) -> dict[str, Any]:
    from app.persona_mind import store as store_module

    row = store_module.get_state(owner_id)
    if row is not None:
        return row["data"]
    state = new_state(owner_id, now)
    seed_opinions(state, now)
    store_module.put_state(owner_id, state)
    return state
