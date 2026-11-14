"""Configuration: secrets from .env / environment, business rules from config/*.yaml.

The YAML files and prompt are copied unchanged from the reference project
`celstream_bd_poc` so business rules stay identical.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float | None) -> float | None:
    value = _env(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_path: Path = ROOT / (_env("LIVE_DATABASE_PATH") or "data/bd_live.db")
    log_path: Path = ROOT / "logs/bd_live.log"

    theirstack_api_key: str = field(default_factory=lambda: _env("THEIRSTACK_API_KEY"), repr=False)
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"), repr=False)
    apollo_api_key: str = field(default_factory=lambda: _env("APOLLO_API_KEY"), repr=False)
    hubspot_access_token: str = field(
        default_factory=lambda: _env("HUBSPOT_ACCESS_TOKEN"), repr=False
    )

    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL"))
    openai_timeout_seconds: float = field(
        default_factory=lambda: _float("OPENAI_TIMEOUT_SECONDS", 45) or 45
    )
    openai_max_retries: int = field(
        default_factory=lambda: int(_float("OPENAI_MAX_RETRIES", 2) or 0)
    )
    openai_max_output_tokens: int = field(
        default_factory=lambda: int(_float("OPENAI_MAX_OUTPUT_TOKENS", 2000) or 2000)
    )
    # Retry allowance when a reasoning model exhausts the normal output budget.
    openai_retry_max_output_tokens: int = 6000
    openai_input_usd_per_million: float | None = field(
        default_factory=lambda: _float("OPENAI_INPUT_USD_PER_MILLION", None)
    )
    openai_cached_input_usd_per_million: float | None = field(
        default_factory=lambda: _float("OPENAI_CACHED_INPUT_USD_PER_MILLION", None)
    )
    openai_output_usd_per_million: float | None = field(
        default_factory=lambda: _float("OPENAI_OUTPUT_USD_PER_MILLION", None)
    )
    ai_run_budget_usd: float = field(
        default_factory=lambda: _float("AI_RUN_BUDGET_USD", 5.0) or 5.0
    )
    max_description_chars: int = 16000

    http_timeout_seconds: float = 30
    http_max_retries: int = 2
    max_retry_wait_seconds: float = 30

    def missing_credentials(self) -> list[str]:
        names = {
            "THEIRSTACK_API_KEY": self.theirstack_api_key,
            "OPENAI_API_KEY": self.openai_api_key,
            "APOLLO_API_KEY": self.apollo_api_key,
            "HUBSPOT_ACCESS_TOKEN": self.hubspot_access_token,
            "OPENAI_MODEL": self.openai_model,
        }
        return [name for name, value in names.items() if not value]

    def secrets(self) -> list[str]:
        return [
            s
            for s in (
                self.theirstack_api_key,
                self.openai_api_key,
                self.apollo_api_key,
                self.hubspot_access_token,
            )
            if s
        ]


@lru_cache
def settings() -> Settings:
    return Settings()


def load_yaml(name: str) -> dict:
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))


@lru_cache
def catalogue() -> dict:
    return load_yaml("capabilities.yaml")


@lru_cache
def scoring_rules() -> dict:
    return load_yaml("scoring.yaml")


@lru_cache
def search_config() -> dict:
    return load_yaml("live_search.yaml")


@lru_cache
def apollo_config() -> dict:
    return load_yaml("apollo.yaml")


@lru_cache
def analysis_prompt() -> str:
    return (ROOT / "prompts/hiring_analysis.txt").read_text(encoding="utf-8")


PROMPT_VERSION = "hiring-analysis-v3-quality-boundaries"
