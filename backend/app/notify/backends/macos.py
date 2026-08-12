"""macOS native notifications via a small UNUserNotificationCenter helper.

The helper (Swift, no third-party packages) is built once into
``EV_NOTIFY_MACOS_BUILD_DIR`` as a signed .app bundle so
``UNUserNotificationCenter`` has a bundle identity. When the helper cannot
reach the notification daemon (sandbox, unsigned context), terminal-notifier
is tried, then ``osascript display notification`` when
``EV_NOTIFY_MACOS_ALLOW_OSASCRIPT=true``; the interim limitations are recorded
in docs/RUNTIME.md.
"""

from __future__ import annotations

import asyncio
import plistlib
import shutil
import subprocess
from pathlib import Path

from app.config import settings
from app.notify.models import DeliveryReceipt, NotificationRecord, NotifierError

HELPER_SOURCE = Path(__file__).resolve().parent.parent / "macos" / "EVNotificationHelper.swift"


class MacOSNotifier:
    name = "macos"

    def _default_target(self, build_dir: Path) -> Path:
        return (
            build_dir
            / "EVNotificationHelper.app"
            / "Contents"
            / "MacOS"
            / "EVNotificationHelper"
        )

    def _helper_path(self) -> Path | None:
        if settings.notify_macos_helper_path:
            path = Path(settings.notify_macos_helper_path).expanduser()
            if path.is_file():
                return path
        build_dir = Path(settings.notify_macos_build_dir).expanduser()
        built = self._default_target(build_dir)
        return built if built.is_file() else None

    def _build_helper(self) -> Path:
        if not HELPER_SOURCE.is_file():
            raise NotifierError(
                f"macos helper source missing at {HELPER_SOURCE}; cannot build"
            )
        build_dir = Path(settings.notify_macos_build_dir).expanduser()
        build_dir.mkdir(parents=True, exist_ok=True)
        app_dir = build_dir / "EVNotificationHelper.app"
        contents_dir = app_dir / "Contents"
        macos_dir = contents_dir / "MacOS"
        macos_dir.mkdir(parents=True, exist_ok=True)
        target = macos_dir / "EVNotificationHelper"
        result = subprocess.run(
            [
                "swiftc",
                "-O",
                "-framework",
                "Foundation",
                "-framework",
                "UserNotifications",
                "-o",
                str(target),
                str(HELPER_SOURCE),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0 or not target.is_file():
            raise NotifierError(
                "macos helper build failed: " + (result.stderr or result.stdout)[-500:]
            )
        info = {
            "CFBundleIdentifier": settings.notify_macos_bundle_id,
            "CFBundleName": "EVNotificationHelper",
            "CFBundleDisplayName": "EV",
            "CFBundleExecutable": "EVNotificationHelper",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "13.0",
        }
        (contents_dir / "Info.plist").write_bytes(plistlib.dumps(info))
        sign = subprocess.run(
            ["codesign", "--force", "--deep", "-s", "-", str(app_dir)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if sign.returncode != 0:
            raise NotifierError(
                "macos helper ad-hoc codesign failed: " + (sign.stderr or sign.stdout)[-300:]
            )
        return target

    async def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return await asyncio.to_thread(
            subprocess.run, args, capture_output=True, text=True, timeout=15
        )

    async def check_permission(self) -> dict:
        helper = self._helper_path()
        if helper is None:
            helper = await asyncio.to_thread(self._build_helper)
        result = await self._run([str(helper), "--check-permission"])
        return {"permission": (result.stdout or result.stderr).strip(), "reason": None}

    async def send(self, notification: NotificationRecord) -> DeliveryReceipt:
        terminal_notifier = shutil.which("terminal-notifier")
        helper = self._helper_path()
        helper_error: str | None = None
        if helper is None:
            try:
                helper = await asyncio.to_thread(self._build_helper)
            except NotifierError as exc:
                helper_error = str(exc)
                if terminal_notifier is not None:
                    helper = None

        if helper is not None:
            result = await self._run(
                [
                    str(helper),
                    "--id",
                    str(notification.id),
                    "--bundle-id",
                    settings.notify_macos_bundle_id,
                    "--title",
                    notification.title,
                    "--body",
                    notification.body,
                ]
            )
            if result.returncode == 0:
                return DeliveryReceipt(
                    status="delivered",
                    backend=self.name,
                    backend_ref=str(notification.id),
                    reason=None,
                    details={"helper": str(helper), "channel": "notification_center"},
                )
            helper_error = (
                f"macos helper rejected: exit {result.returncode}: "
                + (result.stderr or result.stdout or "").strip()[:300]
            )

        if terminal_notifier is not None:
            result = await self._run(
                [
                    terminal_notifier,
                    "-title",
                    notification.title,
                    "-message",
                    notification.body,
                    "-sound",
                    "default",
                ]
            )
            if result.returncode == 0:
                return DeliveryReceipt(
                    status="delivered",
                    backend=self.name,
                    backend_ref=str(notification.id),
                    reason="terminal-notifier interim",
                    details={"channel": "notification_center", "interim": True},
                )
            helper_error = (
                f"terminal-notifier failed: exit {result.returncode}: "
                + (result.stderr or result.stdout or "").strip()[:300]
            )

        if settings.notify_macos_allow_osascript:
            escaped_body = notification.body.replace("\\", "\\\\").replace('"', '\\"')
            escaped_title = notification.title.replace("\\", "\\\\").replace('"', '\\"')
            script = (
                'display notification "'
                + escaped_body
                + '" with title "'
                + escaped_title
                + '" sound name "default"'
            )
            result = await self._run(["osascript", "-e", script])
            if result.returncode == 0:
                return DeliveryReceipt(
                    status="delivered",
                    backend=self.name,
                    backend_ref=str(notification.id),
                    reason="osascript interim (UNUserNotificationCenter blocked in sandbox)",
                    details={"channel": "notification_center", "interim": True, "engine": "osascript"},
                )
            helper_error = (
                f"osascript failed: exit {result.returncode}: "
                + (result.stderr or result.stdout or "").strip()[:300]
            )

        raise NotifierError(
            "macos backend unavailable: "
            + (helper_error or "no delivery path (helper, terminal-notifier, osascript)")
        )
