"""Private Evie Mobile release portal (tailnet-only; funnel stays OFF).

Serves signed Ad Hoc IPAs + OTA manifests for the two registered iPhones.
Lives behind the same boundary as the rest of the backend: bound to
127.0.0.1, exposed only via Tailscale Serve HTTPS (publicly trusted cert,
so iOS itms-services installation works from the tailnet).

Layout on disk (backend/storage/releases/):
  releases/
    canary/release.json   canary/Evie.ipa
    stable/release.json   stable/Evie.ipa
    archive/<channel>/<native_build>/...   bounded history (B34)

Channels:
  CANARY — latest CI build approved for physical testing.
  STABLE — last OWNER-VERIFIED build. A new commit NEVER auto-replaces it.

OTA: Apple's standard itms-services manifest generated per channel at
request time with an absolute https URL (B15/B18).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

from app.config import settings

router = APIRouter(prefix="/evie-install", tags=["evie-install"])

RELEASES_ROOT = Path(settings.storage_root) / "releases"
MAX_IPA_BYTES = 512 * 1024 * 1024


def _channel_dir(channel: str) -> Path:
    if channel not in {"canary", "stable"}:
        raise HTTPException(status_code=404, detail="Unknown channel")
    return RELEASES_ROOT / channel


def _load(channel: str) -> dict[str, Any]:
    path = _channel_dir(channel) / "release.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail=f"No {channel} release published") from None


def _base_url(request: Request) -> str:
    """Public tailnet HTTPS origin, as the phone sees it."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@router.get("/")
async def portal_index(request: Request) -> PlainTextResponse:
    """One screen, no developer jargon (B16)."""
    lines = ["EVIE MOBILE", "", "Stable"]
    try:
        s = _load("stable")
        lines += [f"Version {s['app_version']} · Build {s['native_build']}", "/evie-install/stable/install", ""]
    except HTTPException:
        lines += ["(none yet)", ""]
    lines.append("Canary")
    try:
        c = _load("canary")
        lines += [f"Version {c['app_version']} · Build {c['native_build']}", "/evie-install/canary/install", ""]
    except HTTPException:
        lines += ["(none yet)", ""]
    lines += [
        "Install: open a channel link on your iPhone and tap Install.",
        "Private: this page is reachable only inside your tailnet.",
    ]
    return PlainTextResponse("\n".join(lines))


@router.get("/manifest")
async def releases_manifest(request: Request) -> dict[str, Any]:
    """Machine-readable channel state for the native update UX (B23)."""
    out: dict[str, Any] = {"channels": {}}
    for ch in ("stable", "canary"):
        try:
            rel = _load(ch)
        except HTTPException:
            continue
        out["channels"][ch] = {
            "app_version": rel.get("app_version"),
            "native_build": int(rel.get("native_build", 0)),
            "commit": rel.get("commit"),
            "web_core_build": rel.get("web_core_build"),
            "ipa_sha256": rel.get("ipa_sha256"),
            "created_at": rel.get("created_at"),
            "install_url": f"{_base_url(request)}/evie-install/{ch}/install",
        }
    out["broker_protocol"] = 1
    return out


@router.get("/{channel}/install")
async def ota_manifest(channel: str, request: Request) -> Response:
    """itms-services plist. iOS fetches this, then downloads the IPA."""
    rel = _load(channel)
    base = _base_url(request)
    title = f"Evie ({channel.capitalize()})"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>items</key>
  <array>
    <dict>
      <key>assets</key>
      <array>
        <dict>
          <key>kind</key><string>software-package</string>
          <key>url</key><string>{base}/evie-install/{channel}/Evie.ipa</string>
        </dict>
      </array>
      <key>metadata</key>
      <dict>
        <key>bundle-identifier</key><string>com.ev.evie.shell</string>
        <key>bundle-version</key><string>{rel.get('native_build', '1')}</string>
        <key>kind</key><string>software</string>
        <key>title</key><string>{title}</string>
      </dict>
    </dict>
  </array>
