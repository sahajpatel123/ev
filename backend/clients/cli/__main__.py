"""Entrypoint for `python -m clients.cli` (same main as the `ev` script)."""

from clients.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
