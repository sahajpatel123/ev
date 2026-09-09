"""Mobile V2 portable-operator contract: pure-function operator checks.

Hermetic by construction: only the pure helpers in
``app.device_gateway.mobile_v2`` are exercised. No DB, no network,
no production logic reimplemented here — every assertion calls the
real function and checks its observable contract outcome.
"""

from __future__ import annotations

from app.device_gateway.mobile_v2 import (
    ActionResult,
    Interference,
    RouteTarget,
    classify_interference,
    is_stop_request,
    map_broker_status,
    map_offline_state,
    map_phone_mac_status,
    owner_reply_for,
    phone_brain_allowed,
    phone_brain_is_legacy,
    should_notify,
    resolve_route_target,
)


# --- resolve_route_target: one example per directive lane ---


def test_route_phone_local() -> None:
    assert resolve_route_target("where am i right now") == RouteTarget.PHONE_LOCAL


def test_route_core() -> None:
    assert resolve_route_target("what are my commitments today") == RouteTarget.CORE


def test_route_cloud() -> None:
    assert resolve_route_target("research quantum batteries for me") == RouteTarget.CLOUD


def test_route_home_station() -> None:
    assert resolve_route_target("check my mail") == RouteTarget.HOME_STATION


def test_route_multi_device() -> None:
    assert resolve_route_target("send this photo to my mac") == RouteTarget.MULTI_DEVICE


def test_route_geofence_reminder_is_multi_device() -> None:
    assert (
        resolve_route_target("remind me when I arrive at home")
        == RouteTarget.MULTI_DEVICE
    )


def test_route_empty_defaults_core() -> None:
    assert resolve_route_target("") == RouteTarget.CORE


# --- classify_interference: quiet default, explicit foreground ---


def test_interference_defaults_non_disruptive() -> None:
    assert (
        classify_interference("what are my commitments today")
        == Interference.NON_DISRUPTIVE
    )
    assert classify_interference("research quantum batteries") == Interference.NON_DISRUPTIVE


def test_interference_foreground_capability() -> None:
    assert (
        classify_interference("anything", capability="screen_look")
        == Interference.FOREGROUND_REQUIRED
    )


def test_interference_foreground_scoped_out_when_not_explicit() -> None:
    assert (
        classify_interference(
            "anything", capability="screen_look", explicit_foreground_requested=False
        )
        == Interference.NON_DISRUPTIVE
    )


def test_interference_explicit_owner_ask() -> None:
    assert (
        classify_interference("show me my mac") == Interference.FOREGROUND_REQUIRED
    )
    assert (
        classify_interference("anything", explicit_foreground_requested=True)
        == Interference.FOREGROUND_REQUIRED
    )


# --- is_stop_request ---


def test_stop_requests() -> None:
    assert is_stop_request("stop")
    assert is_stop_request("stop evie")
    assert is_stop_request("cancel that")
    assert is_stop_request("abort")


def test_non_stop_requests() -> None:
    assert not is_stop_request("what are my commitments today")
    assert not is_stop_request("please don't stop trying")
    assert not is_stop_request("")


# --- honest result mapping ---


def test_broker_mapping() -> None:
    assert map_broker_status("SUCCEEDED") == ActionResult.COMPLETED
    assert map_broker_status("EXECUTING") == ActionResult.PARTIAL
    assert map_broker_status("ROUTED") == ActionResult.PARTIAL
    assert map_broker_status("REQUESTED") == ActionResult.PARTIAL
    assert map_broker_status("QUEUED") == ActionResult.QUEUED
    assert map_broker_status("CANCELLED") == ActionResult.BLOCKED
    assert map_broker_status("FAILED") == ActionResult.FAILED
    assert map_broker_status("EXPIRED") == ActionResult.FAILED
    assert map_broker_status("bogus") == ActionResult.FAILED


def test_phone_mac_mapping() -> None:
    assert map_phone_mac_status("COMPLETED") == ActionResult.COMPLETED
    assert map_phone_mac_status("ACCEPTED") == ActionResult.PARTIAL
    assert map_phone_mac_status("QUEUED") == ActionResult.QUEUED
    assert map_phone_mac_status("FAILED") == ActionResult.FAILED
    assert map_phone_mac_status("bogus") == ActionResult.FAILED


def test_offline_mapping() -> None:
    assert map_offline_state("pending") == ActionResult.QUEUED
    assert map_offline_state("accepted") == ActionResult.QUEUED
    assert map_offline_state("executed") == ActionResult.COMPLETED
    assert map_offline_state("failed") == ActionResult.FAILED
    assert map_offline_state("expired") == ActionResult.FAILED
    assert map_offline_state("rejected") == ActionResult.BLOCKED
    assert map_offline_state("bogus") == ActionResult.FAILED


# --- owner_reply_for: QUEUED/OFFLINE never claim completion ---


def test_owner_reply_completed_claims_done() -> None:
    assert owner_reply_for(ActionResult.COMPLETED) == "Done."


def test_owner_reply_queued_never_claims_done() -> None:
    reply = owner_reply_for(ActionResult.QUEUED)
    assert reply != "Done."
    assert "not done yet" in reply.lower()


def test_owner_reply_offline_never_claims_ran() -> None:
    reply = owner_reply_for(ActionResult.DEVICE_OFFLINE)
    assert reply != "Done."
    lowered = reply.lower()
    assert "offline" in lowered
    assert "did not" in lowered


def test_owner_reply_underway_for_partial() -> None:
    reply = owner_reply_for(ActionResult.PARTIAL)
    assert reply != "Done."
    assert "underway" in reply.lower()


# --- should_notify: attention-worthy vs internal ---


def test_should_notify_yes() -> None:
    assert should_notify("task_finished")
    assert should_notify("approval_required")
    assert should_notify("deadline")
    assert should_notify("queued_task_completed")


def test_should_notify_no() -> None:
    assert not should_notify("internal_retry")
    assert not should_notify("sync_ok")
    assert not should_notify("telemetry")
    assert not should_notify("routine_sync")


def test_should_notify_unknown_stays_quiet() -> None:
    assert not should_notify("something_unheard_of")


# --- Muse enforcement: legacy brains never auto ---


def test_legacy_brains_blocked() -> None:
    for provider in (
        "openai_realtime",
        "gpt-4o-transcribe",
        "grok",
        "luna",
        "deepseek",
    ):
        assert not phone_brain_allowed(provider), provider
        assert phone_brain_is_legacy(provider), provider


def test_muse_and_core_brains_allowed() -> None:
    assert phone_brain_allowed("muse_voice")
    assert phone_brain_allowed("meta_muse_voice")
    assert phone_brain_allowed("muse_spark")
    assert phone_brain_allowed("core")
    for provider in ("muse_voice", "core"):
        assert not phone_brain_is_legacy(provider), provider
