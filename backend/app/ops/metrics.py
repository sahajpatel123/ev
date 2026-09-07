"""Aggregate operational metrics: latency, cost, system resources, arbiter."""

from __future__ import annotations

import json
import re
import shutil
import statistics
import subprocess
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ModelCallLog
from app.ops.budgets import (
    LATENCY_BUDGETS_MS,
    MODEL_PRICES_USD_PER_1M,
    MONTHLY_COST_BUDGET_USD,
)
from app.utils.text import utcnow

METRICS_WINDOW_DAYS = 30
RESTORE_DRILL_STALE_DAYS = 35


def _restore_drill_marker() -> Path:
    return Path(settings.storage_root) / "ops" / "restore-drill.json"


def record_restore_drill(*, now=None) -> dict:
    """Persist the last successful wipe→restore drill timestamp."""

    timestamp = now or utcnow()
    marker = _restore_drill_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_success_at": timestamp.isoformat(timespec="seconds")}
    marker.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return payload


def restore_drill_age() -> dict:
    """Age of the last successful restore drill, with a stale alert past 35 days."""

    marker = _restore_drill_marker()
    if not marker.is_file():
        return {
            "last_success_at": None,
            "age_days": None,
            "stale": True,
            "alert": "No restore drill on record; run a wipe→restore drill now.",
        }
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        last = payload.get("last_success_at")
        from datetime import datetime

        parsed = datetime.fromisoformat(last)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=utcnow().tzinfo)
        age_days = round((utcnow() - parsed).total_seconds() / 86400.0, 1)
        return {
            "last_success_at": last,
            "age_days": age_days,
            "stale": age_days > RESTORE_DRILL_STALE_DAYS,
            "alert": (
                f"Last restore drill was {age_days:.0f} days ago "
                f"(limit {RESTORE_DRILL_STALE_DAYS}); run one now."
                if age_days > RESTORE_DRILL_STALE_DAYS
                else None
            ),
        }
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "last_success_at": None,
            "age_days": None,
            "stale": True,
            "alert": "Restore-drill marker is unreadable; run a drill to rewrite it.",
        }


