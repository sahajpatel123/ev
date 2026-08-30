"""PULSE: real notification delivery with receipts and attention budgeting.

The public entrypoint is :mod:`app.notify.service`: dispatch notifications,
deliver pending alert-radar rows, batch quiet-hours digests, and escalate
dead letters. Backends live under :mod:`app.notify.backends`.
"""

from app.notify.service import (  # noqa: F401
    acknowledge_notification,
    build_and_deliver_digest,
    deliver_dlq_escalations,
    deliver_pending_alerts,
    dispatch_action,
    dispatch_notification,
    notify_status,
    send_presence_beacon,
)
