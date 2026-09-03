"""Spoken names for real local files Evie has actually touched.

Compatibility loader. The live twin lives in desk_scene: packets, deixis
slots, held note items, and the mutation ledger.
"""

from __future__ import annotations

from app.ev.desk_scene import ADD_TO_NAMED_RE as ADD_TO_NAMED_RE
from app.ev.desk_scene import BIND_RE as BIND_RE
from app.ev.desk_scene import GENERIC_ALIASES as GENERIC_ALIASES
from app.ev.desk_scene import GROCERY_HINT as GROCERY_HINT
from app.ev.desk_scene import inferred_aliases as inferred_aliases
from app.ev.desk_scene import names_store_path as store_path
from app.ev.desk_scene import normalize_alias as normalize_alias
from app.ev.desk_scene import parse_bind_goal as parse_bind_goal
from app.ev.desk_scene import remember_file as remember_file
from app.ev.desk_scene import reset_desk_names as reset_desk_names
from app.ev.desk_scene import resolve_alias as resolve_alias
from app.ev.desk_scene import resolve_spoken_file as resolve_spoken_file
from app.ev.desk_scene import scene_store_path as scene_store_path
