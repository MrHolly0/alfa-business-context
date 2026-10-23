"""Validated provider profiles loaded from operator-owned JSON."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


HACKATHON_BASES = frozenset({
    "https://changellenge.up.railway.app/v1",
    "https://api.alfahack-gateway26.ru/v1",
})
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
CANONICAL_MODELS = frozenset({
    "google/gemma-4-26b-a4b-it",
    "z-ai/glm-5.3-flash",
    "qwen/qwen3.6-35b-a3b",
    "deepseek/deepseek-v4.1-flash",
    "qwen/qwen3-vl-30b-a3b-instruct",
    "moonshotai/kimi-k2.6",
})


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    profile: str
    base_url: str
    api_key_env: str
    model: str
    budget_scope: str
    currency: str
    daily_limit: Decimal
    input_per_million: Decimal
    output_per_million: Decimal
    timeout_seconds: float
    max_output_tokens: int

    def __post_init__(self) -> None:
        _validate_base(self.profile, self.base_url)
        if not isinstance(self.model, str):
            raise ValueError("model is required")
        identity = self.model.removesuffix(":free")
        if self.model != identity and (self.profile != "openrouter" or identity != "google/gemma-4-26b-a4b-it"):
            raise ValueError("only OpenRouter Gemma may use the :free suffix")
        if identity not in CANONICAL_MODELS:
            raise ValueError("model is not in the six-model allowlist")
        if not isinstance(self.api_key_env, str) or not self.api_key_env.isidentifier():
            raise ValueError("api_key_env must be an environment variable name")
        if not isinstance(self.budget_scope, str) or not self.budget_scope.strip():
            raise ValueError("budget_scope is required")
        expected_currency = "USD" if self.profile == "openrouter" else "RUB"
        if self.currency != expected_currency:
            raise ValueError(f"{self.profile} currency must be {expected_currency}")
        limit = _decimal(self.daily_limit, "daily_limit")
        object.__setattr__(self, "daily_limit", limit)
        if self.profile == "hackathon" and limit > Decimal("500"):
            raise ValueError("hackathon daily_limit cannot exceed 500 RUB")
        object.__setattr__(self, "input_per_million", _decimal(self.input_per_million, "input_per_million"))
        object.__setattr__(self, "output_per_million", _decimal(self.output_per_million, "output_per_million"))
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        if isinstance(self.max_output_tokens, bool) or not isinstance(self.max_output_tokens, int) or self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be an explicit finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be an explicit finite decimal") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _validate_base(profile: str, base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("base_url must be an approved HTTPS API base")
    if parsed.query or parsed.fragment or parsed.path.endswith("/"):
        raise ValueError("base_url must be an exact approved API base")
    if profile == "openrouter":
        if base_url != OPENROUTER_BASE:
            raise ValueError("OpenRouter profile must use its approved API base")
    elif profile == "hackathon":
        if base_url not in HACKATHON_BASES:
            raise ValueError("Hackathon profile must use an approved gateway base")
    else:
        raise ValueError("profile must be 'openrouter' or 'hackathon'")


def _from_mapping(profile: str, value: dict[str, Any]) -> ProviderConfig:
    if not isinstance(value, dict):
        raise ValueError("provider profile must be an object")
    base_url = value.get("base_url")
    if not isinstance(base_url, str):
        raise ValueError("base_url is required")
    _validate_base(profile, base_url)
    raw_model = value.get("model")
    if not isinstance(raw_model, str):
        raise ValueError("model is required")
    model = raw_model
    identity = model
    if model.endswith(":free"):
        if profile != "openrouter" or model.removesuffix(":free") != "google/gemma-4-26b-a4b-it":
            raise ValueError("only OpenRouter Gemma may use the :free suffix")
        identity = model.removesuffix(":free")
    if identity not in CANONICAL_MODELS:
        raise ValueError("model is not in the six-model allowlist")
    api_key_env = value.get("api_key_env")
    if not isinstance(api_key_env, str) or not api_key_env.isidentifier():
        raise ValueError("api_key_env must be an environment variable name")
    scope = value.get("budget_scope")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("budget_scope is required")
    currency = value.get("currency")
    expected_currency = "USD" if profile == "openrouter" else "RUB"
    if currency != expected_currency:
        raise ValueError(f"{profile} currency must be {expected_currency}")
    limit = _decimal(value.get("daily_limit"), "daily_limit")
    if profile == "hackathon" and limit > Decimal("500"):
        raise ValueError("hackathon daily_limit cannot exceed 500 RUB")
    input_rate = _decimal(value.get("input_per_million"), "input_per_million")
    output_rate = _decimal(value.get("output_per_million"), "output_per_million")
    timeout = value.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    max_tokens = value.get("max_output_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("max_output_tokens must be a positive integer")
    return ProviderConfig(profile, base_url, api_key_env, model, scope.strip(), currency, limit,
                          input_rate, output_rate, float(timeout), max_tokens)


def load_config(path: str | Path, profile: str | None = None) -> ProviderConfig:
    """Read JSON config; profile secrets are never read here."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("could not read valid provider config JSON") from None
    if not isinstance(data, dict):
        raise ValueError("provider config must be a JSON object")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("provider config requires a profiles object")
    selected = profile or data.get("default_profile")
    if not isinstance(selected, str) or selected not in profiles:
        raise ValueError("selected provider profile is not configured")
    return _from_mapping(selected, profiles[selected])
