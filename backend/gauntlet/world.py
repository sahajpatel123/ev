"""Isolated worlds for one gauntlet case. Nothing here is production behavior."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


class World:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ev-gauntlet-"))
        self.home = self.root / "home"
        for name in ("Desktop", "Documents", "Downloads", "Code", "Projects"):
            (self.home / name).mkdir(parents=True)
        self.notes: list[dict] = []
        self.reminders: list[dict] = []
        self.memory: dict[str, str] = {}

    def write(self, rel: str, text: str) -> None:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def read(self, rel: str) -> str:
        return (self.home / rel).read_text(encoding="utf-8")

    def exists(self, rel: str) -> bool:
        return (self.home / rel).exists()

    def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def leaked(self) -> list[str]:
        if self.root.exists():
            return [str(self.root)]
        return []
