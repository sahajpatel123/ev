"""Smart home: Home Assistant + local simulated house, evidence-backed acts."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HomeEntity, Integration
from app.services.access_log import log_access
from app.utils.text import utcnow

DEFAULT_HOUSE: tuple[dict[str, Any], ...] = (
    {
        "entity_id": "light.lab",
        "name": "lab lights",
        "area": "lab",
        "domain": "light",
        "state": "off",
    },
    {
        "entity_id": "lock.front_door",
        "name": "front door",
        "area": "front",
        "domain": "lock",
        "state": "unlocked",
    },
    {
        "entity_id": "cover.garage",
        "name": "garage",
        "area": "garage",
        "domain": "cover",
        "state": "closed",
    },
)

_ON = {"on", "open", "unlocked", "true", "1"}
_DOMAIN_ACTIONS = {
    "light": {
        "on": ("on", "turn_on"),
        "off": ("off", "turn_off"),
        "toggle": ("toggle",),
    },
    "lock": {
        "lock": ("lock", "locked"),
        "unlock": ("unlock", "unlocked"),
    },
    "cover": {
        "open": ("open", "open_cover"),
        "close": ("close", "closed", "close_cover"),
    },
}

_HA_SERVICE = {
    ("light", "on"): ("light", "turn_on"),
    ("light", "off"): ("light", "turn_off"),
    ("lock", "lock"): ("lock", "lock"),
    ("lock", "unlock"): ("lock", "unlock"),
    ("cover", "open"): ("cover", "open_cover"),
    ("cover", "close"): ("cover", "close_cover"),
}

_TARGET_STATE = {
    ("light", "on"): "on",
    ("light", "off"): "off",
    ("lock", "lock"): "locked",
    ("lock", "unlock"): "unlocked",
    ("cover", "open"): "open",
    ("cover", "close"): "closed",
}


def normalize_action(domain: str, action: str) -> str | None:
    raw = (action or "").strip().lower().replace(".", "_")
    raw = raw.removeprefix(f"{domain}_").removeprefix(f"{domain}.")
    aliases = _DOMAIN_ACTIONS.get(domain) or {}
    for canonical, names in aliases.items():
        if raw == canonical or raw in names:
            return canonical
    return None


async def active_smart_home(session: AsyncSession) -> Integration | None:
    return (
        await session.execute(
            select(Integration)
            .where(
                Integration.adapter == "smart_home",
                Integration.status == "active",
            )
            .order_by(Integration.created_at.asc())
            .limit(1)
        )
    ).scalars().first()


async def load_home_provider(session: AsyncSession) -> tuple[str, dict]:
    """Vault token + config for the active smart_home integration, or local defaults."""

    integration = await active_smart_home(session)
    if integration is None:
        return "", {"provider": "local"}
    config = dict(integration.config or {})
    provider = str(config.get("provider") or "local").lower()
    token = ""
    if provider == "homeassistant":
        from app.integrations import vault
        from app.integrations.service import _credential

        credential = await _credential(session, integration.id, "oauth")
        if (
            credential is not None
            and credential.revoked_at is None
            and credential.encrypted_access
        ):
            token = vault.decrypt(credential.encrypted_access)
    return token, config


async def ha_configured(session: AsyncSession) -> bool:
    _token, config = await load_home_provider(session)
    return str(config.get("provider") or "").lower() == "homeassistant"


async def ensure_inventory(session: AsyncSession) -> list[HomeEntity]:
    rows = list((await session.execute(select(HomeEntity))).scalars().all())
    if rows:
        return rows
    created: list[HomeEntity] = []
    for item in DEFAULT_HOUSE:
        row = HomeEntity(
            entity_id=item["entity_id"],
            name=item["name"],
            area=item.get("area"),
            domain=item["domain"],
            state=item["state"],
            attributes={},
            updated_at=utcnow(),
        )
        session.add(row)
        created.append(row)
    await session.flush()
    return created


async def resolve_entity(session: AsyncSession, name_or_id: str) -> HomeEntity | None:
    await ensure_inventory(session)
    raw = (name_or_id or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    rows = list((await session.execute(select(HomeEntity))).scalars().all())
    for row in rows:
        if row.entity_id.lower() == lowered or row.name.lower() == lowered:
            return row
    for row in rows:
        if lowered in row.name.lower() or lowered in row.entity_id.lower():
            return row
        aliases = (row.attributes or {}).get("aliases") or []
        if any(lowered == str(alias).lower() for alias in aliases):
            return row
    return None


def entity_dict(row: HomeEntity) -> dict:
    return {
        "entity_id": row.entity_id,
        "name": row.name,
        "area": row.area,
        "domain": row.domain,
        "state": row.state,
        "attributes": dict(row.attributes or {}),
    }


async def home_status(session: AsyncSession, area: str | None = None) -> dict:
    await ensure_inventory(session)
    simulated = not await ha_configured(session)
    rows = list((await session.execute(select(HomeEntity))).scalars().all())
    if area:
        wanted = area.strip().lower()
        rows = [
            row
            for row in rows
            if (row.area or "").lower() == wanted or wanted in (row.name or "").lower()
        ]
    spoken = (
        "This is a simulated home."
        if simulated
        else f"{len(rows)} home entities."
    )
    if rows and simulated:
        bits = [f"{row.name} is {row.state}" for row in rows[:6]]
        spoken = "Simulated home. " + "; ".join(bits) + "."
    elif rows:
        bits = [f"{row.name} is {row.state}" for row in rows[:6]]
        spoken = "; ".join(bits) + "."
    return {
        "ok": True,
        "simulated": simulated,
        "spoken": spoken,
        "count": len(rows),
        "entities": [entity_dict(row) for row in rows],
    }


def _states_match(domain: str, requested: str, actual: str) -> bool:
    want = _TARGET_STATE.get((domain, requested), requested)
    got = (actual or "").lower()
    if want == got:
        return True
    if domain == "light" and want == "on":
        return got in _ON
    if domain == "cover" and want == "open":
        return got in {"open", "opening"}
    if domain == "lock" and want == "locked":
        return got in {"locked", "locking"}
    return False


async def _apply_local(row: HomeEntity, action: str) -> str:
    target = _TARGET_STATE[(row.domain, action)]
    row.state = target
    row.updated_at = utcnow()
    return target


async def _ha_act(
    *,
    entity_id: str,
    domain: str,
    action: str,
    token: str,
    base_url: str,
) -> dict:
    service = _HA_SERVICE.get((domain, action))
    if service is None:
        raise ValueError(f"unsupported homeassistant action {domain}.{action}")
    svc_domain, svc_name = service
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    root = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        posted = await client.post(
            f"{root}/api/services/{svc_domain}/{svc_name}",
            headers=headers,
            json={"entity_id": entity_id},
        )
        posted.raise_for_status()
        state_resp = await client.get(
            f"{root}/api/states/{entity_id}",
            headers=headers,
        )
        state_resp.raise_for_status()
        payload = state_resp.json()
    new_state = str(payload.get("state") or "")
    return {"state": new_state, "raw": payload}


async def home_act(
    session: AsyncSession,
    entity: str,
    action: str,
    *,
    confirm: bool = False,
    actor: str = "master",
    token: str = "",
    config: dict | None = None,
) -> dict:
    from app.ev.actions import autonomy_mode

    row = await resolve_entity(session, entity)
    if row is None:
        return {
            "ok": False,
            "error": "unknown_entity",
            "spoken": f"I don't know an entity named {entity}.",
        }
    canonical = normalize_action(row.domain, action)
    if canonical is None:
        valid = ", ".join(_DOMAIN_ACTIONS.get(row.domain, {}))
        return {
            "ok": False,
            "error": "unknown_action",
            "spoken": f"{row.name} cannot {action}. Valid actions: {valid}.",
        }
    if row.domain == "lock" and autonomy_mode() != "full" and not confirm:
        return {
            "ok": False,
            "error": "confirm_required",
            "spoken": f"Confirm to {canonical} {row.name}.",
        }

    if not config:
        loaded_token, loaded_config = await load_home_provider(session)
        config = loaded_config
        if not token:
            token = loaded_token
    config = config or {}
    provider = str(config.get("provider") or "local").lower()
    new_state: str
    if provider == "homeassistant":
        base_url = str(config.get("base_url") or "").strip()
        if not base_url:
            return {
                "ok": False,
                "error": "missing_base_url",
                "spoken": "Home Assistant is not configured.",
            }
        if not token:
            return {
                "ok": False,
                "error": "missing_token",
                "spoken": "Home Assistant token is not in the vault.",
            }
        try:
            result = await _ha_act(
                entity_id=row.entity_id,
                domain=row.domain,
                action=canonical,
                token=token,
                base_url=base_url,
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            return {
                "ok": False,
                "error": "provider_error",
                "spoken": f"Home Assistant failed: {exc}",
            }
        new_state = str(result.get("state") or "")
        row.state = new_state or row.state
        row.updated_at = utcnow()
    else:
        if config.get("simulate_mismatch"):
            new_state = row.state
        else:
            await _apply_local(row, canonical)
            await session.flush()
            refreshed = await session.get(HomeEntity, row.id)
            new_state = refreshed.state if refreshed is not None else row.state

    matched = _states_match(row.domain, canonical, new_state)
    await log_access(
        session,
        actor=actor,
        action="home_act",
        endpoint="tool:home_act",
        resource_type="home_entity",
        resource_ids=[row.entity_id],
        details={
            "action": canonical,
            "requested": action,
            "new_state": new_state,
            "matched": matched,
            "provider": provider,
        },
    )
    await session.flush()
    simulated = provider != "homeassistant"
    if not matched:
        return {
            "ok": False,
            "error": "state_mismatch",
            "spoken": f"I asked to {canonical} {row.name} but it reads {new_state}.",
            "entity": entity_dict(row),
            "requested": canonical,
            "new_state": new_state,
            "simulated": simulated,
        }
    spoken = f"{row.name} is now {new_state}."
    if simulated:
        spoken = f"Simulated home. {spoken}"
    return {
        "ok": True,
        "spoken": spoken,
        "entity": entity_dict(row),
        "requested": canonical,
        "new_state": new_state,
        "simulated": simulated,
    }


async def adapter_act(
    *,
    action: str,
    args: dict,
    token: str,
    scopes: list[str],
    config: dict,
    session: AsyncSession | None = None,
) -> dict:
    """Used by SmartHomeAdapter.act so HA/local share one honesty rule."""

    if action == "home.status" or action == "home.list_devices":
        if session is None:
            house = _memory_house(config)
            return {
                "ok": True,
                "mode": "local",
                "action": action,
                "simulated": True,
                "spoken": "This is a simulated home.",
                "entities": list(house.values()),
                "devices": list(house.values()),
            }
        area = args.get("area")
        return await home_status(session, area=str(area) if area else None)
    if action in {"home.set_device", "light.set", "lock.set", "cover.set"}:
        provider = str(config.get("provider") or "local").lower()
        if session is None and provider == "homeassistant":
            entity_id = str(
                args.get("entity_id") or args.get("entity") or args.get("device") or ""
            )
            requested = str(args.get("action") or args.get("state") or "")
            if action != "home.set_device" and not requested:
                requested = action.split(".", 1)[0]
            domain = entity_id.split(".", 1)[0] if "." in entity_id else requested
            canonical = normalize_action(domain, requested) or requested
            base_url = str(config.get("base_url") or "").strip()
            if not base_url:
                return {"ok": False, "error": "missing_base_url"}
            result = await _ha_act(
                entity_id=entity_id,
                domain=domain,
                action=canonical,
                token=token,
                base_url=base_url,
            )
            new_state = str(result.get("state") or "")
            matched = _states_match(domain, canonical, new_state)
            return {
                "ok": matched,
                "error": None if matched else "state_mismatch",
                "new_state": new_state,
                "requested": canonical,
                "simulated": False,
            }
        if session is None:
            return _memory_act(action, args, config)
        entity = str(args.get("entity") or args.get("entity_id") or args.get("device") or "")
        requested = str(args.get("action") or args.get("state") or "")
        if action != "home.set_device" and not requested:
            requested = action.split(".", 1)[0]
        return await home_act(
            session,
            entity,
            requested,
            confirm=bool(args.get("confirm")),
            actor="adapter",
            token=token,
            config=config,
        )
    return {"ok": True, "mode": config.get("provider") or "local", "action": action}


def _memory_house(config: dict) -> dict[str, dict]:
    stored = config.get("house")
    if isinstance(stored, dict) and stored:
        return stored
    house = {
        item["entity_id"]: {
            "entity_id": item["entity_id"],
            "name": item["name"],
            "area": item.get("area"),
            "domain": item["domain"],
            "state": item["state"],
        }
        for item in DEFAULT_HOUSE
    }
    config["house"] = house
    return house


def _memory_act(action: str, args: dict, config: dict) -> dict:
    house = _memory_house(config)
    entity = str(args.get("entity") or args.get("entity_id") or args.get("device") or "")
    requested = str(args.get("action") or args.get("state") or "")
    if action != "home.set_device" and not requested:
        requested = action.split(".", 1)[0]
    row = None
    lowered = entity.lower()
    for item in house.values():
        if item["entity_id"].lower() == lowered or item["name"].lower() == lowered:
            row = item
            break
    if row is None:
        return {"ok": False, "error": "unknown_entity", "spoken": f"I don't know an entity named {entity}."}
    canonical = normalize_action(row["domain"], requested)
    if canonical is None:
        return {"ok": False, "error": "unknown_action"}
    if args.get("force_mismatch"):
        # Test seam: pretend the provider did not honor the request.
        return {
            "ok": False,
            "error": "state_mismatch",
            "spoken": f"I asked to {canonical} {row['name']} but it reads {row['state']}.",
            "new_state": row["state"],
            "requested": canonical,
            "simulated": True,
        }
    target = _TARGET_STATE[(row["domain"], canonical)]
    row["state"] = target
    if not _states_match(row["domain"], canonical, row["state"]):
        return {
            "ok": False,
            "error": "state_mismatch",
            "new_state": row["state"],
            "requested": canonical,
            "simulated": True,
        }
    return {
        "ok": True,
        "spoken": f"Simulated home. {row['name']} is now {row['state']}.",
        "entity": dict(row),
        "requested": canonical,
        "new_state": row["state"],
        "simulated": True,
    }


async def sync_from_ha(
    session: AsyncSession,
    *,
    token: str,
    config: dict,
) -> int:
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        return 0
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{base_url.rstrip('/')}/api/states", headers=headers)
        response.raise_for_status()
        items = response.json()
    count = 0
    if not isinstance(items, list):
        return 0
    for item in items:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "")
        if not entity_id:
            continue
        domain = entity_id.split(".", 1)[0]
        if domain not in _DOMAIN_ACTIONS:
            continue
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        name = str(attrs.get("friendly_name") or entity_id)
        area = attrs.get("area") or attrs.get("area_id")
        row = (
            await session.execute(select(HomeEntity).where(HomeEntity.entity_id == entity_id))
        ).scalar_one_or_none()
        if row is None:
            row = HomeEntity(
                entity_id=entity_id,
                name=name,
                area=str(area) if area else None,
                domain=domain,
                state=str(item.get("state") or "unknown"),
                attributes=attrs,
                updated_at=utcnow(),
            )
            session.add(row)
        else:
            row.name = name
            row.state = str(item.get("state") or row.state)
            row.attributes = attrs
            row.updated_at = utcnow()
        count += 1
    await session.flush()
    return count
