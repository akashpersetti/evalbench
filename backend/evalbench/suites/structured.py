"""Schema validation and JSON extraction for the structured suite."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, create_model


_FENCED_JSON = re.compile(
    r"^\s*```json[ \t]*\r?\n(?P<content>.*?)\r?\n?```\s*$",
    re.DOTALL | re.IGNORECASE,
)
_MODEL_CONFIG = ConfigDict(strict=True, extra="forbid")


def annotation_from_schema(name: str, schema: dict[str, Any]) -> Any:
    """Return a strict Python annotation for the supported schema subset."""
    return _annotation_from_schema(name, schema, "$")


def model_from_schema(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Build a strict Pydantic model for an object schema."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        _unsupported("$", "root schema must be an object")
    return _object_model(name, schema, "$")


def extract_json(raw_output: str) -> Any:
    """Extract one object or array JSON value from plain or fenced output."""
    fence = _FENCED_JSON.match(raw_output)
    if fence is not None:
        return _load_json(fence.group("content").strip())

    start = next(
        (index for index, character in enumerate(raw_output) if character in "[{"),
        None,
    )
    if start is None:
        raise ValueError("invalid JSON: no object or array found")

    end = _matching_json_end(raw_output, start)
    if end is None:
        raise ValueError("invalid JSON: incomplete value")

    trailing = raw_output[end + 1 :]
    if "{" in trailing or "[" in trailing or _has_json_value_sequence(trailing):
        raise ValueError("ambiguous JSON: found another value")
    return _load_json(raw_output[start : end + 1])


def validate_output(
    raw_output: str, schema: dict[str, Any]
) -> tuple[Any | None, bool, str | None]:
    """Parse and validate output while retaining the model's original value."""
    try:
        parsed = extract_json(raw_output)
    except ValueError as error:
        return None, False, str(error)

    model = model_from_schema("StructuredOutput", schema)
    try:
        model.model_validate(parsed)
    except ValidationError as error:
        return parsed, False, _concise_validation_error(error)
    return parsed, True, None


def _annotation_from_schema(name: str, schema: dict[str, Any], path: str) -> Any:
    if not isinstance(schema, dict):
        _unsupported(path, "schema must be an object")

    if "anyOf" in schema:
        return _nullable_annotation(name, schema, path)

    schema_type = schema.get("type")
    if schema_type == "object":
        return _object_model(name, schema, path)
    if schema_type == "array":
        _require_only_keys(schema, {"type", "items", "title", "default"}, path)
        if not isinstance(schema.get("items"), dict):
            _unsupported(f"{path}.items", "array items must be a schema object")
        return list[_annotation_from_schema(f"{name}Item", schema["items"], f"{path}.items")]
    if schema_type == "string":
        _require_only_keys(schema, {"type", "enum", "title", "default"}, path)
        if "enum" not in schema:
            return str
        enum_values = schema["enum"]
        if (
            not isinstance(enum_values, list)
            or not enum_values
            or not all(isinstance(value, str) for value in enum_values)
        ):
            _unsupported(f"{path}.enum", "string enum must be a non-empty list of strings")
        return Literal[tuple(enum_values)]
    if schema_type == "integer":
        _require_only_keys(schema, {"type", "title", "default"}, path)
        return int
    if schema_type == "number":
        _require_only_keys(schema, {"type", "title", "default"}, path)
        return float
    if schema_type == "boolean":
        _require_only_keys(schema, {"type", "title", "default"}, path)
        return bool
    _unsupported(path, "unsupported schema type")


def _object_model(name: str, schema: dict[str, Any], path: str) -> type[BaseModel]:
    _require_only_keys(
        schema,
        {"type", "properties", "required", "additionalProperties", "title", "default"},
        path,
    )
    if not isinstance(schema.get("properties"), dict):
        _unsupported(f"{path}.properties", "object properties must be an object")
    properties = schema["properties"]
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(field, str) for field in required):
        _unsupported(f"{path}.required", "required must be a list of property names")
    if any(field not in properties for field in required):
        _unsupported(f"{path}.required", "required names must be declared properties")
    if schema.get("additionalProperties", False) is not False:
        _unsupported(f"{path}.additionalProperties", "must be false")

    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, field_schema in properties.items():
        if not isinstance(field_name, str):
            _unsupported(f"{path}.properties", "property names must be strings")
        field_path = f"{path}.properties.{field_name}"
        annotation = _annotation_from_schema(
            f"{name}_{_model_name_part(field_name)}", field_schema, field_path
        )
        if field_name in required:
            default = ...
        elif _permits_null(field_schema):
            default = None
        elif isinstance(field_schema, dict) and "default" in field_schema:
            default = field_schema["default"]
        else:
            _unsupported(
                field_path,
                "optional non-null fields require an explicit default",
            )
        fields[field_name] = (annotation, default)

    return create_model(name, __config__=_MODEL_CONFIG, **fields)


def _nullable_annotation(name: str, schema: dict[str, Any], path: str) -> Any:
    _require_only_keys(schema, {"anyOf", "title", "default"}, path)
    branches = schema["anyOf"]
    if not isinstance(branches, list) or len(branches) != 2:
        _unsupported(f"{path}.anyOf", "must contain one value branch and one null branch")
    null_branches = [branch for branch in branches if branch == {"type": "null"}]
    if len(null_branches) != 1:
        _unsupported(f"{path}.anyOf", "must contain exactly one {type: null} branch")
    value_schema = next(branch for branch in branches if branch != {"type": "null"})
    if not isinstance(value_schema, dict) or "anyOf" in value_schema:
        _unsupported(f"{path}.anyOf", "nullable value branch must be a supported schema")
    return _annotation_from_schema(name, value_schema, f"{path}.anyOf") | None


def _permits_null(schema: Any) -> bool:
    return (
        isinstance(schema, dict)
        and isinstance(schema.get("anyOf"), list)
        and {"type": "null"} in schema["anyOf"]
    )


def _require_only_keys(schema: dict[str, Any], allowed: set[str], path: str) -> None:
    unsupported = sorted(set(schema) - allowed)
    if unsupported:
        _unsupported(path, f"unsupported keyword {unsupported[0]!r}")


def _matching_json_end(value: str, start: int) -> int | None:
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(value)):
        character = value[index]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
            if depth == 0:
                return index
            if depth < 0:
                return None
    return None


def _load_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON: {error.msg}") from error


def _has_json_value_sequence(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    try:
        decoder = json.JSONDecoder()
        _, end = decoder.raw_decode(candidate)
    except json.JSONDecodeError:
        return False
    trailing = candidate[end:]
    if not trailing.strip():
        return True
    if not trailing[0].isspace():
        return False
    additional = trailing.lstrip()
    try:
        _, end = decoder.raw_decode(additional)
    except json.JSONDecodeError:
        return False
    return end == len(additional) or additional[end].isspace()


def _concise_validation_error(error: ValidationError) -> str:
    detail = error.errors(include_url=False)[0]
    location = ".".join(str(part) for part in detail["loc"])
    return f"{detail['type']} at {location or '$'}"


def _model_name_part(field_name: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in field_name)


def _unsupported(path: str, message: str) -> None:
    raise ValueError(f"unsupported schema at {path}: {message}")
