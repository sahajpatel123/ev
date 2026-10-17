"""S19: standing-grant lifecycle — create, use, expire, revoke; never-grantable."""
import time


def test_s19_grant_lifecycle():
    from app.fabric import grants
    g = grants.grant("shell.run", ttl_s=60)
    assert g["ok"] and g["verified"], g
    use = grants.check_grant("shell.run")
    assert use["ok"] and use["evidence"].get("active"), use
    assert "shell.run" in grants.active_grants()
    r = grants.revoke("shell.run")
    assert r["ok"] and r["verified"], r
    after = grants.check_grant("shell.run")
    assert not after["ok"], after
    # expiry
    grants.grant("short.lived", ttl_s=-1)
    assert not grants.check_grant("short.lived")["ok"]
    assert "short.lived" not in grants.active_grants()


def test_s19_never_grantable():
    from app.fabric import grants
    for scope in ("money", "legal", "credential", "first-contact"):
        g = grants.grant(scope)
        assert not g["ok"], (scope, g)
        assert not grants.check_grant(scope)["ok"], scope
    # default: no grant means not granted
    assert not grants.check_grant("channel:mom")["ok"]
