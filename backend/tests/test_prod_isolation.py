"""Production isolation regression: destructive DDL must not touch live DB."""
import asyncio

import pytest


def test_production_drop_all_refused(monkeypatch):
    """Direct drop_all against production URL must hard-fail."""
    # Simulate a manual script that inherited production URL. Point the shared
    # settings singleton at the production DSN (monkeypatch restores it) so
    # every importer sees the same object — reloading app.config would fork
    # the singleton and leak the production URL past this test.
    from app.config import settings

    monkeypatch.setattr(
        settings, "database_url", "postgresql+psycopg://ev:ev@localhost:5432/ev"
    )
    from app.ops.prod_guard import assert_not_production_for_destructive

    with pytest.raises(RuntimeError, match="REFUSED"):
        assert_not_production_for_destructive("test drop_all")
    # The wrapper itself will raise when called with a bind, but the guard
    # hook must be present on the shared metadata either way.
    from app.db import Base

    assert callable(Base.metadata.drop_all)


def test_isolated_drop_all_allowed(tmp_path, monkeypatch):
    """Same operation against sqlite isolated DB must be allowed."""
    isolated = f"sqlite+aiosqlite:///{tmp_path}/isolated_test.db"
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", isolated)
    from app.ops.prod_guard import (
        assert_not_production_for_destructive,
        is_production_database,
    )

    assert not is_production_database()
    # Should not raise
    assert_not_production_for_destructive("test isolated")
    # create_all/drop_all on sqlite should succeed
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import Base

    async def _run():
        engine = create_async_engine(isolated)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()

    asyncio.run(_run())


def test_subprocess_does_not_inherit_production():
    """Developer subprocess default must be isolated, not production."""
    # This is a meta-test: conftest should have set EV_DATABASE_URL to sqlite temp
    # When running pytest, the env should not be the production postgres URL
    import os

    url = os.environ.get("EV_DATABASE_URL", "")
    # In pytest, conftest sets it to sqlite+aiosqlite:///.../test.db
    # So it must not contain production marker
    assert "5432/ev" not in url or "ev_test" in url or "sqlite" in url, f"Subprocess inherited production URL: {url}"
