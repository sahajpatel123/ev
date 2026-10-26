"""`ev models verify` (R13): the two configured ids resolve against provider listings."""

from __future__ import annotations

import pytest

from app.config import settings
from clients.cli.models import print_verify, verify_models

BRAIN = "meta/muse-spark-1.3-contributor"
LIVE = "gemini-3.8-live-extended-thinking"


def _openrouter(*, model=BRAIN, params=("tools", "reasoning"), outputs=("text",)):
    return {
        "data": [
            {"id": "other/model", "supported_parameters": ["tools"]},
            {
                "id": model,
                "context_length": 1048576,
                "supported_parameters": list(params),
                "architecture": {"output_modalities": list(outputs)},
            },
        ]
    }


def _google(*, name=f"models/{LIVE}", methods=("bidiGenerateContent",), pages=1):
    entry = {"name": name, "supportedGenerationMethods": list(methods)}
    filler = [{"name": f"models/filler-{i}", "supportedGenerationMethods": ["generateContent"]} for i in range(3)]
    if pages == 1:
        return [{"models": filler + [entry]}]
    return [{"models": filler, "nextPageToken": "p2"}, {"models": [entry]}]


class _Fetch:
    def __init__(self, openrouter=None, google=None, *, or_status=200, g_status=200):
        self.openrouter, self.google = openrouter, list(google or [])
        self.or_status, self.g_status = or_status, g_status
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if "openrouter" in url or url.endswith("/models") and "googleapis" not in url:
            return self.or_status, self.openrouter
        return self.g_status, self.google.pop(0) if self.google else {}


@pytest.fixture(autouse=True)
def _ids(monkeypatch):
    monkeypatch.setattr(settings, "brain_model", BRAIN)
    monkeypatch.setattr(settings, "gemini_live_model", LIVE)
    monkeypatch.setattr(settings, "google_api_key", "AIza-test-key-not-real")
    monkeypatch.setattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")


def test_both_ids_resolve_exit_zero() -> None:
    report = verify_models(fetch=_Fetch(_openrouter(), _google()))
    assert report["exit_code"] == 0 and report["ok"] is True
    assert [c["status"] for c in report["checks"]] == ["resolved", "resolved"]


def test_google_listing_is_paged() -> None:
    report = verify_models(fetch=_Fetch(_openrouter(), _google(pages=2)))
    assert report["checks"][1]["status"] == "resolved"


def test_brain_id_absent_from_the_listing_fails_loudly(monkeypatch) -> None:
    monkeypatch.setattr(settings, "brain_model", "meta/retired-model")
    report = verify_models(fetch=_Fetch(_openrouter(), _google()))
    assert report["exit_code"] == 1
    brain = report["checks"][0]
    assert brain["status"] == "missing" and "EV_BRAIN_MODEL" in brain["detail"]


def test_brain_without_tool_calling_is_unsuitable() -> None:
    report = verify_models(fetch=_Fetch(_openrouter(params=("reasoning",)), _google()))
    assert report["exit_code"] == 1
    assert report["checks"][0]["status"] == "unsuitable"


def test_brain_that_does_not_output_text_is_unsuitable() -> None:
    report = verify_models(fetch=_Fetch(_openrouter(outputs=("image",)), _google()))
    assert report["checks"][0]["status"] == "unsuitable"


def test_live_model_without_bidi_support_is_unsuitable() -> None:
    report = verify_models(fetch=_Fetch(_openrouter(), _google(methods=("generateContent",))))
    assert report["exit_code"] == 1
    assert report["checks"][1]["status"] == "unsuitable"


def test_live_id_absent_from_google_listing_fails_loudly(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_live_model", "gemini-9-live-retired")
    report = verify_models(fetch=_Fetch(_openrouter(), _google()))
    assert report["exit_code"] == 1
    assert report["checks"][1]["status"] == "missing"


def test_no_google_key_is_unverified_not_a_pass(monkeypatch) -> None:
    monkeypatch.setattr(settings, "google_api_key", None)
    fetch = _Fetch(_openrouter(), _google())
    report = verify_models(fetch=fetch)
    assert report["exit_code"] == 2 and report["ok"] is False
    assert report["checks"][1]["status"] == "unverified"
    assert not any("googleapis" in url for url, _ in fetch.calls)


def test_unreadable_listing_claims_nothing() -> None:
    report = verify_models(fetch=_Fetch(None, _google(), or_status=503))
    assert report["checks"][0]["status"] == "unverified"
    assert report["exit_code"] == 2


def test_a_missing_id_outranks_an_unreadable_listing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "google_api_key", None)
    monkeypatch.setattr(settings, "brain_model", "meta/retired-model")
    report = verify_models(fetch=_Fetch(_openrouter(), _google()))
    assert report["exit_code"] == 1


def test_the_key_goes_in_a_header_and_never_into_the_output(capsys) -> None:
    fetch = _Fetch(_openrouter(), _google())
    report = verify_models(fetch=fetch)
    print_verify(report)
    out = capsys.readouterr().out
    assert "AIza-test-key-not-real" not in out
    google_calls = [(u, h) for u, h in fetch.calls if "googleapis" in u]
    assert google_calls and all("AIza" not in u for u, _ in google_calls)
    assert all(h.get("x-goog-api-key") == "AIza-test-key-not-real" for _, h in google_calls)


def test_cli_exposes_verify() -> None:
    from pathlib import Path

    import clients.cli as cli

    parser_src = Path(cli.__file__).read_text(encoding="utf-8")
    assert 'choices=["status", "install", "verify"]' in parser_src
