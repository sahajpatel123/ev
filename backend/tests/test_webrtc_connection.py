"""WebRTC connection establishment: unified /v1/realtime/calls encoding and stages."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.device_gateway.webrtc_live import (
    SIGNALING_IMPLEMENTATION,
    SIGNALING_VERSION,
    create_realtime_call,
    prepare_offer_sdp,
    public_audio_status,
    summarize_sdp,
    unified_call_parts,
)

ROOT = Path(__file__).resolve().parents[1]
PWA = ROOT / "clients" / "pwa"
OFFER = (
    "v=0\r\n"
    "o=- 1 2 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0 1\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:AbCd\r\n"
    "a=ice-pwd:abcdefghijklmnopqrstuvwx\r\n"
    "a=fingerprint:sha-256 AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:"
    "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99\r\n"
    "a=setup:actpass\r\n"
    "a=mid:0\r\n"
    "a=sendrecv\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=mid:1\r\n"
    "a=sctp-port:5000\r\n"
)
ANSWER = (
    "v=0\r\n"
    "o=- 2 2 IN IP4 0.0.0.0\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=sendrecv\r\n"
)


def _device() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), role="primary_companion", memory_scope="sandbox", name="Primary")


def test_unified_parts_are_form_fields_not_files() -> None:
    parts = unified_call_parts(OFFER, {"type": "realtime", "model": "gpt-realtime-2.1-mini"})
    assert parts["sdp"][0] is None
    assert parts["sdp"][2] == "application/sdp"
    assert parts["sdp"][1] == OFFER.encode("utf-8")
    assert parts["session"][0] is None
    assert parts["session"][2] == "application/json"
    session = json.loads(parts["session"][1].decode("utf-8"))
    assert session["type"] == "realtime"


def test_prepare_offer_does_not_strip() -> None:
    raw = OFFER
    assert prepare_offer_sdp(raw) is raw
    spaced = "  v=0\r\n"
    assert prepare_offer_sdp(spaced) == spaced
    with pytest.raises(HTTPException) as exc:
        prepare_offer_sdp('{"sdp":"nope"}')
    assert exc.value.status_code == 400
    assert exc.value.detail["failed_stage"] == "M11"


def test_summarize_sdp_structure_only() -> None:
    meta = summarize_sdp(OFFER)
    assert meta["audio_mline"] is True
    assert meta["application_mline"] is True
    assert meta["opus"] is True
    assert meta["ice"] is True
    assert meta["fingerprint"] is True
    assert meta["direction"] == "sendrecv"
    assert "v=0" not in json.dumps(meta)


def test_proxy_source_does_not_use_filename_file_parts() -> None:
    src = (ROOT / "app" / "device_gateway" / "webrtc_live.py").read_text()
    assert '("offer.sdp"' not in src
    assert "SIGNALING_IMPLEMENTATION = \"unified_calls\"" in src
    assert "filename" in src


class _FakeResponse:
    def __init__(self, *, status: int, text: str, location: str | None = None, ctype: str = "application/sdp") -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": ctype}
        if location:
            self.headers["location"] = location

    def json(self) -> dict:
        return json.loads(self.text)


class _FakeClient:
    last: dict | None = None

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> bool:
        del args
        return False

    async def post(self, url, headers=None, files=None, **kwargs):
        del kwargs
        _FakeClient.last = {
            "url": url,
            "files": files,
            "header_names": sorted((headers or {}).keys()),
            "has_auth": "Authorization" in (headers or {}),
        }
        return self.response


@pytest.mark.asyncio
async def test_create_call_sends_unfilenamed_sdp_field(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-test-not-used")
    _FakeClient.response = _FakeResponse(
        status=201,
        text=ANSWER,
        location="/v1/realtime/calls/rtc_u7_TESTCALLID",
    )
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    result = await create_realtime_call(device=_device(), offer_sdp=OFFER, attempt_id="mv_test")
    captured = _FakeClient.last
    assert captured is not None
    assert captured["files"]["sdp"][0] is None
    assert captured["files"]["session"][0] is None
    assert captured["has_auth"] is True
    assert result["sdp"] == ANSWER
    assert result["call_id"] == "rtc_u7_TESTCALLID"
    assert result["signaling"] == SIGNALING_IMPLEMENTATION
    assert result["offer_sha256"]
    assert result["answer_sha256"]


@pytest.mark.asyncio
async def test_provider_missing_sdp_field_is_m10(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-test-not-used")
    _FakeClient.response = _FakeResponse(
        status=400,
        text=json.dumps(
            {
                "error": {
                    "message": 'Invalid multipart form, field "sdp" is required but not found',
                    "type": "invalid_request_error",
                    "code": "invalid_form_data",
                }
            }
        ),
        ctype="application/json",
    )
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    with pytest.raises(HTTPException) as exc:
        await create_realtime_call(device=_device(), offer_sdp=OFFER, attempt_id="mv_old")
    assert exc.value.status_code == 502
    assert exc.value.detail["failed_stage"] == "M10"
    assert exc.value.detail["provider_status"] == 400
    assert exc.value.detail["provider_code"] == "invalid_form_data"
    assert exc.value.detail["message"] == "Realtime signaling failed."


@pytest.mark.asyncio
async def test_non_sdp_success_body_is_m11(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-test-not-used")
    _FakeClient.response = _FakeResponse(status=201, text='{"answer":"wrapped"}', ctype="application/json")
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    with pytest.raises(HTTPException) as exc:
        await create_realtime_call(device=_device(), offer_sdp=OFFER)
    assert exc.value.detail["failed_stage"] == "M11"


def test_status_and_pwa_connection_contract() -> None:
    status = public_audio_status()
    assert status["signaling"] == "unified_calls"
    assert status["signaling_version"] == SIGNALING_VERSION
    assert status["provider_key_in_browser"] is False
    assert status["pcm_fallback_allowed"] is False
    app_js = (PWA / "app.js").read_text()
    webrtc = (PWA / "webrtc.js").read_text()
    html = (PWA / "index.html").read_text()
    assert "2026.08.21.22" in app_js
    assert "2026.08.21.22" in html
    assert "Couldn't connect to Evie Voice." in app_js
    assert "Voice connection failed." not in app_js
    assert "Microphone access denied." in app_js
    assert "Voice connected — tap to enable audio" in app_js
    assert "M21" in webrtc
    assert "VOICE_READY" in webrtc
    assert "unified-calls-v1" in webrtc
    assert "waitIce" not in webrtc
    assert '("offer.sdp"' not in webrtc
    assert "Copy Voice Diagnostic" in html
    assert "FAILED AT:" in html
    assert "voice-stages" in html
    assert "OPENAI_API_KEY" not in webrtc
    assert "api.openai.com" not in webrtc
    csp = (ROOT / "app" / "device_gateway" / "pwa.py").read_text()
    assert "https://api.openai.com" in csp


def test_js_connection_helpers() -> None:
    result = subprocess.run(
        [
            "node",
            "-e",
            "const mv=require(process.argv[1]);"
            "const s=mv.summarizeSdp('v=0\\r\\nm=audio 9 UDP/TLS/RTP/SAVPF 111\\r\\na=sendrecv\\r\\n"
            "a=rtpmap:111 opus/48000/2\\r\\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\\r\\n');"
            "if(!s.audio_mline||!s.application_mline||s.direction!=='sendrecv') process.exit(2);"
            "const d=new mv.ConnectionDiag('mv_1'); d.pass('M00'); d.fail('M10', Object.assign(new Error('Realtime signaling failed.'),{status:400}));"
            "if(d.failed_stage!=='M10') process.exit(3);"
            "const text=mv.formatConnectionDiag(d.snapshot(),{build:'2026.08.21.22'});"
            "if(text.indexOf('M10')<0||text.indexOf('mv_1')<0) process.exit(4);"
            "if(mv.STAGES.length!==22) process.exit(5);"
            "console.log('ok');",
            str(PWA / "webrtc.js"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
