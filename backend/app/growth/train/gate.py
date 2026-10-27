"""GE3 stage 2 GATE: fast static + targeted checks on a submission worktree.

Every check returns (pass: bool, detail: str). A submission passes the gate
only if all checks pass. New-code rule: lint/type findings count only on
added lines, so pre-existing tree debt never blocks a clean submission —
but no new violation ever lands either.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

SHARED_FILES = ("Makefile", ".env.example", "compose.yaml", "docs/ENVIRONMENT.md",
                "backend/app/config.py", "backend/app/models.py", "backend/app/schemas.py",
                "backend/app/api/core.py", "backend/app/api/ev.py",
                "backend/app/api/edith.py", "backend/app/api/companion.py",
                "backend/app/api/tools.py")


def repo() -> Path:
    return Path(__file__).resolve().parents[4]


def ensure_worktree(branch: str) -> Path:
    """Fresh worktree for the branch under ~/.ev/growth/worktrees/ (no edits made)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", branch)
    wt = Path.home() / ".ev" / "growth" / "worktrees" / safe
    if wt.exists():
        subprocess.run(["git", "-C", str(repo()), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, timeout=120)
        shutil.rmtree(wt, ignore_errors=True)
    p = subprocess.run(["git", "-C", str(repo()), "worktree", "add", "--detach", str(wt), branch],
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"worktree add failed: {p.stderr.strip()[-300:]}")
    venv = wt / "backend" / ".venv"
    if not venv.exists():
        main_venv = repo() / "backend" / ".venv"
        if main_venv.is_dir():
            venv.symlink_to(main_venv)
    return wt


def diff_files(base: str, branch: str) -> list[str]:
    p = subprocess.run(["git", "diff", "--name-only", f"{base}...{branch}"],
                       cwd=str(repo()), capture_output=True, text=True, timeout=60)
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def added_lines(base: str, branch: str) -> dict[str, set[int]]:
    """Map path -> added line numbers (new-file side) from the diff."""
    p = subprocess.run(["git", "diff", "-U0", f"{base}...{branch}"],
                       cwd=str(repo()), capture_output=True, text=True, timeout=120)
    out: dict[str, set[int]] = {}
    cur, new_line = "", 0
    for ln in p.stdout.splitlines():
        if ln.startswith("+++ b/"):
            cur = ln[6:]
            out.setdefault(cur, set())
        elif ln.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", ln)
            new_line = int(m.group(1)) if m else 0
        elif cur and not ln.startswith("---"):
            if ln.startswith("+") and not ln.startswith("+++"):
                out[cur].add(new_line)
                new_line += 1
            elif ln.startswith("-") and not ln.startswith("---"):
                pass
            else:
                new_line += 1
    return out


def _py(wt: Path) -> str:
    v = wt / "backend" / ".venv" / "bin" / "python"
    return str(v if v.exists() else Path(sys.executable))


def check_build(wt: Path, files: list[str]) -> tuple[bool, str]:
    be = [f for f in files if f.startswith("backend/") and f.endswith(".py")]
    mac = [f for f in files if f.startswith("macos/")]
    notes = []
    if be:
        p = subprocess.run([_py(wt), "-m", "compileall", "-q",
                            *[str(wt / f) for f in be if (wt / f).is_file()]],
                           capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            return (False, f"compileall failed: {p.stderr.strip()[-300:]}")
        p = subprocess.run([_py(wt), "-c", "import app.main"],
                           cwd=str(wt / "backend"), capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            return (False, f"import app.main failed: {p.stderr.strip()[-300:]}")
        notes.append(f"backend: {len(be)} files compile, app.main imports")
    if mac:
        p = subprocess.run(["swift", "build", "-c", "release"], cwd=str(wt / "macos"),
                           capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            return (False, f"swift build failed: {(p.stderr or '')[-300:]}")
        notes.append("macos: swift release build ok")
    return (True, "; ".join(notes) or "no buildable changes")


def _on_added(tool_out: str, added: dict[str, set[int]], prefix: str) -> list[str]:
    hits = []
    for ln in tool_out.splitlines():
        m = re.match(rf"(?:\./)?(?:{re.escape(prefix)}/)?([^:]+):(\d+)", ln)
        if not m:
            continue
        path, lineno = m.group(1), int(m.group(2))
        for full, lines in added.items():
            if full.endswith(path) and lineno in lines:
                hits.append(ln.strip())
                break
    return hits


def check_lint(wt: Path, files: list[str], added: dict[str, set[int]]) -> tuple[bool, str]:
    pys = [f for f in files if f.endswith(".py") and (wt / f).is_file()]
    if not pys:
        return (True, "no python changes")
    p = subprocess.run([_py(wt), "-m", "ruff", "check", *pys], cwd=str(wt),
                       capture_output=True, text=True, timeout=600)
    hits = _on_added((p.stdout or "") + "\n" + (p.stderr or ""), added, "")
    if hits:
        return (False, f"ruff on added lines: {hits[0][:200]} (+{len(hits) - 1} more)")
    return (True, f"ruff clean on added lines ({len(pys)} files)")


def check_types(wt: Path, files: list[str], added: dict[str, set[int]]) -> tuple[bool, str]:
    pys = [f for f in files if f.startswith("backend/") and f.endswith(".py")
            and (wt / f).is_file()]
    if not pys:
        return (True, "no backend python changes")
    p = subprocess.run([_py(wt), "-m", "mypy", *pys], cwd=str(wt / "backend"),
                       capture_output=True, text=True, timeout=1200)
    rel_added = {k[8:] if k.startswith("backend/") else k: v for k, v in added.items()}
    hits = _on_added(p.stdout or "", rel_added, "")
    hits = [h for h in hits if "note:" not in h]
    if hits:
        return (False, f"mypy on added lines: {hits[0][:200]} (+{len(hits) - 1} more)")
    return (True, f"mypy clean on added lines ({len(pys)} files)")


def check_targeted_tests(wt: Path, files: list[str]) -> tuple[bool, str]:
    targets: set[str] = set()
    for f in files:
        if f.startswith("backend/tests/") and f.endswith(".py") and (wt / f).is_file():
            targets.add(f[8:])
        elif f.startswith("backend/app/") and f.endswith(".py"):
            stem = Path(f).stem
            for cand in sorted((wt / "backend" / "tests").rglob(f"test_*{stem}*.py")):
                targets.add(f"tests/{cand.name}" if cand.parent.name == "tests"
                            else f"tests/{cand.parent.name}/{cand.name}")
        elif f.startswith("backend/clients/"):
            targets.add("tests/test_cli.py")
    guards = ["tests/test_config_drift.py", "tests/e2e_fabric/test_s21_contract.py",
              "tests/test_growth_eval.py", "tests/test_growth_budget.py"]
    targets.update(g for g in guards if (wt / "backend" / g).is_file())
    if not targets:
        return (True, "no test targets")
    p = subprocess.run([_py(wt), "-m", "pytest", "-q", "--tb=line", "-p", "no:cacheprovider",
                        *sorted(targets)], cwd=str(wt / "backend"),
                       capture_output=True, text=True, timeout=1800)
    tail = (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else ""
    if p.returncode != 0:
        fails = [ln for ln in (p.stdout or "").splitlines() if ln.startswith("FAILED")]
        return (False, f"targeted tests failed ({len(targets)} files): "
                       f"{(fails[0] if fails else tail)[:200]}")
    return (True, f"targeted tests pass ({len(targets)} files): {tail[:120]}")


def check_contract_drift(wt: Path) -> tuple[bool, str]:
    p = subprocess.run([_py(wt), "-m", "pytest", "-q", "--tb=line", "-p", "no:cacheprovider",
                        "tests/test_config_drift.py", "tests/e2e_fabric/test_s21_contract.py"],
                       cwd=str(wt / "backend"), capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        return (False, f"contract/drift failed: {(p.stdout or '')[-300:]}")
    return (True, "contract + settings drift green")


def check_secrets(base: str, branch: str) -> tuple[bool, str]:
    patch = subprocess.run(["git", "diff", f"{base}...{branch}"], cwd=str(repo()),
                           capture_output=True, text=True, timeout=120).stdout
    if not patch.strip():
        return (True, "empty diff")
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(patch)
        name = f.name
    try:
        p = subprocess.run(["gitleaks", "detect", "--no-git", "--source", name,
                            "--config", str(repo() / ".gitleaks.toml"), "--verbose"],
                           capture_output=True, text=True, timeout=600)
    finally:
        Path(name).unlink(missing_ok=True)
    if p.returncode == 0:
        return (True, "gitleaks: no leaks in diff")
    if "no leaks found" in (p.stdout or "").lower():
        return (True, "gitleaks: no leaks in diff")
    leaks = [ln for ln in (p.stdout or "").splitlines() if "Secret:" in ln or "RuleID:" in ln]
    return (False, f"gitleaks found leaks: {(leaks[0] if leaks else p.stdout[-200:])}")


CHEAT_PATTERNS = (
    (r"^\s*(import pytest|from pytest)\b", "pytest import in prod code"),
    (r"\bTEST_MODE\b", "TEST_MODE branch in prod code"),
    (r"\bEV_TEST_[A-Z_]+\b", "EV_TEST_* branch in prod code"),
    (r"""["']tests?/[\w_.-]+\.py::""", "test node-id reference in prod code"),
)


def check_anticheat(wt: Path, files: list[str], added: dict[str, set[int]]) -> tuple[bool, str]:
    from .anticheat_allow import ALLOW

    hits = []
    for f in files:
        if not (f.startswith("backend/app/") or f.startswith("backend/clients/")):
            continue
        if not f.endswith(".py") or not (wt / f).is_file():
            continue
        if "/tests/" in f or f.startswith("backend/tests/"):
            continue
        lines = (wt / f).read_text().splitlines()
        for i, ln in enumerate(lines, 1):
            if i not in added.get(f, set()):
                continue
            for pat, why in CHEAT_PATTERNS:
                if re.search(pat, ln) and (f, i) not in ALLOW and f not in ALLOW:
                    hits.append(f"{f}:{i}: {why}")
    if hits:
        return (False, f"anti-cheat: {hits[0]} (+{len(hits) - 1} more)")
    return (True, "anti-cheat clean on added lines")


def check_scope(files: list[str], scope: str, base: str, branch: str) -> tuple[bool, str]:
    declared = [s.strip().rstrip("/") for s in scope.split(",") if s.strip()]
    outside = [f for f in files if not any(
        f == d or f.startswith(d + "/") or d in ("*", ".", "") for d in declared)]
    if outside:
        return (False, f"outside declared scope [{scope}]: {', '.join(outside)}")
    p = subprocess.run(["git", "diff", "-U0", f"{base}...{branch}", "--",
                        *SHARED_FILES], cwd=str(repo()),
                       capture_output=True, text=True, timeout=120)
    removed = [ln for ln in p.stdout.splitlines()
               if ln.startswith("-") and not ln.startswith("---")]
    if removed:
        return (False, f"shared file edit removes lines (append-only): {removed[0][:120]}")
    return (True, f"{len(files)} files inside scope; shared files append-only")


CHECKS = ("build", "lint", "types", "targeted", "contract", "secrets", "anticheat", "scope")


def gate_branch(branch: str, scope: str, base: str = "main") -> dict:
    from .submit import branch_diff_files as diff_files

    report: dict = {"branch": branch, "base": base, "checks": {}, "pass": False}
    try:
        files = diff_files(base, branch)
        added = added_lines(base, branch)
        wt = ensure_worktree(branch)
    except Exception as e:  # noqa: BLE001 - setup failures block with reason
        report["checks"]["setup"] = {"pass": False, "detail": f"{type(e).__name__}: {e}"[:300]}
        return report
    scope_res = check_scope(files, scope, base, branch)
    results = {
        "scope": scope_res,
        "build": check_build(wt, files),
        "lint": check_lint(wt, files, added),
        "types": check_types(wt, files, added),
        "targeted": check_targeted_tests(wt, files),
        "contract": check_contract_drift(wt),
        "secrets": check_secrets(base, branch),
        "anticheat": check_anticheat(wt, files, added),
    }
    report["checks"] = {k: {"pass": ok, "detail": d} for k, (ok, d) in results.items()}
    report["worktree"] = str(wt)
    report["pass"] = all(ok for ok, _ in results.values())
    return report
