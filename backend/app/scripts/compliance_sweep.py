"""CLI entrypoint: run the biometric compliance retention sweep."""

from __future__ import annotations

from app.workers.jobs import run_compliance_retention


def main() -> None:
    import json

    print(json.dumps(run_compliance_retention(), indent=2, default=str))


if __name__ == "__main__":
    main()
