"""Evie fabric HTTP surface (WP1, additive).

Thin adapters only: approvals delegate to the runtime action machinery,
effects dispatch through the policy-gated fabric lanes, and nothing here
invents a second engine, ticket table, or approval flow.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor, require_actor_context, require_master, require_owner_trust
from app.db import get_session
from app.models import ApprovedAction
from app.schemas import ActionDecisionRequest, ApprovedActionOut

router = APIRouter(prefix="/v1/fabric", tags=["fabric"])

_FABRIC_ACTION_TYPES = frozenset({
    "shell_run", "shell_session", "terminal_layout", "screen_ref",
    "sql_query", "pim_act", "builder_job", "atlas_discover",
})

_ATTACH_MAX_BYTES = 20 * 1024 * 1024
_ATTACH_EXTS = frozenset({".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp"})


def _attachments_root() -> Path:
    d = Path.home() / ".ev" / "fabric_attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- models ---


class StatusOut(BaseModel):
    fabric_enabled: bool
    contract: str
    lanes: list[str]
    panic: dict[str, Any]
    recent_turns: list[dict[str, Any]]


class PendingOut(BaseModel):
    ok: bool = True
    count: int
    approvals: list[ApprovedActionOut]


class AttachIn(BaseModel):
    filename: str = Field(min_length=1, max_length=128)
    content_base64: str = Field(min_length=1)
    turn_id: str | None = Field(default=None, max_length=64)
    kind: str = Field(default="screenshot", max_length=32)


class CancelIn(BaseModel):
    turn_id: str | None = Field(default=None, max_length=64)


class PanicIn(BaseModel):
    reason: str = Field(default="", max_length=280)


class LayoutSaveIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    panes: list[dict[str, Any]] = Field(min_length=1)


class LayoutRestoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class UndoRestoreIn(BaseModel):
    id: str = Field(min_length=1, max_length=256)


# --- status ---


@router.get("/status", response_model=StatusOut)
async def fabric_status(actor: str = Depends(require_actor)) -> StatusOut:
    from app.fabric import panic as panic_mod
    from app.fabric import trace
    from app.fabric.facade import fabric_enabled
    from app.fabric.policy import contract_version

    del actor
    return StatusOut(
        fabric_enabled=fabric_enabled(),
        contract=contract_version(),
        lanes=sorted(_FABRIC_ACTION_TYPES),
        panic=panic_mod.panic_state(),
        recent_turns=trace.recent_entries(),
    )


# --- approvals (delegated to the runtime action machinery) ---


@router.get("/approvals/pending", response_model=PendingOut)
async def fabric_pending(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PendingOut:
    del actor
    stmt = (
        select(ApprovedAction)
        .where(
            ApprovedAction.status == "pending",
            ApprovedAction.action_type.in_(sorted(_FABRIC_ACTION_TYPES)),
        )
        .order_by(ApprovedAction.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return PendingOut(
        count=len(rows), approvals=[ApprovedActionOut.model_validate(r) for r in rows]
    )


async def _fabric_ticket_or_404(session: AsyncSession, action_id: UUID) -> ApprovedAction:
    action = await session.get(ApprovedAction, action_id)
    if action is None or action.action_type not in _FABRIC_ACTION_TYPES:
        raise HTTPException(status_code=404, detail="fabric approval not found")
    return action


@router.post("/approvals/{action_id}/approve", response_model=ApprovedActionOut)
async def fabric_approve(
    action_id: UUID,
    data: ActionDecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    ctx=Depends(require_actor_context),
    x_ev_reverify: str | None = Header(default=None, alias="X-EV-Reverify"),
) -> ApprovedActionOut:
    from app.api.runtime import approve_action as runtime_approve

    await _fabric_ticket_or_404(session, action_id)
    return await runtime_approve(action_id, data, session, ctx, x_ev_reverify)


@router.post("/approvals/{action_id}/deny", response_model=ApprovedActionOut)
async def fabric_deny(
    action_id: UUID,
    data: ActionDecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    ctx=Depends(require_owner_trust),
) -> ApprovedActionOut:
    from app.api.runtime import deny_action as runtime_deny

    await _fabric_ticket_or_404(session, action_id)
    return await runtime_deny(action_id, data, session, ctx)


# --- attach ---


@router.post("/attach")
async def fabric_attach(
    data: AttachIn,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict[str, Any]:
    suffix = Path(data.filename).suffix.lower()
    if suffix not in _ATTACH_EXTS:
        raise HTTPException(status_code=422, detail=f"unsupported image type '{suffix}'")
    try:
        raw = base64.b64decode(data.content_base64, validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="invalid base64 content") from None
    if len(raw) > _ATTACH_MAX_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large")
    digest = hashlib.sha256(raw).hexdigest()
    folder = _attachments_root() / (data.turn_id or "unbound")
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in Path(data.filename).name if c.isalnum() or c in "._-")[:80] or "image"
    path = folder / f"{digest[:12]}_{safe}"
    path.write_bytes(raw)
    back = path.read_bytes()
    if hashlib.sha256(back).hexdigest() != digest:
        raise HTTPException(status_code=500, detail="attachment read-back mismatch")
    from app.schemas import EventCreate
    from app.services.event_service import EventService

    service = EventService(session, actor=actor)
    event = await service.create(
        EventCreate(
            source="fabric",
            event_type="image.attached",
            text=f"{data.kind}: {safe}",
            content={"path": str(path), "sha256": digest, "turn_id": data.turn_id,
                     "kind": data.kind},
        )
    )
    await session.commit()
    return {"ok": True, "artifact_id": str(event.id), "path": str(path),
            "sha256": digest, "bytes": len(raw)}


# --- cancel ---


@router.post("/cancel")
async def fabric_cancel(
    data: CancelIn,
    actor: str = Depends(require_actor),
) -> dict[str, Any]:
    from app.fabric import cancel as cancel_mod

    del actor
    if data.turn_id:
        return cancel_mod.cancel_turn(data.turn_id)
    return cancel_mod.cancel_all()


# --- permissions / environment probes ---


def _probe(name: str, kind: str, check, fix: str) -> dict[str, Any]:
    try:
        status, detail = check()
    except Exception as exc:
        status, detail = "unknown", f"probe failed: {type(exc).__name__}"
    entry: dict[str, Any] = {"permission": name, "kind": kind, "status": status,
                             "detail": detail}
    if status != "granted":
        entry["fix"] = fix
    return entry


def _which(tool: str):
    def check():
        found = shutil.which(tool)
        return ("granted", found or "") if found else ("denied", f"{tool} not on PATH")

    return check


@router.get("/permissions")
async def fabric_permissions(actor: str = Depends(require_actor)) -> dict[str, Any]:
    from app.fabric import terminal

    del actor

    def _tmux_session() -> tuple[str, str]:
        if not terminal.has_tmux():
            return "denied", "tmux not installed"
        try:
            p = subprocess.run(["tmux", "-V"], capture_output=True, text=True, timeout=5)
            return "granted", (p.stdout or "").strip()[:80]
        except Exception as exc:
            return "unknown", str(exc)[:120]

    def _ghostty() -> tuple[str, str]:
        probed = terminal.probe_ghostty()["evidence"]
        if not probed.get("bundle_present"):
            return "denied", "Ghostty.app not installed"
        return "granted", str(probed.get("version") or "installed")[:120]

    def _tailscale() -> tuple[str, str]:
        cli = shutil.which("tailscale")
        app_cli = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        binary = cli or (app_cli if Path(app_cli).exists() else None)
        if binary is None:
            return "denied", "Tailscale not installed"
        try:
            p = subprocess.run([binary, "status", "--json=false"], capture_output=True,
                               text=True, timeout=10)
            out = ((p.stdout or "") + (p.stderr or "")).strip()[:200]
            if "stopped" in out.lower():
                return "denied", "Tailscale is stopped"
            return "granted", out or "running"
        except Exception as exc:
            return "unknown", str(exc)[:120]

    def _evie_app() -> tuple[str, str]:
        bundle = Path("/Applications/Evie.app")
        if not bundle.exists():
            return "denied", "Evie.app not installed"
        try:
            p = subprocess.run(["codesign", "-dvv", str(bundle)], capture_output=True,
                               text=True, timeout=10)
            ident = ""
            for line in ((p.stderr or "") + (p.stdout or "")).splitlines():
                if line.startswith("Authority="):
                    ident = line
                    break
            return "granted", ident[:120] or "installed"
        except Exception as exc:
            return "unknown", str(exc)[:120]

    def _fda_hint() -> tuple[str, str]:
        # Prompt-free read check: opening a TCC-protected file without rights
        # returns EPERM (no dialog). Readable Messages db strongly implies FDA.
        candidates = [Path.home() / "Library/Messages/chat.db",
                      Path.home() / "Library/Mail/V10/MailData/Envelope Index"]
        for candidate in candidates:
            try:
                with open(candidate, "rb") as fh:
                    fh.read(1)
                return "granted", f"readable: {candidate}"
            except FileNotFoundError:
                continue
            except OSError:
                return "denied", f"unreadable (needs Full Disk Access): {candidate}"
        return "unknown", "no TCC-protected file found to test"

    tcc_fix = "Run `ev setup` and grant it in System Settings when prompted."
    entries = [
        _probe("tmux", "tool", _tmux_session, "Install tmux: `brew install tmux`."),
        _probe("ghostty", "tool", _ghostty, "Install Ghostty from ghostty.org."),
        _probe("tailscale", "tool", _tailscale,
               "Install Tailscale, sign in, then `ev setup phone`."),
        _probe("docker", "tool", _which("docker"), "Install Docker Desktop (optional; scratch Postgres)."),
        _probe("osascript", "tool", _which("osascript"), "Install Xcode Command Line Tools."),
        _probe("evie.app", "app", _evie_app, "Reinstall Evie.app, then re-grant TCC items."),
        _probe("full_disk_access", "tcc", _fda_hint,
               "System Settings > Privacy & Security > Full Disk Access > Evie."),
        _probe("accessibility", "tcc", lambda: ("unknown", "interactive check only"), tcc_fix),
        _probe("automation", "tcc", lambda: ("unknown", "interactive check only"), tcc_fix),
        _probe("screen_recording", "tcc", lambda: ("unknown", "interactive check only"), tcc_fix),
        _probe("microphone", "tcc", lambda: ("unknown", "interactive check only"), tcc_fix),
        _probe("reminders", "tcc", lambda: ("unknown", "interactive check only"), tcc_fix),
        _probe("calendars", "tcc", lambda: ("unknown", "interactive check only"), tcc_fix),
        _probe("contacts", "tcc", lambda: ("unknown", "interactive check only"), tcc_fix),
    ]
    return {"ok": True, "entries": entries}


# --- undo ---


def _undo_entries() -> list[dict[str, Any]]:
    import json as _json

    from app.fabric.roots import default_roots

    out: list[dict[str, Any]] = []
    for root in default_roots():
        for sub, kind in ((".ev-undo", "snapshot"), (".ev-trash", "trash")):
            base = root / sub
            if not base.is_dir():
                continue
            for sidecar in sorted(base.glob("*.info.json")):
                try:
                    info = _json.loads(sidecar.read_text())
                    rel = str(info.get("rel") or "")
                except (OSError, ValueError):
                    continue
                target = sidecar.name[: -len(".info.json")]
                if not (base / target).exists():
                    continue
                out.append({"id": f"{root}::{sidecar.name}", "root": str(root),
                            "rel": rel, "kind": kind,
                            "at": sidecar.stat().st_mtime})
    return sorted(out, key=lambda row: row["at"], reverse=True)


@router.get("/undo")
async def fabric_undo_list(actor: str = Depends(require_actor)) -> dict[str, Any]:
    del actor
    entries = _undo_entries()
    return {"ok": True, "entries": entries, "count": len(entries)}


@router.post("/undo/restore")
async def fabric_undo_restore(
    data: UndoRestoreIn,
    actor: str = Depends(require_actor),
) -> dict[str, Any]:
    import json as _json

    from app.fabric import files as fabric_files

    del actor
    if "::" not in data.id:
        raise HTTPException(status_code=422, detail="bad undo id")
    root_s, sidecar_name = data.id.split("::", 1)
    if "/" in sidecar_name or sidecar_name.startswith("."):
        raise HTTPException(status_code=422, detail="bad undo id")
    root = Path(root_s)
    sidecar = None
    kind = None
    for sub, subkind in ((".ev-undo", "snapshot"), (".ev-trash", "trash")):
        candidate = root / sub / sidecar_name
        try:
            # Containment: the sidecar must resolve inside the undo/trash dir.
            candidate.resolve().relative_to((root / sub).resolve())
        except (OSError, ValueError):
            continue
        if candidate.is_file():
            sidecar, kind = candidate, subkind
            break
    if sidecar is None or kind is None:
        raise HTTPException(status_code=404, detail="undo entry not found")
    try:
        info = _json.loads(sidecar.read_text())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="undo entry unreadable") from None
    target = sidecar.parent / sidecar.name[: -len(".info.json")]
    undo_info = ({"snapshot": str(target), "rel": str(info.get("rel") or "")}
                 if kind == "snapshot"
                 else {"trash": str(target), "rel": str(info.get("rel") or "")})
    receipt = fabric_files.undo(undo_info, root=root)
    if not receipt.get("ok"):
        raise HTTPException(status_code=409, detail=str(receipt.get("error") or "restore failed"))
    with_time = dict(receipt)
    with_time["restored_at"] = time.time()
    return with_time


# --- layouts ---


@router.get("/layouts")
async def fabric_layouts(actor: str = Depends(require_actor)) -> dict[str, Any]:
    from app.fabric import terminal
    from app.fabric.roots import lane_root

    del actor
    return terminal.list_layouts(root=lane_root())


@router.post("/layouts/save")
async def fabric_layout_save(
    data: LayoutSaveIn,
    actor: str = Depends(require_actor),
) -> dict[str, Any]:
    from app.fabric import terminal
    from app.fabric.roots import lane_root

    del actor
    receipt = terminal.save_layout(data.name, {"panes": data.panes}, root=lane_root())
    if not receipt.get("ok"):
        raise HTTPException(status_code=422, detail=str(receipt.get("error") or "save failed"))
    return receipt


@router.post("/layouts/restore")
async def fabric_layout_restore(
    data: LayoutRestoreIn,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict[str, Any]:
    from app.ev.tools import dispatch

    response = await dispatch(
        session, "terminal_layout", {"op": "restore", "name": data.name},
        actor=actor, allow_sensitive=True, audit_endpoint="POST /v1/fabric/layouts/restore",
    )
    await session.commit()
    body = response.result if isinstance(response.result, dict) else {}
    return {"ok": response.ok and bool(body.get("ok")), "envelope_ok": response.ok,
            "error": response.error or body.get("error"), "result": body}


# --- panic (D10) ---


@router.get("/panic")
async def fabric_panic_state(actor: str = Depends(require_actor)) -> dict[str, Any]:
    from app.fabric import panic as panic_mod

    del actor
    return {"ok": True, **panic_mod.panic_state()}


@router.post("/panic")
async def fabric_panic(
    data: PanicIn,
    _: str = Depends(require_master),
) -> dict[str, Any]:
    from app.fabric import panic as panic_mod

    return panic_mod.panic(data.reason or "owner panic via API")


@router.post("/panic/clear")
async def fabric_panic_clear(_: str = Depends(require_master)) -> dict[str, Any]:
    from app.fabric import panic as panic_mod

    return panic_mod.clear()
