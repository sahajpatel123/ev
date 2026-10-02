"""API exposure hardening: the tailnet-facing schema stays closed by default."""

from __future__ import annotations


def test_api_docs_are_disabled_by_default() -> None:
    from app.config import settings
    from app.main import app

    assert settings.api_docs_enabled is False
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    # The schema is still generated in-process for eval gates and contracts.
    assert app.openapi()["openapi"]
