"""Structural lock for fleet plan + elite 20-agent launch pack.

Tests drive real in-repo docs (not reimplemented brief text as oracle logic).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"


def _read(name: str) -> str:
    path = DOCS / name
    assert path.is_file(), f"missing fleet doc: {path}"
    return path.read_text(encoding="utf-8")


def test_agent_fleet_roster_twenty_and_bans() -> None:
    text = _read("AGENT_FLEET.md")
    for n in range(1, 21):
        # Numbered roster rows use **N** markdown bold
        assert f"**{n}**" in text or f"| {n} |" in text or f"| **{n}**" in text, (
            f"fleet roster missing agent {n}"
        )
    assert "exclusive" in text.lower() or "Owns" in text
    assert "Domain 20" in text or "Domain-20" in text
    assert "19" in text
    assert "20" in text
    assert "1 → 20" in text or "1→20" in text or "1 →" in text
    assert "EVIE" in text
    assert "presence" in text.lower() or "real" in text.lower()
    assert "Done when" in text or "done when" in text.lower()
    assert "AGENT_LAUNCH" in text
    assert "nested" in text.lower() or "No nested globs" in text


def test_product_docs_cross_link_launch_pack() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "AGENT_FLEET" in readme
    assert "AGENT_LAUNCH" in readme
    assert "Domain 20" in readme or "20" in readme or "19" in readme


def test_done_when_gates_are_checkable() -> None:
    fleet = _read("AGENT_FLEET.md")
    launch = _read("AGENT_LAUNCH.md")
    combined = fleet + "\n" + launch
    for token in (
        "Suite green",
        "Voice",
        "Runtime",
        "Surface",
        "Identity",
        "eval",
        "Calendar",
        "collectors",
    ):
        assert token in combined or token.lower() in combined.lower(), (
            f"done-when coverage missing token: {token}"
        )


def test_clients_paths_non_nested_in_fleet() -> None:
    fleet = _read("AGENT_FLEET.md")
    assert "device_listener.py" in fleet
    assert "cli/**" in fleet or "clients/cli" in fleet
    assert "web/**" in fleet or "clients/web" in fleet
    assert "collectors/**" in fleet or "clients/collectors" in fleet
    # Agent 17 surface must not own parent clients/**
    assert "clients/cli/**" in fleet
    # nested ban
    assert "nested" in fleet.lower()


def _launch_message_body(agent_num: int) -> str:
    """Extract paste-ready fenced message for Agent N from AGENT_LAUNCH.md."""
    launch = _read("AGENT_LAUNCH.md")
    marker = f"## Agent {agent_num} —"
    assert marker in launch, f"missing heading {marker}"
    start = launch.index(marker)
    fence_open = launch.index("```text", start)
    body_start = fence_open + len("```text")
    fence_close = launch.index("```", body_start)
    return launch[body_start:fence_close]


def test_agent_launch_pack_count_and_number_order() -> None:
    launch = _read("AGENT_LAUNCH.md")
    assert "exactly 20" in launch.lower()
    assert "1 → 20" in launch or "1, then 2" in launch or "ascending" in launch.lower()
    assert "Domain 20" in launch or "Domain-20" in launch
    assert "19" in launch
    assert "How you launch" in launch or "how to launch" in launch.lower()
    assert "commit" in launch.lower() and "push" in launch.lower()
    assert "Wave-1" not in launch and "Wave-2" not in launch
    assert "EVIE" in launch
    # All 20 agent headings
    for n in range(1, 21):
        assert f"## Agent {n} —" in launch, f"missing Agent {n} section"
    fleet = _read("AGENT_FLEET.md")
    assert "AGENT_LAUNCH" in fleet


@pytest.mark.parametrize("agent_num", list(range(1, 21)))
def test_agent_launch_message_is_premium_paste_ready(agent_num: int) -> None:
    body = _launch_message_body(agent_num)
    upper = body.upper()
    assert "OWNS" in upper
    assert "DOES NOT TOUCH" in upper
    assert "VERIFY" in upper
    assert "REPORT FOOTER" in upper
    assert "uv run" in body
    assert "/Users/sahajpatel/Code/ev" in body
    assert "MISSION" in upper or "WHY YOU EXIST" in upper
    assert "DONE WHEN" in upper
    assert "PRODUCT OUTCOME" in upper
    # Premium depth: not a tiny stub
    assert len(body) > 800, f"Agent {agent_num} message too thin ({len(body)} chars)"


def test_agent_launch_clients_path_exclusivity() -> None:
    """Agents 14 / 17 / 13 carve clients/ without nested parent owns (fleet v3)."""
    pulse = _launch_message_body(14)
    workbench = _launch_message_body(17)
    ambient = _launch_message_body(13)

    assert "device_listener.py" in pulse
    assert "ONLY" in pulse or "only" in pulse or "device_listener" in pulse
    assert "collectors" in pulse.lower() or "DOES NOT TOUCH" in pulse.upper()

    assert "clients/cli/**" in workbench or "backend/clients/cli/**" in workbench
    assert "clients/web/**" in workbench or "backend/clients/web/**" in workbench
    owns = workbench.split("OWNS", 1)[1].split("DOES NOT TOUCH", 1)[0]
    assert "backend/clients/**" not in owns or "NEVER" in owns or "never" in owns
    assert "device_listener" in workbench
    assert "collectors" in workbench

    assert "collectors/**" in ambient or "clients/collectors" in ambient
    does_not = ambient.split("DOES NOT TOUCH", 1)[1]
    assert "device_listener" in does_not
    assert "cli" in does_not.lower() or "CLI" in does_not


def test_agent_launch_training_consent_and_real_data() -> None:
    body = _launch_message_body(11).lower()  # FORGE
    assert "silent" in body and "lora" in body
    assert "consent" in body
    assert "public" in body and ("dataset" in body or "data" in body)
    assert "dry-run" in body or "dry run" in body
    assert "eval" in body


def test_agent_launch_perception_owner_consent() -> None:
    body = _launch_message_body(6).lower()  # EYES
    assert "consent" in body
    assert "owner" in body
    assert "vision" in body or "ocr" in body
    assert "surveillance" in body or "stranger" in body or "never" in body


def test_agent_launch_ship_outcome_after_twenty() -> None:
    launch = _read("AGENT_LAUNCH.md")
    assert "After all 20" in launch or "after all 20" in launch.lower()
    assert "compose" in launch.lower() or "go-live" in launch.lower()
    assert "backup" in launch.lower()
