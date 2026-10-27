"""GE3: submission registry + gate mechanics (tmp git repos, no network)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.growth.train import gate


def _repo(tmp_path) -> str:
    r = tmp_path / "r"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=r, check=True)
    return str(r)


def test_added_lines_parses_unified_diff(tmp_path, monkeypatch):
    from app.growth.train import gate as G

    repo = _repo(tmp_path)
    monkeypatch.setattr(G, "repo", lambda: __import__("pathlib").Path(repo))
    subprocess.run(["git", "checkout", "-qb", "feat"], cwd=repo, check=True)
    Path(f"{repo}/a.py").write_text("x = 1\ny = 2\nz = 3\n")
    subprocess.run(["git", "commit", "-qam", "x"], cwd=repo, check=True)
    added = G.added_lines("main", "feat")
    assert added["a.py"] == {2, 3}


def test_scope_subset_and_shared_append_only(tmp_path, monkeypatch):
    from app.growth.train import gate as G

    repo = _repo(tmp_path)
    monkeypatch.setattr(G, "repo", lambda: __import__("pathlib").Path(repo))
    subprocess.run(["git", "checkout", "-qb", "feat"], cwd=repo, check=True)
    Path(f"{repo}/a.py").write_text(Path(f"{repo}/a.py").read_text() + "y = 2\n")
    subprocess.run(["git", "commit", "-qam", "x"], cwd=repo, check=True)
    ok, _ = G.check_scope(["a.py"], "a.py", "main", "feat")
    assert ok
    ok, detail = G.check_scope(["a.py", "other.py"], "a.py", "main", "feat")
    assert not ok and "outside declared scope" in detail


def test_anticheat_trips_on_added_cheat_lines(tmp_path):
    wt = tmp_path / "wt"
    (wt / "backend" / "app").mkdir(parents=True)
    (wt / "backend" / "app" / "x.py").write_text("import pytest\nTEST_MODE = 1\nok = 1\n")
    added = {"backend/app/x.py": {1, 2}}
    ok, detail = gate.check_anticheat(wt, ["backend/app/x.py"], added)
    assert not ok and "prod code" in detail
    ok2, _ = gate.check_anticheat(wt, ["backend/app/x.py"], {"backend/app/x.py": {3}})
    assert ok2  # finding on untouched lines does not count


def test_submit_rejects_empty_and_missing_branch():
    from app.growth.train.submit import branch_diff_files

    with pytest.raises(ValueError):
        branch_diff_files("definitely-not-a-branch-xyz")


def test_gitleaks_trips_on_real_secret_not_on_fixture(tmp_path):
    import shutil

    if not shutil.which("gitleaks"):
        pytest.skip("gitleaks not installed")
    repo = tmp_path / "g"
    repo.mkdir()
    # Split so the fake key never appears contiguously in this blob (push
    # protection); the written file content is byte-identical at runtime.
    (repo / "real.py").write_text('AWS_KEY = "AKIA' 'IOSFODNN7X4PL3K9QW28"\n')
    p = subprocess.run(["gitleaks", "detect", "--no-git", "--source", str(repo / "real.py"),
                        "--config", "/Users/sahajpatel/Code/ev/.gitleaks.toml"],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode != 0, p.stdout + p.stderr
    (repo / "fake.py").write_text('# placeholder, not a secret: AKIA-FAKE-EXAMPLE\n')
    p2 = subprocess.run(["gitleaks", "detect", "--no-git", "--source", str(repo / "fake.py"),
                         "--config", "/Users/sahajpatel/Code/ev/.gitleaks.toml"],
                        capture_output=True, text=True, timeout=120)
    assert p2.returncode == 0, p2.stdout + p2.stderr
