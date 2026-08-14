"""Tool-invocation validation (Invocation-Refiner pattern).

The gateway pre-validates every tool call returned by a model before anything is
dispatched: the name must exist in the provided specs, arguments must match the
declared JSON-schema subset, sensitive tools need an explicit permission gate,
and missing optional arguments with defaults are rectified deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from app.contracts import ToolCall, ToolSpec


@dataclass
class ValidatedToolCall:
    """Result of validating one model tool invocation."""

    call: ToolCall
    status: str  # ok | rectified | rejected
    issues: list[str] = field(default_factory=list)
    rectified_arguments: dict | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.call.id,
            "name": self.call.name,
            "arguments": self.call.arguments,
            "status": self.status,
            "issues": self.issues,
            "rectified_arguments": self.rectified_arguments,
        }


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _matches_type(value: Any, json_type: str) -> bool:
    if json_type not in _JSON_TYPES:
        return True  # unknown schema types are not grounds for rejection
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, cast(type, _JSON_TYPES[json_type]))


def _schema_issues(name: str, value: Any, schema: dict) -> list[str]:
    """Validate one value against the supported JSON-schema subset."""

    issues: list[str] = []
    json_type = schema.get("type")
    if json_type and not _matches_type(value, json_type):
        return [f"argument '{name}' must be {json_type}"]
    if json_type in ("integer", "number") and isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            issues.append(f"argument '{name}' must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            issues.append(f"argument '{name}' must be <= {schema['maximum']}")
    if json_type == "string" and isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            issues.append(f"argument '{name}' must be at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            issues.append(f"argument '{name}' must be at most {schema['maxLength']} characters")
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        choices = ", ".join(str(e) for e in enum)
        issues.append(f"argument '{name}' must be one of: {choices}")
    return issues


def validate_arguments(arguments: dict, parameters: dict) -> tuple[dict, list[str]]:
    """Validate and deterministically repair arguments against a JSON-schema subset.

    Returns (effective_arguments, issues). Effective arguments include defaults for
    missing optional properties; issues that are not auto-repairable are returned
    so the caller can reject the invocation.
    """

    if not parameters:
        return {}, []
    issues: list[str] = []
    effective = dict(arguments)
    properties = parameters.get("properties") or {}
    required = parameters.get("required") or []
    allow_extra = parameters.get("additionalProperties", True) is not False

    for name in required:
        if name not in effective:
            issues.append(f"missing required argument '{name}'")
            continue
        schema = properties.get(name) or {}
        issues.extend(_schema_issues(name, effective[name], schema))

    for name, schema in properties.items():
        if name in effective:
            if effective[name] is None and name not in required:
                # Optional JSON null / filled None — omit, do not type-check.
                del effective[name]
                continue
            issues.extend(_schema_issues(name, effective[name], schema))
        elif "default" in schema and schema["default"] is not None:
            effective[name] = schema["default"]

    if not allow_extra:
        for name in effective:
            if name not in properties:
                issues.append(f"unknown argument '{name}'")

    return effective, issues


def validate_output(result: Any, output_schema: dict) -> list[str]:
    """Check a dispatched result against the tool's declared output shape.

    Only the top-level shape is enforced today: object/array type and required
    top-level keys. Deeper property validation is applied per tool as needed.
    """

    if not output_schema:
        return []
    expected_type = output_schema.get("type")
    if expected_type == "object":
        if not isinstance(result, dict):
            return [f"output must be {expected_type}"]
        return [
            f"output missing required property '{key}'"
            for key in output_schema.get("required") or []
            if key not in result
        ]
    if expected_type == "array" and not isinstance(result, list):
        return [f"output must be {expected_type}"]
    return []


def validate_tool_call(
    call: ToolCall,
    spec: ToolSpec,
    *,
    sensitive_allowed: bool,
) -> ValidatedToolCall:
    if spec.sensitive and not sensitive_allowed:
        return ValidatedToolCall(
            call=call,
            status="rejected",
            issues=[f"'{spec.name}' requires explicit permission before execution"],
        )
    effective, issues = validate_arguments(call.arguments, spec.parameters)
    if issues:
        return ValidatedToolCall(
            call=call,
            status="rejected",
            issues=issues,
            rectified_arguments=effective,
        )
    if effective != call.arguments:
        return ValidatedToolCall(
            call=call,
            status="rectified",
            issues=["missing optional arguments filled with declared defaults"],
            rectified_arguments=effective,
        )
    return ValidatedToolCall(call=call, status="ok")


def validate_tool_calls(
    calls: list[ToolCall] | tuple[ToolCall, ...],
    specs: list[ToolSpec] | tuple[ToolSpec, ...],
    *,
    sensitive_allowed: bool = False,
) -> list[ValidatedToolCall]:
    """Pre-validate every tool call from the model before execution."""

    by_name = {spec.name: spec for spec in specs}
    validated: list[ValidatedToolCall] = []
    for call in calls:
        spec = by_name.get(call.name)
        if spec is None:
            validated.append(
                ValidatedToolCall(
                    call=call,
                    status="rejected",
                    issues=[f"unknown tool '{call.name}'"],
                )
            )
            continue
        validated.append(
            validate_tool_call(call, spec, sensitive_allowed=sensitive_allowed)
        )
    return validated
