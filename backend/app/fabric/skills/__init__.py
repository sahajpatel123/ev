"""HANDS: reliable, verifiable operation of the Mac (docs/contracts/SKILLS_API.md).

Import `Hands` from `app.fabric.skills.api`. Importing this package never
touches the machine and never imports an optional dependency.
"""

from .receipts import API_VERSION, CONTRACT

__all__ = ["API_VERSION", "CONTRACT"]
