"""Skill packs. Importing `load_all()` registers every pack's skills in the registry."""

from __future__ import annotations


def load_all() -> None:
    from . import skills_media, skills_system, skills_terminal  # noqa: F401
