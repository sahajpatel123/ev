
def test_s05_shell_run(tmp_path, fake_shell):
    from app.fabric.shell_sessions import ShellSessions
    s = ShellSessions(root=tmp_path)
    r = s.run("echo hi")
    assert r["ok"] and r["verified"]

def test_s06_shell_deny(tmp_path):
    from app.fabric.shell_sessions import ShellSessions
    s = ShellSessions(root=tmp_path)
    r = s.run("rm -rf /")
    assert not r["ok"] or "verified" in r
