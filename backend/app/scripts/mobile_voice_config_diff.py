"""Print Mac golden vs iPhone Mobile Voice session fingerprints."""

from __future__ import annotations

import json

from app.device_gateway.mobile_voice import fingerprint_report


def main() -> None:
    report = fingerprint_report()
    print(json.dumps(report, indent=2, default=str))
    mismatches = [row for row in report["diff"] if not row.get("match") and row.get("field") != "transport"]
    print()
    print("MOBILE VOICE: OWNER FAILURE / CONVERGENCE ACTIVE")
    print(f"intentional_or_gap_fields: {len(mismatches)}")
    for row in mismatches:
        print(f"  {row['field']}: mac={row.get('mac')!r}  iphone={row.get('iphone')!r}")


if __name__ == "__main__":
    main()
