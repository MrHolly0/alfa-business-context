"""Explicit operator-controlled model routes sharing one provider budget account."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import ProviderConfig, _from_mapping
from .service import LLMService

ROLES = frozenset({"researcher", "analyst", "critic", "extractor", "generator", "judge"})
ROUTE_FIELDS = frozenset({"model", "input_per_million", "output_per_million", "max_output_tokens", "reasoning_effort"})


def _route_key(value: str) -> tuple[str, str | None]:
    if not isinstance(value, str):
        raise ValueError("route key must be a supported role or role:complex")
    if value in ROLES:
        return value, None
    if isinstance(value, str) and ":" in value:
        role, variant = value.split(":", 1)
        if role in ROLES and variant == "complex":
            return role, variant
    raise ValueError("route key must be a supported role or role:complex")


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite non-negative decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite non-negative decimal") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a finite non-negative decimal")
    return result


class ModelRouter:
    """Build role-specific LLMService objects without changing account or budget identity.

    ``routes`` may contain a default route for a role and an explicit ``role:complex``
    route. Complexity selection is caller-directed; request failures never trigger a
    different route or an implicit retry.
    """

    def __init__(self, base_config: ProviderConfig | dict[str, Any], ledger_path: str | Path,
                 routes: dict[str, dict[str, Any]] | None = None):
        if isinstance(base_config, ProviderConfig):
            self.base_config = base_config
        elif isinstance(base_config, dict):
            profile = base_config.get("profile")
            allowed = {"profile", "base_url", "api_key_env", "model", "budget_scope", "currency",
                       "daily_limit", "input_per_million", "output_per_million", "timeout_seconds",
                       "max_output_tokens", "allow_paid", "max_request_cost", "lifetime_limit",
                       "pricing_mode", "attempt_reserve_rub", "max_calls_total", "reasoning_effort"}
            if set(base_config) - allowed or not isinstance(profile, str):
                raise ValueError("base_config must be a validated provider profile mapping")
            self.base_config = _from_mapping(profile, {k:v for k,v in base_config.items() if k != "profile"})
        else:
            raise ValueError("base_config must be ProviderConfig or its validated mapping")
        if not isinstance(ledger_path, (str, Path)) or not str(ledger_path).strip():
            raise ValueError("ledger_path is required")
        self.ledger_path = str(ledger_path)
        if routes is None:
            routes = {}
        if not isinstance(routes, dict):
            raise ValueError("routes must be an object")
        self.routes: dict[tuple[str, str | None], dict[str, Any]] = {}
        for key, override in routes.items():
            route_key = _route_key(key)
            if route_key in self.routes:
                raise ValueError("duplicate route")
            if not isinstance(override, dict) or set(override) - ROUTE_FIELDS:
                raise ValueError("route may override only model, rates, max_output_tokens, and reasoning_effort")
            cleaned = dict(override)
            if "model" in cleaned:
                model = cleaned["model"]
                if not isinstance(model, str):
                    raise ValueError("route model must be a string")
                # Validate model allowlist and free suffix through ProviderConfig.
                replace(self.base_config, model=model)
            for field in ("input_per_million", "output_per_million"):
                if field in cleaned:
                    cleaned[field] = _decimal(cleaned[field], field)
            if "max_output_tokens" in cleaned:
                value = cleaned["max_output_tokens"]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError("max_output_tokens must be a positive integer")
            # Validate the complete route at configuration load time, not only
            # when a role's service is first requested.
            replace(self.base_config, **cleaned)
            self.routes[route_key] = cleaned
        self._services: dict[tuple[str, str | None], LLMService] = {}

    def _service(self, role: str, variant: str | None = None) -> LLMService:
        key = (role, variant)
        if key in self._services:
            return self._services[key]
        override = self.routes.get(key, {})
        config = replace(self.base_config, **override)
        service = LLMService(config, ledger_path=self.ledger_path)
        self._services[key] = service
        return service

    def services(self) -> dict[str, LLMService]:
        """Return the default deterministic service for each supported role."""
        return {role: self._service(role) for role in sorted(ROLES)}

    def route_for(self, role: str, *, complexity: str | None = None) -> LLMService:
        if not isinstance(role, str) or role not in ROLES:
            raise ValueError("unknown model route role")
        if complexity not in (None, "standard", "complex"):
            raise ValueError("complexity must be standard or complex")
        if complexity == "complex":
            if (role, "complex") not in self.routes:
                raise ValueError("no explicit complex route configured for role")
            return self._service(role, "complex")
        return self._service(role)
