"""Tailnet policy invariants: phones reach home:443 only, nothing else."""

from __future__ import annotations

import json
from pathlib import Path

POLICY = Path(__file__).resolve().parents[2] / "tailscale" / "tailnet-policy.json"


def _load() -> dict:
    return json.loads(POLICY.read_text())


def test_policy_is_valid_json_with_both_tags() -> None:
    policy = _load()
    owners = policy["tagOwners"]
    assert "tag:evie-home" in owners
    assert "tag:evie-phone" in owners


def test_every_accept_is_phones_to_home_443_only() -> None:
    policy = _load()
    acls = policy["acls"]
    assert len(acls) >= 1, "policy must contain at least the phone->home rule"
    for rule in acls:
        assert rule["action"] == "accept"
        assert rule["src"] == ["tag:evie-phone"], rule
        for dst in rule["dst"]:
            tag, _, port = dst.rpartition(":")
            assert tag == "tag:evie-home", rule
            assert port == "443", rule
    blob = json.dumps(policy)
    assert "*:*" not in blob
    assert "autogroup:shared" not in blob


def test_no_ssh_no_funnel_no_extra_surface() -> None:
    policy = _load()
    assert "ssh" not in policy, "Tailscale SSH stays disabled by omission"
    blob = json.dumps(policy).lower()
    assert "funnel" not in blob
    assert "5432" not in blob and "6379" not in blob and "8000" not in blob
