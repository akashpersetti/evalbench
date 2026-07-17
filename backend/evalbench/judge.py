"""Generic text and JSON judge operations."""

import json
import math
import random
import re
from collections.abc import Callable
from typing import Any

import litellm

from evalbench.config import get_settings


class JudgeResponseError(ValueError):
    """Raised when a judge response does not match the requested structure."""


_JSON_FENCE = re.compile(
    r"\A```json[ \t]*(?:\r?\n)?(?P<body>.*?)(?:\r?\n)?```[ \t]*\Z",
    flags=re.IGNORECASE | re.DOTALL,
)


class Judge:
    def __init__(
        self,
        model: str,
        *,
        completion_fn: Callable[..., Any] | None = None,
        timeout_seconds: float | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.model = model
        self._completion_fn = (
            completion_fn if completion_fn is not None else litellm.completion
        )
        self.timeout_seconds = (
            get_settings().litellm_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        self.rng = rng if rng is not None else random.Random()

    def complete_text(
        self,
        messages: list[dict],
        *,
        response_format: dict[str, str] | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {}
        if response_format is not None:
            kwargs["response_format"] = response_format
        response = self._completion_fn(
            model=self.model,
            messages=messages,
            timeout=self.timeout_seconds,
            **kwargs,
        )
        choices = getattr(response, "choices", [])
        message = getattr(choices[0], "message", None)
        return str(getattr(message, "content", "") or "")

    def _json_response_format(self) -> dict[str, str] | None:
        """Request provider-enforced JSON output where litellm reports support.

        Falls back to None (relying on prompt instructions plus the fence
        stripping below) for models litellm doesn't recognize or that don't
        support response_format - avoids hard-failing on an unusual judge_model.
        """
        try:
            supported = litellm.get_supported_openai_params(model=self.model) or []
        except Exception:
            return None
        return {"type": "json_object"} if "response_format" in supported else None

    def complete_json(self, messages: list[dict]) -> dict[str, Any]:
        text = self.complete_text(
            messages, response_format=self._json_response_format()
        ).strip()
        fenced = _JSON_FENCE.fullmatch(text)
        if fenced is not None:
            text = fenced.group("body").strip()

        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            raise JudgeResponseError(
                f"Judge response was not valid JSON: {text[:300]!r}"
            ) from None
        if not isinstance(parsed, dict):
            raise JudgeResponseError(
                f"Judge response JSON root was not an object: {text[:300]!r}"
            )
        return parsed

    def score_free_text(
        self,
        *,
        prompt: str,
        expected: str,
        actual: str,
        rubric: str,
    ) -> float:
        result = self.complete_json(
            [
                {
                    "role": "system",
                    "content": (
                        "Score the answer using the supplied rubric. Return only "
                        'the JSON object {"score": <number from 0 to 1>}.'
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Prompt:\n{prompt}\n\nExpected answer:\n{expected}"
                        f"\n\nActual answer:\n{actual}\n\nRubric:\n{rubric}"
                    ),
                },
            ]
        )
        raw_score = result.get("score")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise JudgeResponseError(
                f"Judge response did not contain a numeric score: {result!r}"
            )
        score = float(raw_score)
        if not math.isfinite(score):
            raise JudgeResponseError("Judge response score was not finite")
        return min(1.0, max(0.0, score))
