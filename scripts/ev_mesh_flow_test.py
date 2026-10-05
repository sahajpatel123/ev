"""End-to-end flow test for the Evie Mesh, run against an isolated server.

Exercises: device creation -> heartbeats -> BLE-style proximity observe ->
mesh advertise -> proximity alias resolution -> presence HUD battery ->
converge broadcast -> photo capture targeting -> intent poll/ack receipts ->
nudge escalation order -> sensor routing -> Mac verb routing -> same-name
HUD dedupe -> device-identity security gate.

Usage: EV_FLOW_URL=http://127.0.0.1:18123 EV_FLOW_MASTER=<key> python ev_flow.py
Exit 0 = all assertions passed.
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.environ.get("EV_FLOW_URL", "http://127.0.0.1:18123").rstrip("/")
MASTER = os.environ.get("EV_FLOW_MASTER", "flow-master-key-1234567890abcdef")

passed: list[str] = []


def ok(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    passed.append(name)
    print(f"  ok: {name}")


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_device(c: httpx.Client, name: str, caps: list[str], device_type: str) -> tuple[str, str]:
    r = c.post(
        f"{BASE}/v1/devices",
        headers=headers(MASTER),
        json={"name": name, "capabilities": caps, "device_type": device_type, "platform": "apple"},
    )
    r.raise_for_status()
    body = r.json()
    return body["device"]["id"], body["token"]


def main() -> None:
    c = httpx.Client(timeout=20.0)

    # --- readiness ---------------------------------------------------------
    ready = c.get(f"{BASE}/v1/health")
    ok("server ready", ready.status_code == 200)

    # --- paired devices ----------------------------------------------------
    a_id, a_token = create_device(c, "flow-mac", ["computer_control", "mesh", "text"], "mac")
    b_id, b_token = create_device(c, "flow-iphone", ["camera", "sensor", "notification", "mesh", "text"], "phone")
    ok("devices paired", bool(a_id) and bool(b_id) and a_id != b_id)

    # --- presence via heartbeat -------------------------------------------
    for token, dev in ((a_token, a_id), (b_token, b_id)):
        r = c.post(
            f"{BASE}/v1/runtime/heartbeat",
            headers=headers(token),
            json={"device_id": dev, "status": "ok", "listener_state": "listening"},
        )
        ok(f"heartbeat {dev[:8]}", r.status_code == 201, r.text[:200])

    # --- BLE proximity + advertisement ------------------------------------
    short_b = b_id[:8]
    r = c.post(
        f"{BASE}/v1/everywhere/proximity/observe",
        headers=headers(a_token),
        json={"observations": [{"subject_device_id": short_b, "rssi": -52.0}]},
    )
    ok("observe short id", r.status_code == 200, r.text[:200])
    ok("zone immediate", r.json()["recorded"][0]["zone"] == "immediate")
    r = c.post(
        f"{BASE}/v1/everywhere/mesh/advertise",
        headers=headers(b_token),
        json={"battery_percent": 87.0, "low_power": False, "capabilities": ["mesh"]},
    )
    ok("advertise", r.status_code == 200 and r.json()["advertisement"]["battery_percent"] == 87.0)

    # --- proximity alias resolution (short id -> full registry UUID) ------
    r = c.get(f"{BASE}/v1/everywhere/proximity", headers=headers(a_token))
    ranks = r.json().get("proximity_ranks", {})
    ok("alias resolves full uuid", ranks.get(b_id) == 0, f"ranks={ranks}")
    ok("zones count > 0", sum(r.json().get("zones", {}).values()) >= 1)

    # --- presence HUD: battery + dedupe source data ------------------------
    r = c.get(f"{BASE}/v1/everywhere/presence/hud", headers=headers(a_token))
    hud = r.json()
    rows = [d for d in hud["devices"] if d["device_id"] == b_id]
    ok("hud has phone", len(rows) == 1)
    ok("hud battery from advertisement", rows[0]["battery_percent"] == 87.0, str(rows[0]))

    # --- converge broadcast ------------------------------------------------
    r = c.post(f"{BASE}/v1/everywhere/converge", headers=headers(a_token), json={"reason": "flow"})
    conv = r.json()
    ok("converge targets both", conv["target_count"] >= 2 and b_id in conv["targets"], str(conv)[:200])
    conv_id = conv["intent_id"]

    # --- phone polls and acks the broadcast --------------------------------
    r = c.get(f"{BASE}/v1/everywhere/intents", headers=headers(b_token), params={"status": "PENDING"})
    ids = [i["id"] for i in r.json()["intents"]]
    ok("phone sees converge", conv_id in ids, str(ids)[:200])
    r = c.post(
        f"{BASE}/v1/everywhere/intents/{conv_id}/ack",
        headers=headers(b_token),
        json={"status": "DONE", "note": "flow: converged"},
    )
    ok("phone acks converge", r.status_code == 200, r.text[:200])

    # --- photo capture targets the camera device ---------------------------
    r = c.post(f"{BASE}/v1/everywhere/photo/capture", headers=headers(a_token), json={"reason": "flow"})
    photo = r.json()
    ok("photo targets phone", photo["target_device_id"] == b_id, str(photo)[:200])
    r = c.get(f"{BASE}/v1/everywhere/intents", headers=headers(b_token), params={"status": "PENDING"})
    pending = {i["id"]: i for i in r.json()["intents"]}
    ok("phone sees photo", photo["intent_id"] in pending)
    ok(
        "photo carries requester provenance",
        pending[photo["intent_id"]]["args"].get("requester_device_id") == a_id,
        str(pending[photo["intent_id"]]["args"])[:200],
    )
    r = c.post(
        f"{BASE}/v1/everywhere/intents/{photo['intent_id']}/ack",
        headers=headers(b_token),
        json={"status": "DONE", "note": "flow: captured"},
    )
    ok("phone acks photo", r.status_code == 200)
    r = c.get(f"{BASE}/v1/everywhere/intents", headers=headers(a_token))
    receipts = {i["id"]: i.get("receipts") for i in r.json()["intents"]}
    ok(
        "receipt recorded",
        any(
            rec.get("status") == "DONE"
            for rec in (receipts.get(photo["intent_id"]) or [])
        ),
        str(receipts.get(photo["intent_id"]))[:200],
    )

    # --- nudge escalation: nearest first -----------------------------------
    r = c.post(
        f"{BASE}/v1/everywhere/nudge/escalate",
        headers=headers(a_token),
        json={"title": "flow", "body": "escalation"},
    )
    esc = r.json()
    ok("nudge nearest first", esc["first_target_device_id"] == b_id, str(esc)[:200])

    # --- sensor read routes to the sensor device ---------------------------
    r = c.post(
        f"{BASE}/v1/everywhere/sensor/read",
        headers=headers(a_token),
        json={"sensor": "battery"},
    )
    sens = r.json()
    ok("sensor risk R1", sens["risk"] == "R1")
    ok("sensor targets phone", sens["target_device_id"] == b_id, str(sens)[:200])

    # --- Mac verb from the phone routes to the Mac, R1 auto -----------------
    r = c.post(
        f"{BASE}/v1/everywhere/mac/verb",
        headers=headers(b_token),
        json={"verb": "status", "arguments": {}},
    )
    verb = r.json()
    ok("verb targets mac", verb["target_device_id"] == a_id, str(verb)[:200])
    ok("R1 verb no approval prompt", verb["risk"] == "R1" and verb["approval_required"] is False)

    # --- same-name duplicate collapses in the HUD (registry fix) -----------
    dup_id, dup_token = create_device(c, "flow-mac", ["mesh"], "mac")
    r = c.post(
        f"{BASE}/v1/runtime/heartbeat",
        headers=headers(dup_token),
        json={"device_id": dup_id, "status": "ok", "listener_state": "listening"},
    )
    ok("duplicate paired", r.status_code == 201)
    r = c.get(f"{BASE}/v1/everywhere/presence/hud", headers=headers(a_token))
    macs = [d for d in r.json()["devices"] if d["display_name"] == "flow-mac"]
    ok("hud collapses same-name rows", len(macs) == 1, f"got {len(macs)}")

    # --- security: master key cannot act as a device -----------------------
    r = c.post(
        f"{BASE}/v1/everywhere/proximity/observe",
        headers=headers(MASTER),
        json={"observations": [{"subject_device_id": short_b, "rssi": -50.0}]},
    )
    ok("master key is not a device", r.status_code == 401, r.text[:200])

    print(f"\nFLOW PASS: {len(passed)} checks")


if __name__ == "__main__":
    main()
