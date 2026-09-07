"""Evie Mobile Bridge: one dispatcher Shortcut plus official URL invocation.

Apple-supported invocation:
  shortcuts://run-shortcut?name=...&input=text&text=...
  shortcuts://x-callback-url/run-shortcut?...&x-success=...

The signed .shortcut is generated on Home Station (macOS `shortcuts sign`)
and downloaded once per iPhone. Owner does not edit actions.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from . import BRIDGE_NAME, BRIDGE_PROTOCOL, BRIDGE_VERSION

_SIGNED_CACHE: dict[str, bytes] = {}


def _text(value: str) -> dict[str, Any]:
    return {
        "Value": {"string": value, "attachmentsByRange": {}},
        "WFSerializationType": "WFTextTokenString",
    }


def _input() -> dict[str, Any]:
    return {
        "Value": {"Type": "ExtensionInput"},
        "WFSerializationType": "WFTextTokenAttachment",
    }


def _out(action_uuid: str, name: str) -> dict[str, Any]:
    return {
        "Value": {
            "OutputUUID": action_uuid,
            "OutputName": name,
            "Type": "ActionOutput",
        },
        "WFSerializationType": "WFTextTokenAttachment",
    }


def _attach_in_string(action_uuid: str, name: str) -> dict[str, Any]:
    return {
        "Value": {
            "string": "\ufffc",
            "attachmentsByRange": {
                "{0, 1}": {
                    "OutputUUID": action_uuid,
                    "Type": "ActionOutput",
                    "OutputName": name,
                }
            },
        },
        "WFSerializationType": "WFTextTokenString",
    }


def _dict_item(key: str, value: dict[str, Any] | str) -> dict[str, Any]:
    serialized = _text(value) if isinstance(value, str) else value
    return {
        "WFItemType": 0,
        "WFKey": _text(key),
        "WFValue": serialized,
    }


def _action(identifier: str, params: dict[str, Any], action_uuid: str) -> dict[str, Any]:
    body = dict(params)
    body["UUID"] = action_uuid
    return {
        "WFWorkflowActionIdentifier": identifier,
        "WFWorkflowActionParameters": body,
    }


def workflow_dict(*, resolve_url: str, device_id: str) -> dict[str, Any]:
    """Unsigned Shortcut document. Security: resolve then execute `run` only."""

    u = {name: str(uuid.uuid4()).upper() for name in (
        "comment",
        "detect",
        "token",
        "resolve",
        "approved",
        "ok",
        "run",
        "kind",
        "complete_url",
        "claim_url",
        "completion",
        "claim",
        "claim_ok",
        "minutes",
        "title",
        "when",
        "url",
        "query",
        "message",
        "phone",
        "if_ok",
        "if_kind_timer",
        "if_kind_url",
        "if_kind_reminder",
        "if_kind_call",
        "if_kind_message",
        "if_kind_self",
        "complete",
        "done",
    )}
    actions: list[dict[str, Any]] = [
        _action(
            "is.workflow.actions.comment",
            {
                "WFCommentActionText": (
                    f"Evie Mobile Bridge v{BRIDGE_VERSION} protocol {BRIDGE_PROTOCOL}\n"
                    "Do not edit. This shortcut sends a one-time token to Home Station "
                    "and runs only the authorized action. Contacts stay on this iPhone."
                )
            },
            u["comment"],
        ),
        _action(
            "is.workflow.actions.detect.dictionary",
            {"WFInput": _input()},
            u["detect"],
        ),
        _action(
            "is.workflow.actions.getvalueforkey",
            {"WFDictionaryKey": "token", "WFInput": _out(u["detect"], "Dictionary")},
            u["token"],
        ),
        _action(
            "is.workflow.actions.downloadurl",
            {
                "Advanced": True,
                "WFHTTPMethod": "POST",
                "WFHTTPBodyType": "JSON",
                "ShowHeaders": False,
                "WFJSONValues": {
                    "Value": {
                        "WFDictionaryFieldValueItems": [
                            _dict_item("token", _attach_in_string(u["token"], "Dictionary Value")),
                            _dict_item("device_id", device_id),
                            _dict_item("protocol", str(BRIDGE_PROTOCOL)),
                        ]
                    },
                    "WFSerializationType": "WFDictionaryFieldValue",
                },
                "WFURL": _text(resolve_url),
            },
            u["resolve"],
        ),
        _action(
            "is.workflow.actions.detect.dictionary",
            {"WFInput": _out(u["resolve"], "Contents of URL")},
            u["approved"],
        ),
        _action(
            "is.workflow.actions.getvalueforkey",
            {"WFDictionaryKey": "ok", "WFInput": _out(u["approved"], "Dictionary")},
            u["ok"],
        ),
        _action(
            "is.workflow.actions.conditional",
            {
                "GroupingIdentifier": u["if_ok"],
                "WFControlFlowMode": 0,
                "WFCondition": 4,
                "WFConditionalActionString": "false",
                "WFInput": _out(u["ok"], "Dictionary Value"),
            },
            str(uuid.uuid4()).upper(),
        ),
        _action(
            "is.workflow.actions.notification",
            {
                "WFNotificationActionTitle": "Evie",
                "WFNotificationActionBody": "This Mobile Action is no longer valid.",
            },
            str(uuid.uuid4()).upper(),
        ),
        _action("is.workflow.actions.exit", {}, str(uuid.uuid4()).upper()),
        _action(
            "is.workflow.actions.conditional",
            {"GroupingIdentifier": u["if_ok"], "WFControlFlowMode": 2},
            str(uuid.uuid4()).upper(),
        ),
        _action(
            "is.workflow.actions.getvalueforkey",
            {"WFDictionaryKey": "run", "WFInput": _out(u["approved"], "Dictionary")},
            u["run"],
        ),
        _action(
            "is.workflow.actions.getvalueforkey",
            {"WFDictionaryKey": "kind", "WFInput": _out(u["run"], "Dictionary Value")},
            u["kind"],
        ),
        _action(
            "is.workflow.actions.getvalueforkey",
            {"WFDictionaryKey": "complete_url", "WFInput": _out(u["run"], "Dictionary Value")},
            u["complete_url"],
        ),
        _action(
            "is.workflow.actions.getvalueforkey",
            {"WFDictionaryKey": "claim_url", "WFInput": _out(u["run"], "Dictionary Value")},
            u["claim_url"],
        ),
        _action(
            "is.workflow.actions.getvalueforkey",
            {
                "WFDictionaryKey": "completion_token",
                "WFInput": _out(u["run"], "Dictionary Value"),
            },
            u["completion"],
        ),
        _action(
            "is.workflow.actions.downloadurl",
            {
                "Advanced": True,
                "WFHTTPMethod": "POST",
                "WFHTTPBodyType": "JSON",
                "WFJSONValues": {
                    "Value": {
                        "WFDictionaryFieldValueItems": [
                            _dict_item(
                                "completion_token",
                                _attach_in_string(u["completion"], "Dictionary Value"),
                            )
                        ]
                    },
                    "WFSerializationType": "WFDictionaryFieldValue",
                },
                "WFURL": _attach_in_string(u["claim_url"], "Dictionary Value"),
            },
            u["claim"],
        ),
    ]

    def if_kind(kind: str, group: str, body: list[dict[str, Any]]) -> None:
        actions.append(
            _action(
                "is.workflow.actions.conditional",
                {
                    "GroupingIdentifier": group,
                    "WFControlFlowMode": 0,
                    "WFCondition": 4,
                    "WFConditionalActionString": kind,
                    "WFInput": _out(u["kind"], "Dictionary Value"),
                },
                str(uuid.uuid4()).upper(),
            )
        )
        actions.extend(body)
        actions.append(
            _action(
                "is.workflow.actions.conditional",
                {"GroupingIdentifier": group, "WFControlFlowMode": 2},
                str(uuid.uuid4()).upper(),
            )
        )

    minutes = str(uuid.uuid4()).upper()
    if_kind(
        "timer",
        u["if_kind_timer"],
        [
            _action(
                "is.workflow.actions.getvalueforkey",
                {
                    "WFDictionaryKey": "duration_minutes",
                    "WFInput": _out(u["run"], "Dictionary Value"),
                },
                minutes,
            ),
            _action(
                "is.workflow.actions.timer.start",
                {
                    "WFDuration": {
                        "Value": {
                            "Magnitude": "\ufffc",
                            "Unit": "min",
                            "attachmentsByRange": {
                                "{0, 1}": {
                                    "OutputUUID": minutes,
                                    "Type": "ActionOutput",
                                    "OutputName": "Dictionary Value",
                                }
                            },
                        },
                        "WFSerializationType": "WFQuantityFieldValue",
                    }
                },
                str(uuid.uuid4()).upper(),
            ),
        ],
    )
    url_get = str(uuid.uuid4()).upper()
    if_kind(
        "open_url",
        u["if_kind_url"],
        [
            _action(
                "is.workflow.actions.getvalueforkey",
                {"WFDictionaryKey": "url", "WFInput": _out(u["run"], "Dictionary Value")},
                url_get,
            ),
            _action(
                "is.workflow.actions.openurl",
                {"WFInput": _out(url_get, "Dictionary Value")},
                str(uuid.uuid4()).upper(),
            ),
        ],
    )
    title_get = str(uuid.uuid4()).upper()
    when_get = str(uuid.uuid4()).upper()
    if_kind(
        "reminder",
        u["if_kind_reminder"],
        [
            _action(
                "is.workflow.actions.getvalueforkey",
                {"WFDictionaryKey": "title", "WFInput": _out(u["run"], "Dictionary Value")},
                title_get,
            ),
            _action(
                "is.workflow.actions.getvalueforkey",
                {
                    "WFDictionaryKey": "when_iso",
                    "WFInput": _out(u["run"], "Dictionary Value"),
                },
                when_get,
            ),
            _action(
                "is.workflow.actions.addnewreminder",
                {
                    "WFCalendarItemTitle": _attach_in_string(title_get, "Dictionary Value"),
                    "WFCalendarItemAlertEnabled": True,
                    "WFAlertCustomTime": _attach_in_string(when_get, "Dictionary Value"),
                },
                str(uuid.uuid4()).upper(),
            ),
        ],
    )
    query_get = str(uuid.uuid4()).upper()
    if_kind(
        "call",
        u["if_kind_call"],
        [
            _action(
                "is.workflow.actions.getvalueforkey",
                {
                    "WFDictionaryKey": "contact_query",
                    "WFInput": _out(u["run"], "Dictionary Value"),
                },
                query_get,
            ),
            _action(
                "is.workflow.actions.call",
                {"WFCallActionPhoneNumber": _attach_in_string(query_get, "Dictionary Value")},
                str(uuid.uuid4()).upper(),
            ),
        ],
    )
    msg_to = str(uuid.uuid4()).upper()
    msg_body = str(uuid.uuid4()).upper()
    if_kind(
        "message",
        u["if_kind_message"],
        [
            _action(
                "is.workflow.actions.getvalueforkey",
                {
                    "WFDictionaryKey": "contact_query",
                    "WFInput": _out(u["run"], "Dictionary Value"),
                },
                msg_to,
            ),
            _action(
                "is.workflow.actions.getvalueforkey",
                {
                    "WFDictionaryKey": "message",
                    "WFInput": _out(u["run"], "Dictionary Value"),
                },
                msg_body,
            ),
            _action(
                "is.workflow.actions.sendmessage",
                {
                    "IntentAppDefinition": "com.apple.MobileSMS",
                    "WFSendMessageActionRecipients": _attach_in_string(msg_to, "Dictionary Value"),
                    "WFSendMessageContent": _attach_in_string(msg_body, "Dictionary Value"),
                },
                str(uuid.uuid4()).upper(),
            ),
        ],
    )
    if_kind("self_test", u["if_kind_self"], [])

    _text("executed")
    actions.append(
        _action(
            "is.workflow.actions.downloadurl",
            {
                "Advanced": True,
                "WFHTTPMethod": "POST",
                "WFHTTPBodyType": "JSON",
                "WFJSONValues": {
                    "Value": {
                        "WFDictionaryFieldValueItems": [
                            _dict_item(
                                "completion_token",
                                _attach_in_string(u["completion"], "Dictionary Value"),
                            ),
                            _dict_item("status", "executed"),
                            _dict_item("result", "EXECUTED"),
                        ]
                    },
                    "WFSerializationType": "WFDictionaryFieldValue",
                },
                "WFURL": _attach_in_string(u["complete_url"], "Dictionary Value"),
            },
            u["complete"],
        )
    )
    actions.append(
        _action(
            "is.workflow.actions.notification",
            {
                "WFNotificationActionTitle": "Evie",
                "WFNotificationActionBody": "Mobile Action finished.",
            },
            u["done"],
        )
    )

    return {
        "WFWorkflowClientRelease": "18.0",
        "WFWorkflowClientVersion": "2605.0.5",
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowOutputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowTypes": ["NCWidget", "WatchKit"],
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 4282601983,
            "WFWorkflowIconGlyphNumber": 59749,
        },
        "WFQuickActionSurfaces": [],
        "WFWorkflowActions": actions,
        "WFWorkflowName": BRIDGE_NAME,
    }


def unsigned_bytes(*, origin: str, device_id: str) -> bytes:
    resolve_url = origin.rstrip("/") + "/v1/device-gateway/mobile-actions/resolve"
    return plistlib.dumps(workflow_dict(resolve_url=resolve_url, device_id=device_id))


def signed_shortcut(*, origin: str, device_id: str) -> tuple[bytes, str]:
    """Return signed bytes when `shortcuts sign` is available; else unsigned plist."""

    key = f"{origin}|{device_id}|{BRIDGE_VERSION}"
    cached = _SIGNED_CACHE.get(key)
    if cached:
        return cached, "signed" if cached[:2] != b"bP" and not cached.startswith(b"<?xml") else "unsigned"
    raw = unsigned_bytes(origin=origin, device_id=device_id)
    cli = shutil.which("shortcuts")
    if not cli:
        _SIGNED_CACHE[key] = raw
        return raw, "unsigned"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "EvieMobileBridge.shortcut"
        dst = Path(tmp) / "EvieMobileBridge.signed.shortcut"
        src.write_bytes(raw)
        try:
            subprocess.run(
                [cli, "sign", "--mode", "anyone", "--input", str(src), "--output", str(dst)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            _SIGNED_CACHE[key] = raw
            return raw, "unsigned"
        data = dst.read_bytes()
        if not data:
            _SIGNED_CACHE[key] = raw
            return raw, "unsigned"
        _SIGNED_CACHE[key] = data
        return data, "signed"


def callback_urls(origin: str) -> dict[str, str]:
    base = origin.rstrip("/") + "/evie/"
    return {"success": base, "cancel": base, "error": base}


def build_run_url(
    *,
    action_id: str,
    token: str,
    origin: str,
    callbacks: dict[str, str] | None = None,
) -> str:
    payload = json.dumps(
        {
            "protocol": BRIDGE_PROTOCOL,
            "action_id": action_id,
            "token": token,
        },
        separators=(",", ":"),
    )
    params = {
        "name": BRIDGE_NAME,
        "input": "text",
        "text": payload,
    }
    query = urlencode(params, quote_via=quote)
    if callbacks:
        extra = {
            "x-success": callbacks.get("success") or "",
            "x-cancel": callbacks.get("cancel") or "",
            "x-error": callbacks.get("error") or "",
        }
        return (
            "shortcuts://x-callback-url/run-shortcut?"
            + query
            + "&"
            + urlencode({k: v for k, v in extra.items() if v}, quote_via=quote)
        )
    return "shortcuts://run-shortcut?" + query


def import_url(download_url: str) -> str:
    return "shortcuts://import-shortcut?url=" + quote(download_url, safe="")
