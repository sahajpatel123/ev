"""Standing owner authority for life actions (messaging/phone).

Once the owner grants a standing scope (``messaging:act``, ``phone:act``,
``contacts:read``), known contacts are pre-authorized; the contact allowlist
decides how wide that pre-authorization is:

- ``EV_LIFE_CONTACT_ALLOWLIST=any`` — any recipient is pre-authorized.
- ``EV_LIFE_CONTACT_ALLOWLIST=all`` — every known (resolved) contact is
  pre-authorized; unknown recipients need confirmation.
- ``EV_LIFE_CONTACT_ALLOWLIST=starred`` — only starred contacts are
  pre-authorized; everything else needs confirmation.

When confirmation is required, the caller must pass ``confirm: true`` on the
action (or the runtime approval flow must have approved it). With
``EV_LIFE_AUTONOMY=full`` the owner has explicitly opted out of per-action
confirmation for anything inside the granted scopes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings

ALLOWLIST_ANY = "any"
ALLOWLIST_ALL = "all"
ALLOWLIST_STARRED = "starred"


@dataclass(frozen=True)
class LifePolicyDecision:
    allowed: bool
    confirmation_required: bool
    reason: str
    contact: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "confirmation_required": self.confirmation_required,
            "reason": self.reason,
            "contact": self.contact,
        }


def evaluate_life_policy(
    *,
    scopes: list[str],
    action: str,
    recipient: str | None,
    contact: dict[str, Any] | None = None,
    confirm: bool = False,
    allowlist: str | None = None,
    autonomy: str | None = None,
    confirm_unknown: bool | None = None,
) -> LifePolicyDecision:
    """Decide whether an action may proceed and whether confirmation is needed."""
    if action in ("phone.call", "facetime.call"):
        action_scope = "phone:act"
    elif action == "mail.send":
        action_scope = "mail:act"
    else:
        action_scope = "messaging:act"
    if action_scope not in scopes:
        return LifePolicyDecision(
            allowed=False,
            confirmation_required=False,
            reason=f"scope '{action_scope}' is not granted",
        )
    effective_allowlist = (allowlist or settings.life_contact_allowlist).lower()
    if effective_allowlist not in (ALLOWLIST_ANY, ALLOWLIST_ALL, ALLOWLIST_STARRED):
        return LifePolicyDecision(
            allowed=False,
            confirmation_required=False,
            reason=f"invalid EV_LIFE_CONTACT_ALLOWLIST '{effective_allowlist}'",
        )
    effective_autonomy = (autonomy or settings.life_autonomy).lower()
    effective_confirm_unknown = (
        confirm_unknown
        if confirm_unknown is not None
        else bool(settings.life_confirm_unknown)
    )
    if effective_autonomy == "full":
        return LifePolicyDecision(
            allowed=True,
            confirmation_required=False,
            reason="EV_LIFE_AUTONOMY=full",
            contact=contact,
        )
    if effective_allowlist == ALLOWLIST_ANY:
        return LifePolicyDecision(
            allowed=True,
            confirmation_required=False,
            reason="allowlist=any",
            contact=contact,
        )

    is_known = bool(contact and (contact.get("phone") or contact.get("email") or contact.get("id")))
    is_starred = bool(contact and contact.get("starred") is True)
    preauthorized = (
        (effective_allowlist == ALLOWLIST_ALL and is_known)
        or (effective_allowlist == ALLOWLIST_STARRED and is_starred)
    )
    if preauthorized:
        return LifePolicyDecision(
            allowed=True,
            confirmation_required=False,
            reason=f"known contact under allowlist={effective_allowlist}",
            contact=contact,
        )
    if not recipient:
        return LifePolicyDecision(
            allowed=False,
            confirmation_required=False,
            reason="missing recipient",
            contact=contact,
        )
    if effective_confirm_unknown:
        if confirm:
            return LifePolicyDecision(
                allowed=True,
                confirmation_required=True,
                reason="explicit confirm=true",
                contact=contact,
            )
        return LifePolicyDecision(
            allowed=False,
            confirmation_required=True,
            reason=(
                "recipient is not pre-authorized by allowlist "
                f"{effective_allowlist}; pass confirm=true or add to allowlist"
            ),
            contact=contact,
        )
    return LifePolicyDecision(
        allowed=False,
        confirmation_required=False,
        reason=f"recipient not in allowlist={effective_allowlist} and unknown-confirmation is disabled",
        contact=contact,
    )
