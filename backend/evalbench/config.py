"""Application settings and static model metadata."""

import logging
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    openrouter_api_key: str | None = None
    xai_api_key: str | None = None
    database_url: str = "sqlite+aiosqlite:///./evalbench.db"
    judge_model: str = "anthropic/claude-sonnet-4-5"
    litellm_timeout_seconds: float = 60.0
    max_concurrency: int = Field(default=4, ge=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(override=False)
    return Settings()


PROVIDER_PREFIXES: dict[str, str] = {
    "openai/": "openai",
    "anthropic/": "anthropic",
    "gemini/": "gemini",
    "xai/": "xai",
    "openrouter/": "openrouter",
    "voyage-": "openrouter",
    "voyage/": "openrouter",
    "cohere": "openrouter",
}

FAMILY_LABELS: dict[str, str] = {
    "openai/": "OpenAI",
    "anthropic/": "Anthropic",
    "gemini/": "Gemini",
    "xai/": "XAI",
    "openrouter/": "OpenRouter",
    "voyage-": "Voyage",
    "voyage/": "Voyage",
    "cohere": "Cohere",
}

MODEL_TIERS: dict[str, str] = {
    "openai/gpt-5.6": "frontier",
    "openai/gpt-4o": "frontier",
    "anthropic/claude-sonnet-4-5": "frontier",
    "openai/text-embedding-3-small": "embedding",
    "voyage-3": "embedding",
    "voyage/voyage-3": "embedding",
    "cohere": "embedding",
    "cohere/embed-v4.0": "embedding",
}

# Standard, non-batch USD rates per million tokens, retrieved 2026-07-15 from:
# https://developers.openai.com/api/docs/models (GPT-5.6)
# https://developers.openai.com/api/docs/models/gpt-4o
# https://developers.openai.com/api/docs/models/text-embedding-3-small
# https://platform.claude.com/docs/en/about-claude/pricing
# https://docs.voyageai.com/docs/pricing
# https://cohere.com/pricing
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "openai/gpt-5.6": (5.0, 30.0),
    "openai/gpt-4o": (2.5, 10.0),
    "anthropic/claude-sonnet-4-5": (3.0, 15.0),
    "openai/text-embedding-3-small": (0.02, 0.0),
    "voyage-3": (0.06, 0.0),
    "voyage/voyage-3": (0.06, 0.0),
    "cohere": (0.12, 0.0),
    "cohere/embed-v4.0": (0.12, 0.0),
}


def split_pipeline_model(model: str) -> tuple[str, str | None]:
    base_model, separator, chunk_strategy = model.partition("::")
    if not separator:
        return model, None
    return base_model, chunk_strategy


def provider_for_model(model: str) -> str:
    base_model, _ = split_pipeline_model(model)
    for prefix, provider in PROVIDER_PREFIXES.items():
        if base_model.startswith(prefix):
            return provider
    return "openrouter"


def family_for_model(model: str) -> str:
    base_model, _ = split_pipeline_model(model)
    for prefix, family in FAMILY_LABELS.items():
        if base_model.startswith(prefix):
            return family
    return "OpenRouter"


def calculate_cost_usd(
    model: str, prompt_tokens: int, completion_tokens: int
) -> float:
    base_model, _ = split_pipeline_model(model)
    rates = MODEL_PRICING.get(base_model)
    if rates is None:
        logger.warning("No pricing configured for model %s", base_model)
        return 0.0

    input_rate, output_rate = rates
    return (
        prompt_tokens * input_rate + completion_tokens * output_rate
    ) / 1_000_000
