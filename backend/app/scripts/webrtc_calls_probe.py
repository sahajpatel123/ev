"""Probe OpenAI Realtime WebRTC signaling encodings. Never prints secrets."""

from __future__ import annotations

import hashlib
import json
import re
import sys

import httpx

from app.config import settings
from app.device_gateway.webrtc_live import (
    OPENAI_CALLS,
    OPENAI_CLIENT_SECRETS,
    phone_webrtc_session,
)

FAKE_SDP = (
    "v=0\r\n"
    "o=- 3902424242 2 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0 1\r\n"
    "a=extmap-allow-mixed\r\n"
    "a=msid-semantic: WMS\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111 63\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtcp:9 IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:AbCd\r\n"
    "a=ice-pwd:abcdefghijklmnopqrstuvwx\r\n"
    "a=ice-options:trickle\r\n"
    "a=fingerprint:sha-256 "
    "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99\r\n"
    "a=setup:actpass\r\n"
    "a=mid:0\r\n"
    "a=sendrecv\r\n"
    "a=rtcp-mux\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:AbCd\r\n"
    "a=ice-pwd:abcdefghijklmnopqrstuvwx\r\n"
    "a=ice-options:trickle\r\n"
    "a=fingerprint:sha-256 "
    "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99\r\n"
    "a=setup:actpass\r\n"
    "a=mid:1\r\n"
    "a=sctp-port:5000\r\n"
    "a=max-message-size:262144\r\n"
)


def redact(text: str, key: str, ephemeral: str = "") -> str:
    blob = text or ""
    if key:
        blob = blob.replace(key, "<SERVER_KEY>")
    if ephemeral:
        blob = blob.replace(ephemeral, "<EK>")
    blob = re.sub(r"ek_[A-Za-z0-9]+", "ek_<redacted>", blob)
    blob = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-<redacted>", blob)
    return blob[:1500]


def summarize(resp: httpx.Response) -> dict:
    body = resp.text or ""
    return {
        "status": resp.status_code,
        "ctype": resp.headers.get("content-type"),
        "location": resp.headers.get("location"),
        "request_id": resp.headers.get("x-request-id") or resp.headers.get("openai-request-id"),
        "bytes": len(resp.content),
        "starts_v": body.lstrip().startswith("v="),
        "starts_json": body.lstrip().startswith("{"),
        "body": body[:800],
    }


def main() -> int:
    key = (settings.openai_api_key or "").strip()
    if not key:
        print("NO_OPENAI_KEY")
        return 2
    sess = phone_webrtc_session()
    print("model", sess.get("model"))
    print("type", sess.get("type"))
    print("sdp_sha", hashlib.sha256(FAKE_SDP.encode()).hexdigest()[:16])
    headers_json = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "OpenAI-Safety-Identifier": "evie-webrtc-probe",
    }
    with httpx.Client(timeout=25.0) as client:
        mint = client.post(
            OPENAI_CLIENT_SECRETS,
            headers=headers_json,
            json={"expires_after": {"anchor": "created_at", "seconds": 60}, "session": sess},
        )
        print("\n=== mint full session ===")
        print(json.dumps({k: summarize(mint)[k] for k in summarize(mint) if k != "body"}, indent=2))
        print(redact(mint.text, key))
        ephemeral = ""
        if mint.status_code < 400:
            payload = mint.json()
            value = payload.get("value")
            if isinstance(value, str) and value.startswith("ek_"):
                ephemeral = value
            elif isinstance(payload.get("client_secret"), dict):
                nested = payload["client_secret"].get("value") or ""
                ephemeral = nested if isinstance(nested, str) else ""
            print("ephemeral_extracted", bool(ephemeral), "prefix", (ephemeral[:3] if ephemeral else ""))

        mint_min = client.post(
            OPENAI_CLIENT_SECRETS,
            headers=headers_json,
            json={
                "session": {
                    "type": "realtime",
                    "model": sess["model"],
                    "audio": {"output": {"voice": sess["audio"]["output"]["voice"]}},
                }
            },
        )
        print("\n=== mint minimal session ===")
        print("status", mint_min.status_code, redact(mint_min.text, key)[:400])

        attempts = []
        if ephemeral:
            attempts.append(
                (
                    "ephemeral application/sdp",
                    lambda: client.post(
                        OPENAI_CALLS,
                        headers={"Authorization": f"Bearer {ephemeral}", "Content-Type": "application/sdp"},
                        content=FAKE_SDP.encode("utf-8"),
                    ),
                )
            )
            attempts.append(
                (
                    "ephemeral multipart sdp only",
                    lambda: client.post(
                        OPENAI_CALLS,
                        headers={"Authorization": f"Bearer {ephemeral}"},
                        files={"sdp": ("offer.sdp", FAKE_SDP, "application/sdp")},
                    ),
                )
            )
        attempts.append(
            (
                "server multipart sdp+session (unified)",
                lambda: client.post(
                    OPENAI_CALLS,
                    headers={"Authorization": f"Bearer {key}", "OpenAI-Safety-Identifier": "evie-webrtc-probe"},
                    files={
                        "sdp": ("offer.sdp", FAKE_SDP, "application/sdp"),
                        "session": ("session.json", json.dumps(sess), "application/json"),
                    },
                ),
            )
        )
        attempts.append(
            (
                "server application/sdp no session",
                lambda: client.post(
                    OPENAI_CALLS,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/sdp"},
                    content=FAKE_SDP.encode("utf-8"),
                ),
            )
        )
        for name, fn in attempts:
            resp = fn()
            print(f"\n=== calls {name} ===")
            info = summarize(resp)
            print(json.dumps({k: v for k, v in info.items() if k != "body"}, indent=2))
            print(redact(info["body"], key, ephemeral))
    return 0


if __name__ == "__main__":
    sys.exit(main())
