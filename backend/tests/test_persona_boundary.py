"""Boundary + purity tests: no network/LLM imports, <5 ms, deterministic."""

from __future__ import annotations

import ast
import time
from pathlib import Path

from app.persona import build_persona_block, persona_guard
from app.persona.callbacks import InMemoryCallbackStore
from app.persona.context import PersonaContext
from app.persona.reactions import classify_reaction
from app.persona.settings import PERSONA_ENABLED
from app.persona.tone import decide_tone

PERSONA_DIR = Path(__file__).resolve().parent.parent / "app" / "persona"

# Network clients, model SDKs, and provider/lane modules. app.utils (pure
# helpers) and app.persona itself are allowed; everything else app.* is denied
# so the persona hot path can never reach the network or a model.
DENIED_TOP_LEVEL = {
    "httpx", "requests", "urllib", "urllib3", "socket", "ssl", "http",
    "openai", "anthropic", "google", "vertexai", "transformers", "torch",
    "onnxruntime", "edge_tts", "aiohttp", "websockets",
}
# app.* allowlist: utils (pure helpers), persona (self), and exactly one
# pinned leaf — app.ev.dynamic_personality.turn_shape, which imports only
# os/re/dataclasses (verified) and carries no network or model access.
ALLOWED_APP_MODULES = {"app.utils", "app.persona", "app.ev.dynamic_personality"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
            if node.module == "app" or node.module.startswith("app."):
                modules.add(node.module)
    return modules


def test_no_network_or_model_imports() -> None:
    offenders: list[str] = []
    for path in sorted(PERSONA_DIR.glob("*.py")):
        for module in _imported_modules(path):
            root = module.split(".")[0]
            if root in DENIED_TOP_LEVEL:
                offenders.append(f"{path.name}: {module}")
            if (
                module == "app"
                or module in ALLOWED_APP_MODULES
                or module.startswith("app.persona.")
                or module.startswith("app.utils.")
            ):
                continue
            if module.startswith("app."):
                offenders.append(f"{path.name}: {module}")
    assert offenders == []


def test_pinned_leaf_module_is_pure() -> None:
    import app.ev.dynamic_personality as leaf

    assert leaf.__file__ is not None
    tree = ast.parse(Path(leaf.__file__).read_text(encoding="utf-8"))
    top_level = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    # Import stays light: stdlib only at top level (no cycles, fast import).
    assert top_level <= {"__future__", "os", "re", "dataclasses"}, top_level
    # Function-level app imports are pinned to the §4 delegation set:
    # app.persona (proven pure above) plus the reused detectors.
    allowed_lazy = {
        "app.persona.delegation",
        "app.ev.interaction",
        "app.ev.resolve",
    }
    for module in _imported_modules(Path(leaf.__file__)):
        assert module.split(".")[0] not in DENIED_TOP_LEVEL, module
        if module.startswith("app."):
            assert module in allowed_lazy, module


def test_flag_stays_off() -> None:
    assert PERSONA_ENABLED is False


def _sample_ctx() -> PersonaContext:
    return PersonaContext(channel="text", owner_msg="roast me", thread_id="t",
                          turn_index=1)


def test_hot_path_under_5ms() -> None:
    ctx = _sample_ctx()
    store = InMemoryCallbackStore()
    build_persona_block(ctx)  # warm caches (spec read, exemplar load)
    calls = [
        lambda: build_persona_block(ctx),
        lambda: build_persona_block(ctx, store=store),
        lambda: decide_tone(ctx, None),
        lambda: persona_guard("Great question! done 🎉🎉", ctx),
        lambda: classify_reaction("haha nice", None),
    ]
    for call in calls:
        worst = 0.0
        for _ in range(50):
            start = time.perf_counter()
            call()
            worst = max(worst, time.perf_counter() - start)
        assert worst < 0.005, f"{worst * 1000:.2f} ms over budget"


def test_deterministic_across_calls() -> None:
    ctx = _sample_ctx()
    first = build_persona_block(ctx)
    for _ in range(5):
        again = build_persona_block(ctx)
        assert again.text == first.text
        assert again.tone == first.tone


def test_receipts_are_read_only() -> None:
    receipts = ({"ok": False, "name": "send"},)
    ctx = PersonaContext(channel="text", receipts=receipts)
    build_persona_block(ctx)
    assert receipts == ({"ok": False, "name": "send"},)
