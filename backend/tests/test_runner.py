import logging

import pytest

from evalbench.config import (
    calculate_cost_usd,
    family_for_model,
    provider_for_model,
    split_pipeline_model,
)


@pytest.mark.parametrize(
    ("model", "provider", "family"),
    [
        ("openai/gpt-4o", "openai", "OpenAI"),
        ("anthropic/claude-sonnet-4-5", "anthropic", "Anthropic"),
        ("gemini/gemini-2.5-pro", "gemini", "Gemini"),
        ("xai/grok-4", "xai", "XAI"),
        ("openrouter/openai/gpt-4o", "openrouter", "OpenRouter"),
        ("voyage-3", "openrouter", "Voyage"),
        ("voyage/voyage-3", "openrouter", "Voyage"),
        ("cohere", "openrouter", "Cohere"),
        ("cohere/embed-v4.0", "openrouter", "Cohere"),
        (
            "openai/text-embedding-3-small::fixed_512",
            "openai",
            "OpenAI",
        ),
    ],
)
def test_known_model_metadata(model: str, provider: str, family: str) -> None:
    assert provider_for_model(model) == provider
    assert family_for_model(model) == family


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (
            "openai/text-embedding-3-small::fixed_512",
            ("openai/text-embedding-3-small", "fixed_512"),
        ),
        ("openai/gpt-4o", ("openai/gpt-4o", None)),
    ],
)
def test_split_pipeline_model(
    model: str, expected: tuple[str, str | None]
) -> None:
    assert split_pipeline_model(model) == expected


@pytest.mark.parametrize(
    ("model", "prompt_tokens", "completion_tokens", "expected"),
    [
        ("openai/gpt-5.6", 200_000, 10_000, 1.3),
        ("openai/gpt-4o", 400_000, 100_000, 2.0),
        ("anthropic/claude-sonnet-4-5", 100_000, 20_000, 0.6),
        ("openai/text-embedding-3-small", 1_000_000, 0, 0.02),
        (
            "openai/text-embedding-3-small::recursive",
            500_000,
            0,
            0.01,
        ),
        ("voyage-3", 1_000_000, 0, 0.06),
        ("voyage/voyage-3", 1_000_000, 0, 0.06),
        ("cohere", 1_000_000, 0, 0.12),
        ("cohere/embed-v4.0", 1_000_000, 0, 0.12),
    ],
)
def test_calculate_known_model_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    expected: float,
) -> None:
    assert calculate_cost_usd(model, prompt_tokens, completion_tokens) == pytest.approx(
        expected
    )


def test_unknown_model_cost_is_zero_and_logs_only_model_name(caplog) -> None:
    model = "unknown-provider/unknown-model"

    with caplog.at_level(logging.WARNING, logger="evalbench.config"):
        cost = calculate_cost_usd(model, 123_456, 654_321)

    assert cost == 0.0
    assert len(caplog.records) == 1
    assert model in caplog.text
    assert "123456" not in caplog.text
    assert "654321" not in caplog.text
