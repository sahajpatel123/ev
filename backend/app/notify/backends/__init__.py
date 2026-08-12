"""Backend factory for the Notifier protocol."""

from __future__ import annotations

from app.notify.backends.apns import APNsNotifier
from app.notify.backends.console import ConsoleNotifier
from app.notify.backends.macos import MacOSNotifier
from app.notify.backends.webhook import WebhookNotifier
from app.notify.models import NotifierError


def get_backend(name: str):
    mapping = {
        "console": ConsoleNotifier,
        "macos": MacOSNotifier,
        "webhook": WebhookNotifier,
        "apns": APNsNotifier,
    }
    try:
        return mapping[name]()
    except KeyError as exc:
        raise NotifierError(f"unknown notification backend {name!r}") from exc
