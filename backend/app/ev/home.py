"""Smart home: Home Assistant + local simulated house, evidence-backed acts."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.actuator import (
    DEFAULT_TIMEOUT_SECONDS,
    evidence_base,
    record_actuator,
    with_retry,
    with_timeout,
)
from app.ev.resolve import Match, ambiguous_spoken, candidate_names, pick_unique
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


def _ha_ready(config: dict, token: str) -> bool:
    provider = str(config.get("provider") or "").lower()
    return provider == "homeassistant" and bool(str(config.get("base_url") or "").strip()) and bool(token)


async def ha_configured(session: AsyncSession) -> bool:
    token, config = await load_home_provider(session)
    return _ha_ready(config, token)


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


def _entity_labels(row: HomeEntity) -> list[str]:
    aliases = (row.attributes or {}).get("aliases") or []
    labels = [row.entity_id, row.name, *(str(alias) for alias in aliases)]
    if row.area:
        labels.append(f"{row.area} {row.domain}")
        labels.append(f"{row.area} {row.name}")
    return labels


async def match_home_entity(session: AsyncSession, name_or_id: str) -> Match[HomeEntity]:
    await ensure_inventory(session)
    raw = (name_or_id or "").strip()
    if not raw:
        return Match(status="none", item=None, score=0.0)
    rows = list((await session.execute(select(HomeEntity))).scalars().all())
    return pick_unique(raw, rows, labels=_entity_labels)


async def resolve_entity(session: AsyncSession, name_or_id: str) -> HomeEntity | None:
    match = await match_home_entity(session, name_or_id)
    return match.item if match.unique else None


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
    token, config = await load_home_provider(session)
    provider = str(config.get("provider") or "local").lower()
    simulated = provider != "homeassistant"
    rows = list((await session.execute(select(HomeEntity))).scalars().all())
    if area:
        wanted = area.strip().lower()
        rows = [
            row
            for row in rows
            if (row.area or "").lower() == wanted or wanted in (row.name or "").lower()
        ]
    observed = simulated
    stale = False
    error = None
    source = "local"
    if provider == "homeassistant":
        source = "homeassistant"
        if not _ha_ready(config, token):
            observed = False
            stale = True
            error = "not_connected"
        else:
            refreshed = await with_timeout(
                _ha_refresh_rows(
                    session,
                    rows,
                    token=token,
                    base_url=str(config.get("base_url") or ""),
                ),
                seconds=DEFAULT_TIMEOUT_SECONDS,
                spoken="Home Assistant timed out. Showing last known state.",
            )
            if isinstance(refreshed, dict) and refreshed.get("error") in {"timeout", "cancelled"}:
                observed = False
                stale = True
                error = str(refreshed.get("error"))
            elif isinstance(refreshed, dict) and refreshed.get("ok") is False:
                observed = False
                stale = True
                error = str(refreshed.get("error") or "provider_error")
            else:
                observed = True
                rows = list((await session.execute(select(HomeEntity))).scalars().all())
                if area:
                    wanted = area.strip().lower()
                    rows = [
                        row
                        for row in rows
                        if (row.area or "").lower() == wanted or wanted in (row.name or "").lower()
                    ]
    bits = [f"{row.name} is {row.state}" for row in rows[:6]]
    if simulated:
        spoken = "Simulated home. " + ("; ".join(bits) + "." if bits else "No entities.")
    elif stale:
        prefix = "Home Assistant is not configured." if error == "not_connected" else "Home Assistant did not respond."
        spoken = prefix + (" Last known: " + "; ".join(bits) + "." if bits else "")
    else:
        spoken = "; ".join(bits) + "." if bits else f"{len(rows)} home entities."
    result = {
        "ok": True,
        "simulated": simulated,
        "stale": stale,
        "spoken": spoken,
        "count": len(rows),
        "entities": [entity_dict(row) for row in rows],
        "evidence": evidence_base(
            source=source,
            accepted=True,
            observed=observed,
            entity_count=len(rows),
            stale=stale,
        ),
    }
    if error:
        result["error"] = error
        result["evidence"]["error"] = error
    return result


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


class TransientHomeError(Exception):
    """Retryable Home Assistant transport or gateway failure."""


def _ha_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _ha_request_json(
    method: str,
    url: str,
    *,
    token: str,
    json: dict | None = None,
) -> dict | list:
    async def once() -> dict | list:
        headers = _ha_headers(token)
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            if method.upper() == "GET":
                response = await client.get(url, headers=headers)
            else:
                response = await client.post(url, headers=headers, json=json)
            status = getattr(response, "status_code", 200)
            if status in {429, 502, 503, 504}:
                raise TransientHomeError(f"homeassistant status {status}")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, (dict, list)):
                return {}
            return payload

    return await with_retry(
        once,
        retry_on=(TransientHomeError, httpx.TransportError, httpx.TimeoutException),
    )


async def _ha_get_state(*, entity_id: str, token: str, base_url: str) -> dict:
    payload = await _ha_request_json(
        "GET",
        f"{base_url.rstrip('/')}/api/states/{entity_id}",
        token=token,
    )
    state = ""
    raw = payload if isinstance(payload, dict) else {}
    if isinstance(payload, dict):
        state = str(payload.get("state") or "")
    return {"state": state, "raw": raw, "accepted": True}


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
    root = base_url.rstrip("/")
    await _ha_request_json(
        "POST",
        f"{root}/api/services/{svc_domain}/{svc_name}",
        token=token,
        json={"entity_id": entity_id},
    )
    observed = await _ha_get_state(entity_id=entity_id, token=token, base_url=base_url)
    return {"state": observed.get("state") or "", "raw": observed.get("raw") or {}, "accepted": True}


async def _ha_refresh_rows(
    session: AsyncSession,
    rows: list[HomeEntity],
    *,
    token: str,
    base_url: str,
) -> dict:
    updated = 0
    for row in rows:
        if row.domain != "light":
            continue
        snapshot = await _ha_get_state(entity_id=row.entity_id, token=token, base_url=base_url)
        state = str(snapshot.get("state") or "")
        if state:
            row.state = state
            row.updated_at = utcnow()
            updated += 1
    await session.flush()
    return {"ok": True, "updated": updated}


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

    match = await match_home_entity(session, entity)
    if match.status == "ambiguous":
        names = candidate_names(match.candidates, name_of=lambda row: row.name)
        kind = "light" if all(row.domain == "light" for row in match.candidates) else "device"
        return {
            "ok": False,
            "error": "ambiguous",
            "candidates": [entity_dict(row) for row in match.candidates],
            "spoken": ambiguous_spoken(kind, names),
        }
    row = match.item
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
    target_state = _TARGET_STATE.get((row.domain, canonical), canonical)
    skipped_command = False
    accepted = False
    new_state: str
    if provider == "homeassistant":
        base_url = str(config.get("base_url") or "").strip()
        if not base_url:
            return {
                "ok": False,
                "error": "not_connected",
                "spoken": "Home Assistant is not configured.",
            }
        if not token:
            return {
                "ok": False,
                "error": "not_connected",
                "spoken": "Home Assistant token is not in the vault.",
            }
        try:
            live = await with_timeout(
                _ha_get_state(entity_id=row.entity_id, token=token, base_url=base_url),
                seconds=DEFAULT_TIMEOUT_SECONDS,
                spoken="Home Assistant timed out. I will not claim the light changed.",
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            return {
                "ok": False,
                "error": "provider_error",
                "spoken": f"Home Assistant failed: {exc}",
            }
        if isinstance(live, dict) and live.get("error") in {"timeout", "cancelled"}:
            return live
        live_state = str((live or {}).get("state") or "")
        if live_state:
            row.state = live_state
            row.updated_at = utcnow()
        if _states_match(row.domain, canonical, row.state):
            skipped_command = True
            accepted = True
            new_state = row.state
        else:
            try:
                result = await with_timeout(
                    _ha_act(
                        entity_id=row.entity_id,
                        domain=row.domain,
                        action=canonical,
                        token=token,
                        base_url=base_url,
                    ),
                    seconds=DEFAULT_TIMEOUT_SECONDS,
                    spoken="Home Assistant timed out. I will not claim the light changed.",
                )
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                return {
                    "ok": False,
                    "error": "provider_error",
                    "spoken": f"Home Assistant failed: {exc}",
                }
            if isinstance(result, dict) and result.get("error") in {"timeout", "cancelled"}:
                return result
            new_state = str((result or {}).get("state") or "")
            accepted = bool((result or {}).get("accepted"))
            row.state = new_state or row.state
            row.updated_at = utcnow()
    else:
        if _states_match(row.domain, canonical, row.state) and not config.get("simulate_mismatch"):
            skipped_command = True
            accepted = True
            new_state = row.state
        elif config.get("simulate_mismatch"):
            accepted = True
            new_state = row.state
        else:
            await _apply_local(row, canonical)
            await session.flush()
            refreshed = await session.get(HomeEntity, row.id)
            new_state = refreshed.state if refreshed is not None else row.state
            accepted = True

    matched = _states_match(row.domain, canonical, new_state)
    now = utcnow()
    evidence = evidence_base(
        source="homeassistant" if provider == "homeassistant" else "local",
        accepted=accepted,
        observed=matched,
        now=now,
        entity_id=row.entity_id,
        accepted_state=target_state,
        observed_state=new_state,
        idempotent=skipped_command,
    )
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
            "accepted": accepted,
        },
    )
    await session.flush()
    simulated = provider != "homeassistant"
    if not matched:
        result = {
            "ok": False,
            "error": "state_mismatch",
            "spoken": f"I asked to {canonical} {row.name} but it reads {new_state}.",
            "entity": entity_dict(row),
            "requested": canonical,
            "new_state": new_state,
            "simulated": simulated,
            "evidence": evidence,
        }
        await record_actuator(
            session, name="home_act", actor=actor, key=row.entity_id, result=result, target=row.name
        )
        return result
    spoken = f"{row.name} is now {new_state}."
    if simulated:
        spoken = f"Simulated home. {spoken}"
    if skipped_command:
        spoken = f"{row.name} is already {new_state}."
        if simulated:
            spoken = f"Simulated home. {spoken}"
    result = {
        "ok": True,
        "spoken": spoken,
        "entity": entity_dict(row),
        "requested": canonical,
        "new_state": new_state,
        "simulated": simulated,
        "idempotent_replay": skipped_command,
        "evidence": evidence,
    }
    await record_actuator(
        session, name="home_act", actor=actor, key=f"{row.entity_id}:{canonical}", result=result, target=row.name
    )
    return result


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
        "evidence": {
            "source": "local",
            "accepted": True,
            "observed": True,
            "accepted_state": target,
            "observed_state": row["state"],
            "entity_id": row["entity_id"],
        },
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


async def apply_observed_updates(session: AsyncSession, events: list) -> int:
    """Apply HA webhook state onto owned inventory. Unknown devices are ignored."""

    updated = 0
    for event in events:
        event_type = getattr(event, "event_type", None)
        payload = getattr(event, "payload", None)
        if isinstance(event, dict):
            event_type = event.get("event_type")
            payload = event.get("payload")
        if event_type not in {None, "home.device.updated"}:
            continue
        if not isinstance(payload, dict):
            continue
        device = str(payload.get("entity_id") or payload.get("device_id") or payload.get("device") or "").strip()
        state = payload.get("state")
        if not device or state is None:
            continue
        row = (
            await session.execute(select(HomeEntity).where(HomeEntity.entity_id == device))
        ).scalar_one_or_none()
        if row is None:
            match = await match_home_entity(session, device)
            row = match.item if match.unique else None
        if row is None:
            continue
        row.state = str(state)
        attrs = payload.get("attributes")
        if isinstance(attrs, dict):
            merged = dict(row.attributes or {})
            merged.update(attrs)
            row.attributes = merged
        row.updated_at = utcnow()
        updated += 1
    if updated:
        await session.flush()
    return updated
