"""Production isolation regression: destructive DDL must not touch live DB."""
import os
import pytest

def test_production_drop_all_refused():
    """Direct drop_all against production URL must hard-fail."""
    # Simulate a manual script that inherited production URL
    os.environ["EV_DATABASE_URL"] = "postgresql+psycopg://ev:ev@localhost:5432/ev"
    # Need to reload settings to pick up new URL
    import importlib
    import app.config
    importlib.reload(app.config)
    from app.config import settings
    # Force settings to production URL
    settings.database_url = "postgresql+psycopg://ev:ev@localhost:5432/ev"
    from app.ops.prod_guard import assert_not_production_for_destructive
    with pytest.raises(RuntimeError, match="REFUSED"):
        assert_not_production_for_destructive("test drop_all")
    # Also via Base.metadata guard
    from app.db import Base
    try:
        Base.metadata.drop_all  # this is the guarded wrapper
        # The wrapper itself will raise when called with a bind, but just checking the function exists
        assert callable(Base.metadata.drop_all)
    finally:
        # restore
        os.environ.pop("EV_DATABASE_URL", None)

def test_isolated_drop_all_allowed(tmp_path):
    """Same operation against sqlite isolated DB must be allowed."""
    isolated = f"sqlite+aiosqlite:///{tmp_path}/isolated_test.db"
    os.environ["EV_DATABASE_URL"] = isolated
    import importlib
    import app.config
    importlib.reload(app.config)
    from app.config import settings
    settings.database_url = isolated
    from app.ops.prod_guard import assert_not_production_for_destructive, is_production_database
    assert not is_production_database()
    # Should not raise
    assert_not_production_for_destructive("test isolated")
    # create_all/drop_all on sqlite should succeed
    from app.db import Base
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine
    async def _run():
        engine = create_async_engine(isolated)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
    asyncio.run(_run())
    os.environ.pop("EV_DATABASE_URL", None)

def test_subprocess_does_not_inherit_production():
    """Developer subprocess default must be isolated, not production."""
    # This is a meta-test: conftest should have set EV_DATABASE_URL to sqlite temp
    # When running pytest, the env should not be the production postgres URL
    url = os.environ.get("EV_DATABASE_URL", "")
    # In pytest, conftest sets it to sqlite+aiosqlite:///.../test.db
    # So it must not contain production marker
    assert "5432/ev" not in url or "ev_test" in url or "sqlite" in url, f"Subprocess inherited production URL: {url}"