</dict>
</plist>
"""
    return Response(
        content=plist,
        media_type="application/xml",
        headers={"Content-Disposition": f'inline; filename="evie-{channel}.plist"'},
    )


@router.get("/{channel}/Evie.ipa")
async def download_ipa(channel: str) -> FileResponse:
    rel = _load(channel)
    ipa = _channel_dir(channel) / "Evie.ipa"
    if not ipa.is_file():
        raise HTTPException(status_code=404, detail="IPA missing from release store")
    return FileResponse(
        ipa,
        media_type="application/octet-stream",
        filename="Evie.ipa",
        headers={"X-Ipa-Sha256": str(rel.get("ipa_sha256", ""))},
    )


# ---------------------------------------------------------------------------
# Publishing (CI calls these with the master key; never exposed publicly)
# ---------------------------------------------------------------------------


def publish_release(channel: str, source_dir: Path) -> dict[str, Any]:
    """Atomically promote a verified artifact directory into a channel."""
    if channel not in {"canary", "stable"}:
        raise ValueError("channel must be canary or stable")
    meta_path = source_dir / "release.json"
    ipa_path = source_dir / "Evie.ipa"
    if not meta_path.is_file() or not ipa_path.is_file():
        raise ValueError("source dir needs release.json + Evie.ipa (run verify first)")
    meta = json.loads(meta_path.read_text())
    ipa_bytes = ipa_path.read_bytes()
    if len(ipa_bytes) > MAX_IPA_BYTES:
        raise ValueError("IPA exceeds size cap")
    declared = str(meta.get("ipa_sha256") or "")
    actual = hashlib.sha256(ipa_bytes).hexdigest()
    if not declared or declared != actual:
        raise ValueError(
            f"checksum mismatch: manifest says {declared[:16]}…, artifact is {actual[:16]}…"
        )

    dest = _channel_dir(channel)
    staging = dest.with_suffix(".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copy2(meta_path, staging / "release.json")
    shutil.copy2(ipa_path, staging / "Evie.ipa")

    # Keep previous stable in bounded history before overwrite (B21/B34).
    if dest.exists() and (dest / "release.json").is_file():
        prev = _safe_prev_build(dest)
        if prev:
            archive = RELEASES_ROOT / "archive" / channel / prev
            archive.parent.mkdir(parents=True, exist_ok=True)
            if not archive.exists():
                shutil.copytree(dest, archive)
    if dest.exists():
        shutil.rmtree(dest)
    staging.rename(dest)
    _prune_archive(channel)
    return meta


def _safe_prev_build(dest: Path) -> str | None:
    try:
        return str(json.loads((dest / "release.json").read_text()).get("native_build")) or None
    except (OSError, ValueError, TypeError):
        return None


def _prune_archive(channel: str, keep: int = 3) -> None:
    archive = RELEASES_ROOT / "archive" / channel
    if not archive.is_dir():
        return
    builds = sorted((p for p in archive.iterdir() if p.is_dir()), key=lambda p: p.name)
    for old in builds[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def promote_canary_to_stable(expected_build: str | None = None) -> dict[str, Any]:
    """Promote the EXACT tested artifact (no rebuild) after owner approval (B20)."""
    canary_meta = _load("canary")
    if expected_build and str(canary_meta.get("native_build")) != str(expected_build):
        raise ValueError(
            f"canary is build {canary_meta.get('native_build')}, expected {expected_build}"
        )
    return publish_release("stable", _channel_dir("canary"))


# ---------------------------------------------------------------------------
# CI endpoints — master-key only, upload + promote (B39/B40)
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402

from fastapi import Depends, File, UploadFile  # noqa: E402

from app.auth import require_master  # noqa: E402


@router.post("/admin/publish")
async def admin_publish(
    channel: str,
    release_json: UploadFile = File(...),
    ipa: UploadFile = File(...),
    _master: str = Depends(require_master),
) -> dict[str, Any]:
    """Publish a VERIFIED artifact to a channel. Rejects on any mismatch."""
    if channel not in {"canary", "stable"}:
        raise HTTPException(status_code=400, detail="channel must be canary|stable")
    try:
        meta = json.loads((await release_json.read()).decode())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid release.json") from None
    ipa_bytes = await ipa.read()
    if len(ipa_bytes) > MAX_IPA_BYTES:
        raise HTTPException(status_code=413, detail="IPA too large")
    declared = str(meta.get("ipa_sha256") or "")
    actual = hashlib.sha256(ipa_bytes).hexdigest()
    if not declared or declared != actual:
        raise HTTPException(
            status_code=400,
            detail=f"checksum mismatch: manifest says {declared[:16]}…, upload is {actual[:16]}…",
        )
    staging = RELEASES_ROOT / "staging" / channel
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "release.json").write_text(json.dumps(meta, indent=2) + "\n")
    (staging / "Evie.ipa").write_bytes(ipa_bytes)
    published = publish_release(channel, staging)
    shutil.rmtree(staging, ignore_errors=True)
    return {"ok": True, "channel": channel, "published": published}


@router.post("/admin/promote")
async def admin_promote(
    from_build: str | None = None,
    _master: str = Depends(require_master),
) -> dict[str, Any]:
    """Promote current canary artifact to stable. Owner-approved builds only."""
    try:
        published = promote_canary_to_stable(from_build)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"ok": True, "stable": published}
