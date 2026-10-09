"""Enroll the owner's contact details from environment variables.

Required: EV_OWNER_ENROLL_NAME, EV_OWNER_ENROLL_PHONE,
EV_OWNER_ENROLL_EMAIL_PRIMARY. Optional: EV_OWNER_ENROLL_EMAIL_SECONDARY,
EV_OWNER_ENROLL_EMAIL_WORK. Fails closed (exit 2) when a required value is
missing. Dry-run by default; --apply writes. Values are never printed in
full — dry-run shows field names with masked values (last 2 chars only).

Usage:
    uv run python -m app.scripts.owner_enroll_cli
    uv run python -m app.scripts.owner_enroll_cli --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

REQUIRED_VARS = (
    "EV_OWNER_ENROLL_NAME",
    "EV_OWNER_ENROLL_PHONE",
    "EV_OWNER_ENROLL_EMAIL_PRIMARY",
)

OPTIONAL_VARS = (
    ("EV_OWNER_ENROLL_EMAIL_SECONDARY", "secondary"),
    ("EV_OWNER_ENROLL_EMAIL_WORK", "work"),
)


def mask_value(value: str) -> str:
    """Mask everything except the last 2 chars (short values fully masked)."""
    if len(value) <= 2:
        return "*" * len(value)
    return "*" * (len(value) - 2) + value[-2:]


def _read_env() -> tuple[dict[str, str], list[str]]:
    values = {var: os.environ.get(var, "").strip() for var in REQUIRED_VARS}
    for var, _slot in OPTIONAL_VARS:
        values[var] = os.environ.get(var, "").strip()
    missing = [var for var in REQUIRED_VARS if not values[var]]
    return values, missing


async def run_cli(*, apply: bool) -> int:
    values, missing = _read_env()
    if missing:
        print(
            f"error: missing required enrollment env vars: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2
    if not apply:
        print("owner enrollment (dry-run): no writes performed")
        print(f"  display_name: {mask_value(values['EV_OWNER_ENROLL_NAME'])}")
        print(f"  primary_phone: {mask_value(values['EV_OWNER_ENROLL_PHONE'])}")
        print(
            "  email_primary: "
            f"{mask_value(values['EV_OWNER_ENROLL_EMAIL_PRIMARY'])}"
        )
        for var, slot in OPTIONAL_VARS:
            raw = values[var]
            shown = mask_value(raw) if raw else "(not set)"
            print(f"  email_{slot}: {shown}")
        return 0

    from app.db import SessionLocal
    from app.ev.owner_enroll import enroll_owner_contact

    emails = {"primary": values["EV_OWNER_ENROLL_EMAIL_PRIMARY"]}
    for var, slot in OPTIONAL_VARS:
        if values[var]:
            emails[slot] = values[var]
    async with SessionLocal() as session:
        result = await enroll_owner_contact(
            session,
            display_name=values["EV_OWNER_ENROLL_NAME"],
            primary_phone=values["EV_OWNER_ENROLL_PHONE"],
            emails=emails,
        )
        await session.commit()
    # Ids and counts only — contact values are never printed.
    print(
        "owner enrollment (APPLY): "
        f"event_id={result['event_id']} "
        f"facts={len(result['memory_ids'])} "
        f"display_name_set={result['display_name_set']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enroll owner contact details.")
    parser.add_argument(
        "--apply", action="store_true", help="Write enrollment (default is dry-run)."
    )
    args = parser.parse_args(argv)
    return asyncio.run(run_cli(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
