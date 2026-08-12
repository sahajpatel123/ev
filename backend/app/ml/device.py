"""Device selection: CoreML/ANE first, then MLX/Metal, then ONNX CPU.

CUDA is deliberately never assumed: this stack targets Apple Silicon unified
memory (and generic CPU fallbacks).
"""

from __future__ import annotations

import importlib.util
import platform
import sys
from collections.abc import Callable

BACKEND_COREML = "coreml"
BACKEND_MLX = "mlx"
BACKEND_ONNX_CPU = "onnx_cpu"
BACKEND_NONE = "none"


def _probe(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() in {"arm64", "aarch64"}


def _onnx_providers() -> tuple[str, ...]:
    try:
        import onnxruntime as ort
    except Exception:
        return ()
    return tuple(ort.get_available_providers())


def select_backend(
    preferred: str | None = None,
    *,
    probe: Callable[[str], bool] = _probe,
    is_apple_silicon: Callable[[], bool] = _is_apple_silicon,
    onnx_providers: Callable[[], tuple[str, ...]] = _onnx_providers,
) -> dict:
    """Choose the best available backend for this machine.

    ``preferred`` is honored only when that backend is actually available.
    """

    apple = is_apple_silicon()
    providers = onnx_providers()
    if preferred == BACKEND_COREML:
        if apple and "CoreMLExecutionProvider" in providers:
            return _result(BACKEND_COREML, "preferred CoreML EP on Apple Silicon")
    elif preferred == BACKEND_MLX:
        if apple and probe("mlx"):
            return _result(BACKEND_MLX, "preferred MLX on Apple Silicon")
    elif preferred == BACKEND_ONNX_CPU and providers:
        return _result(BACKEND_ONNX_CPU, "preferred ONNX Runtime CPU")

    if apple and providers and "CoreMLExecutionProvider" in providers:
        return _result(
            BACKEND_COREML,
            "ONNX Runtime CoreML EP available (ANE/Metal via Apple Silicon)",
        )
    if apple and probe("mlx"):
        return _result(BACKEND_MLX, "MLX/Metal available on Apple Silicon")
    if providers:
        return _result(BACKEND_ONNX_CPU, "ONNX Runtime CPU execution provider")
    return _result(
        BACKEND_NONE,
        "no ML runtime installed; install extras: uv sync --extra ml (ONNX) "
        "or --extra mlx (Apple Silicon)",
    )


def _result(backend: str, reason: str) -> dict:
    return {"backend": backend, "reason": reason, "cuda": False}


def detect_posture(settings=None, *, probe: Callable[[str], bool] = _probe) -> str:
    """Return ``api-first`` or ``local`` deployment posture.

    ``EV_ML_POSTURE`` wins when set; otherwise the presence of the ``mlx``
    package (trainer extra) selects ``local``.
    """

    from app.ml.settings import get_ml_settings

    ml_settings = settings or get_ml_settings()
    if ml_settings.ml_posture in ("api-first", "local"):
        return ml_settings.ml_posture
    return "local" if probe("mlx") else "api-first"


def backend_for_model(model_name: str, preferred: str | None = None) -> dict:
    result = select_backend(preferred=preferred)
    result["model"] = model_name
    return result