def _swap_usage() -> dict | None:
    """Swap usage in MB via ``sysctl vm.swapusage`` (macOS) or /proc/meminfo."""

    if sys.platform == "darwin":
        try:
            output = subprocess.run(
                ["sysctl", "vm.swapusage"],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        values: dict[str, float] = {}
        for part in output.replace("vm.swapusage:", "").split(","):
            key, _, value = part.strip().partition("=")
            try:
                values[key.strip()] = float(value.strip().split()[0])
            except (ValueError, IndexError):
                continue
        if not values:
            return None
        return {
            "total_mb": round(values.get("total", 0.0), 1),
            "used_mb": round(values.get("used", 0.0), 1),
            "free_mb": round(values.get("free", 0.0), 1),
        }
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            meminfo = handle.read()
    except OSError:
        return None
    total: float | None = None
    free: float | None = None
    for line in meminfo.splitlines():
        if line.startswith("SwapTotal:"):
            total = float(line.split()[1]) / 1024.0
        elif line.startswith("SwapFree:"):
            free = float(line.split()[1]) / 1024.0
    if total is None or free is None:
        return None
    used = total - free
    return {
        "total_mb": round(total, 1),
        "used_mb": round(used, 1),
        "free_mb": round(free, 1),
    }


def _free_ram_mb() -> float | None:
    """Free + inactive memory (reclaimable) in MB via vm_stat (macOS)."""

    if sys.platform != "darwin":
        try:
            with open("/proc/meminfo", encoding="utf-8") as handle:
                meminfo = handle.read()
            for line in meminfo.splitlines():
                if line.startswith("MemAvailable:"):
                    return round(float(line.split()[1]) / 1024.0, 1)
        except (OSError, ValueError):
            return None
        return None
    try:
        output = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    page_size = 16384
    match = re.search(r"page size of (\d+) bytes", output)
    if match:
        page_size = int(match.group(1))
    values: dict[str, int] = {}
    for line in output.splitlines():
        key, _, value = line.partition(":")
        value = value.strip().rstrip(".")
        try:
            values[key.strip()] = int(value)
        except ValueError:
            continue
    free = values.get("Pages free", 0)
    inactive = values.get("Pages inactive", 0)
    speculative = values.get("Pages speculative", 0)
    return round((free + inactive + speculative) * page_size / (1024 * 1024), 1)


def _free_disk_gb(path: Path) -> float | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(path)
        return round(usage.free / (1024**3), 2)
    except OSError:
        return None


def _stack_rss_mb() -> float | None:
    """Total RSS of the EV native stack processes (API, workers, db, redis)."""

    try:
        output = subprocess.run(
            ["ps", "-axo", "rss=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    markers = (
        "app.main",
        "app.workers.runner",
        "app.workers.scheduler",
        "app.workers.runtime_daemon",
        "clients.ears",
        "clients.collectors",
        "redis-server",
        "postgres",
    )
    total_kb = 0.0
    for line in output.splitlines():
        try:
            rss_kb, command = line.strip().split(" ", 1)
        except ValueError:
            continue
        if any(marker in command for marker in markers):
            try:
                total_kb += float(rss_kb)
            except ValueError:
                continue
    return round(total_kb / 1024.0, 1) if total_kb else None


def _arbiter_metrics() -> dict:
    """Process-wide arbiter stats plus evictions observed by this module."""

    global _arbiter_evictions, _arbiter_last_resident
    try:
        from app.audio.models import model_arbiter
    except Exception:  # noqa: BLE001 - observability must not crash the API
        return {
            "available": False,
            "resident_mb": {},
            "resident_total_mb": None,
            "refusals_last_50": [],
            "evictions_observed": _arbiter_evictions,
            "ceiling_mb": None,
        }
    stats = model_arbiter().stats()
    resident_mb = {
        str(model["name"]): int(model["resident_mb"])
        for model in stats.get("models", [])
    }
    current_names = set(resident_mb)
    if _arbiter_last_resident is not None:
        _arbiter_evictions += len(_arbiter_last_resident - current_names)
    _arbiter_last_resident = current_names
    return {
        "available": True,
        "resident_mb": resident_mb,
        "resident_total_mb": stats.get("resident_total_mb"),
        "resident_by_tier_mb": stats.get("resident_by_tier_mb", {}),
        "ceiling_mb": stats.get("ceiling_mb"),
        "exclusive_holder": stats.get("exclusive_holder"),
        "refusals_last_50": stats.get("refusals_last_50", []),
        "evictions_observed": _arbiter_evictions,
        "free_disk_gb": stats.get("free_disk_gb"),
    }


_arbiter_evictions = 0
_arbiter_last_resident: set[str] | None = None


def _per_model_p95(rows: list) -> dict[str, dict]:
    by_model: dict[str, list[float]] = {}
    for row in rows:
        if row.status == "ok" and row.model and row.latency_ms is not None:
            by_model.setdefault(row.model, []).append(row.latency_ms)
    return {
        model: {
            "count": len(latencies),
            "p95_ms": _percentile(latencies, 95),
            "p50_ms": _percentile(latencies, 50),
        }
        for model, latencies in sorted(by_model.items())
    }


def estimate_cost_usd(
    *,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Estimate the provider cost of one model call from token usage."""

    price = MODEL_PRICES_USD_PER_1M.get(provider) or MODEL_PRICES_USD_PER_1M["default"]
    return round(
        (prompt_tokens * price["input"] + completion_tokens * price["output"])
        / 1_000_000,
        6,
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((percentile / 100.0) * (len(ordered) - 1))))
    return round(ordered[index], 1)


def _ensure_aware(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=utcnow().tzinfo)
    return value


async def collect_metrics(
    session: AsyncSession,
    *,
    limit: int = 10_000,
) -> dict:
    """Aggregate model-call latency, error, and estimated-cost metrics.

    The report is intentionally cheap and personal-scale: it scans the last
    ``limit`` audit rows and computes percentiles/cost estimates in memory.
    """

    result = await session.execute(
        select(ModelCallLog).order_by(ModelCallLog.created_at.desc()).limit(limit)
    )
    rows = list(result.scalars().all())
    now = utcnow()
    cutoff = now - timedelta(days=METRICS_WINDOW_DAYS)

    ok_rows = [r for r in rows if r.status == "ok"]
    latencies = [r.latency_ms for r in ok_rows if r.latency_ms is not None]

    by_status = dict(Counter(r.status for r in rows))
    by_provider = dict(Counter(r.provider for r in rows))
    prompt_tokens = sum(r.prompt_tokens or 0 for r in rows)
    completion_tokens = sum(r.completion_tokens or 0 for r in rows)

    cost_by_provider: dict[str, float] = {}
    for row in rows:
        cost_by_provider[row.provider] = cost_by_provider.get(row.provider, 0.0) + (
            estimate_cost_usd(
                provider=row.provider,
                prompt_tokens=row.prompt_tokens or 0,
                completion_tokens=row.completion_tokens or 0,
            )
        )

    window_rows = [
        r
        for r in rows
        if (created := _ensure_aware(r.created_at)) is not None and created >= cutoff
    ]
    window_cost = sum(
        estimate_cost_usd(
            provider=r.provider,
            prompt_tokens=r.prompt_tokens or 0,
            completion_tokens=r.completion_tokens or 0,
        )
        for r in window_rows
    )
    total_cost = sum(cost_by_provider.values())
    p95 = _percentile(latencies, 95)
    storage_root = Path(settings.storage_root)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_days": METRICS_WINDOW_DAYS,
        "latency": {
            "count": len(latencies),
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "max_ms": round(max(latencies), 1) if latencies else None,
            "mean_ms": round(statistics.fmean(latencies), 1) if latencies else None,
            "budgets_ms": LATENCY_BUDGETS_MS,
            "within_budget_p95": p95 is not None and p95 <= LATENCY_BUDGETS_MS["chat_first_token"],
        },
        "cost": {
            "total_usd": round(total_cost, 6),
            "last_30d_usd": round(window_cost, 6),
            "monthly_budget_usd": MONTHLY_COST_BUDGET_USD,
            "within_budget": window_cost <= MONTHLY_COST_BUDGET_USD,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "by_provider": {
                provider: round(value, 6) for provider, value in cost_by_provider.items()
            },
        },
        "calls": {
            "total": len(rows),
            "ok": len(ok_rows),
            "errors": len(rows) - len(ok_rows),
            "by_status": by_status,
            "by_provider": by_provider,
            "latency_by_model": _per_model_p95(rows),
        },
        "system": {
            "free_ram_mb": _free_ram_mb(),
            "free_disk_gb": _free_disk_gb(storage_root),
            "swap": _swap_usage(),
            "stack_rss_mb": _stack_rss_mb(),
        },
        "arbiter": _arbiter_metrics(),
        "restore_drill": restore_drill_age(),
        "warnings": [],
        "stale": False,
    }


async def collect_metrics_with_warnings(
    session: AsyncSession,
    *,
    limit: int = 10_000,
) -> dict:
    """collect_metrics plus a derived warnings list for the ops surface."""

    report = await collect_metrics(session, limit=limit)
    warnings: list[str] = []
    drill = report.get("restore_drill") or {}
    if drill.get("stale") and drill.get("alert"):
        warnings.append(str(drill["alert"]))
    system = report.get("system") or {}
    free_ram = system.get("free_ram_mb")
    if free_ram is not None and free_ram < 2048:
        warnings.append(
            f"Only {free_ram:.0f} MB of RAM is reclaimable; EV will feel slow "
            "and macOS may swap."
        )
    free_disk = system.get("free_disk_gb")
    if free_disk is not None and free_disk < 5:
        warnings.append(
            f"Only {free_disk:.1f} GB free disk; model downloads are refused "
            "below 5 GB (run make prune)."
        )
    swap = system.get("swap") or {}
    if swap.get("used_mb") and swap["used_mb"] > 512:
        warnings.append(
            f"Swap in use: {swap['used_mb']:.0f} MB; RAM pressure is degrading "
            "latency."
        )
    report["warnings"] = warnings
    report["stale"] = bool(warnings)
    return report
