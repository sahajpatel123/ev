"""S21: the locked API contract is additive-only; fabric adds no routes."""
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONTRACT = REPO / "backend" / "eval" / "contract_v1.json"


def _load(p: Path) -> dict:
    return json.loads(p.read_text())


def test_s21_contract_paths_unchanged():
    cur = _load(CONTRACT)
    # 550 locked at baseline + 17 /v1/fabric/* paths (WP1+WP3, additive; the last
    # 2 are the S3 presence stream/since endpoints) + 3
    # /v1/device-gateway/relay/* paths (pre-existing drift, locked by the
    # same regeneration — they were live but unmanifested since baseline)
    # + 10 /v1/agent/* paths (general agent runtime + durable task lane).
    assert len(cur["paths"]) == 580, len(cur["paths"])
    base_raw = subprocess.run(
        ["git", "show", "pre-fabric-baseline:backend/eval/contract_v1.json"],
        capture_output=True, text=True, cwd=str(REPO), timeout=30)
    assert base_raw.returncode == 0, "baseline tag must exist for R4 proof"
    base = json.loads(base_raw.stdout)
    assert set(base["paths"].keys()) <= set(cur["paths"].keys()), "original paths removed"
    for path, ops in base["paths"].items():
        assert cur["paths"][path] == ops, f"contract changed at {path}"
    added = set(cur["paths"].keys()) - set(base["paths"].keys())
    for path in added:
        assert (
            path.startswith("/v1/fabric/")
            or path.startswith("/v1/device-gateway/relay/")
            or path.startswith("/v1/agent/")
        ), path


def test_s21_fabric_adds_no_routes():
    import app.fabric.bridge as b
    import app.fabric.builder as bd
    import app.fabric.comms as c
    import app.fabric.files as f
    import app.fabric.policy as p
    import app.fabric.run_turn as rt
    import app.fabric.screen_ref as sc
    import app.fabric.shell_sessions as s
    import app.fabric.sql_lane as q
    import app.fabric.terminal as t
    for mod in (p, rt, f, s, q, sc, b, c, bd, t):
        src = Path(mod.__file__).read_text()
        assert "APIRouter" not in src and "include_router" not in src, mod.__name__


def test_s21_fabric_settings_drift():
    # R6: every EV_FABRIC_* var lives in Settings + .env.example + ENVIRONMENT.md
    import re
    settings_src = (REPO / "backend" / "app" / "config.py").read_text()
    example = (REPO / ".env.example").read_text()
    env_doc = (REPO / "docs" / "ENVIRONMENT.md").read_text()
    # Settings uses env_prefix="EV_": fabric_roots -> EV_FABRIC_ROOTS
    fields = sorted(set(re.findall(r"^\s+(fabric_[a-z_]+)\s*:", settings_src, re.M)))
    if re.search(r"^\s+fabric\s*:", settings_src, re.M):
        fields.append("fabric")
    fabric_vars = ["EV_" + f.upper() for f in fields]
    assert fabric_vars, "fabric Settings vars missing"
    for var in fabric_vars:
        assert var in example, f"{var} missing from .env.example"
        assert var in env_doc, f"{var} missing from docs/ENVIRONMENT.md"
    # no bare os.getenv EV_* bypasses in fabric modules
    import app.fabric as fab
    pkg = Path(fab.__file__).parent
    for mod in pkg.glob("*.py"):
        src = mod.read_text()
        assert 'os.getenv("EV_' not in src and "os.getenv('EV_" not in src, mod.name
