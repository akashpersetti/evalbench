"""Schema validation and JSON extraction for the structured suite."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, create_model

from evalbench.judge import Judge
from evalbench.suites.base import Suite, Task


_FENCED_JSON = re.compile(
    r"^\s*```json[ \t]*\r?\n(?P<content>.*?)\r?\n?```\s*$",
    re.DOTALL | re.IGNORECASE,
)
_MODEL_CONFIG = ConfigDict(strict=True, extra="forbid")
_MISSING = object()
_FREE_TEXT_RUBRIC = (
    "Semantic equivalence and factual completeness; ignore wording differences."
)


class StructuredSuite(Suite):
    """Evaluate schema adherence and exact structured-answer accuracy."""

    name = "structured"
    metric_keys = [
        "first_attempt_valid",
        "schema_valid",
        "retries_to_valid",
        "retry_cost_usd",
        "field_accuracy",
    ]
    display_metrics = [
        {
            "key": "schema_valid",
            "label": "Schema valid",
            "format": "percent",
            "higher_is_better": True,
        },
        {
            "key": "first_attempt_valid",
            "label": "First-attempt valid",
            "format": "percent",
            "higher_is_better": True,
        },
        {
            "key": "field_accuracy",
            "label": "Field accuracy",
            "format": "percent",
            "higher_is_better": True,
        },
        {
            "key": "retries_to_valid",
            "label": "Retries to valid",
            "format": "number",
            "higher_is_better": False,
        },
        {
            "key": "retry_cost_usd",
            "label": "Retry cost",
            "format": "currency",
            "higher_is_better": False,
        },
    ]

    def load_tasks(self, domain: str) -> list[Task]:
        """Return no tasks until the structured dataset is installed."""
        return []

    def build_prompt(self, task: Task) -> list[dict]:
        schema = task.payload["schema"]
        return [
            {
                "role": "system",
                "content": (
                    "Return only JSON conforming to the supplied schema, with no markdown."
                ),
            },
            {
                "role": "user",
                "content": f"{task.prompt}\n\nSchema:\n{_canonical_schema(schema)}",
            },
        ]

    def evaluate(
        self, task: Task, raw_output: str, judge: Judge
    ) -> dict[str, float]:
        schema = task.payload["schema"]
        parsed, schema_valid, validation_error = validate_output(raw_output, schema)
        first_attempt_valid = schema_valid
        latest_parsed = parsed
        retries = 0
        retry_cost_usd = 0.0
        original_messages = self.build_prompt(task)

        while not schema_valid and retries < 3:
            context = task._execution_context
            if context is None:
                raise RuntimeError("structured retries require an execution context")
            result = context.complete(
                build_retry_messages(
                    original_messages,
                    raw_output,
                    validation_error or "invalid output",
                    schema,
                )
            )
            retries += 1
            retry_cost_usd += result.cost_usd
            raw_output = result.text
            parsed, schema_valid, validation_error = validate_output(raw_output, schema)
            if parsed is not None:
                latest_parsed = parsed

        return {
            "first_attempt_valid": float(first_attempt_valid),
            "schema_valid": float(schema_valid),
            "retries_to_valid": float(retries),
            "retry_cost_usd": float(retry_cost_usd),
            "field_accuracy": float(field_accuracy(task, latest_parsed, judge)),
        }


def build_retry_messages(
    original_messages: list[dict],
    invalid_output: str,
    validation_error: str,
    schema: dict[str, Any],
) -> list[dict]:
    """Append a correction turn without exposing the expected answer."""
    return [
        *original_messages,
        {"role": "assistant", "content": invalid_output},
        {
            "role": "user",
            "content": (
                f"Your previous output was invalid: {validation_error}. "
                "Return only JSON conforming to this schema, with no markdown.\n"
                f"Schema:\n{_canonical_schema(schema)}"
            ),
        },
    ]


def iter_expected_leaves(expected: Any, pointer: str = "") -> list[tuple[str, Any]]:
    """Flatten an expected value into RFC 6901-style leaf pointers."""
    if isinstance(expected, dict):
        if not expected:
            return [] if pointer == "" else [(pointer, expected)]
        return [
            leaf
            for key, value in expected.items()
            for leaf in iter_expected_leaves(
                value, f"{pointer}/{_escape_pointer_token(key)}"
            )
        ]
    if isinstance(expected, list):
        if not expected:
            return [(pointer, expected)]
        return [
            leaf
            for index, value in enumerate(expected)
            for leaf in iter_expected_leaves(value, f"{pointer}/{index}")
        ]
    return [(pointer, expected)]


def field_accuracy(task: Task, parsed: Any | None, judge: Judge) -> float:
    """Score expected leaves exactly, delegating declared free-text leaves."""
    expected = task.payload["expected"]
    if expected == {}:
        return 1.0
    leaves = iter_expected_leaves(expected)
    if parsed is None:
        return 0.0

    free_text_fields = set(task.payload.get("free_text_fields", []))
    scores: list[float] = []
    for pointer, expected in leaves:
        actual = _value_at_pointer(parsed, pointer)
        if actual is _MISSING or type(actual) is not type(expected):
            scores.append(0.0)
        elif pointer in free_text_fields:
            scores.append(
                float(
                    judge.score_free_text(
                        prompt=task.prompt,
                        expected=str(expected),
                        actual=str(actual),
                        rubric=_FREE_TEXT_RUBRIC,
                    )
                )
            )
        else:
            scores.append(float(actual == expected))
    return sum(scores) / len(scores)


def _canonical_schema(schema: dict[str, Any]) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def _escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _value_at_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    current = value
    for token in pointer.removeprefix("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current.get(token, _MISSING)
        elif isinstance(current, list) and token.isdecimal():
            index = int(token)
            current = current[index] if index < len(current) else _MISSING
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


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
            _validate_default(annotation, default, field_path)
        else:
            _unsupported(
                field_path,
                "optional non-null fields require an explicit default",
            )
        fields[field_name] = (annotation, default)

    return create_model(name, __config__=_MODEL_CONFIG, **fields)


def _validate_default(annotation: Any, default: Any, path: str) -> None:
    try:
        TypeAdapter(annotation).validate_python(default, strict=True)
    except ValidationError as error:
        raise ValueError(f"invalid default at {path}") from error


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
