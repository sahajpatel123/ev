"""HANDS tests are hermetic and DB-free: override the heavy autouse DB fixture."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def fresh_db():  # noqa: D401 - override of tests/conftest.py::fresh_db
    yield


@pytest.fixture
def fake_rt(tmp_path):
    from app.fabric.skills.testing import FakeRuntime

    return FakeRuntime(home=tmp_path)
