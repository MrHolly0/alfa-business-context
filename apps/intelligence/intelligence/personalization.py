"""Typed automatic follow-up intents and trusted Core-side budget policy."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator
from decimal import Decimal

from .schemas import StrictModel, CompanyContext


class FollowUpIntent(StrictModel):
    """A request for Core to consider one bounded company-config optimization."""

    schema_version: Literal["ai-follow-up-intent-v1"] = "ai-follow-up-intent-v1"
    kind: Literal["optimize_company_analyst"] = "optimize_company_analyst"
    parent_request_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=256)
    company_id: str = Field(min_length=1, max_length=256)
    context_version: int = Field(ge=1)
    profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_config_revision: int = Field(ge=1)
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["onboard", "context_changed"]

    @model_validator(mode="after")
    def idempotency_matches_inputs(self):
        expected = self.compute_idempotency_key(self.tenant_id, self.company_id,
            self.context_version, self.profile_fingerprint)
        if self.idempotency_key != expected:
            raise ValueError("idempotency_key does not match company context identity")
        return self

    @staticmethod
    def compute_idempotency_key(tenant_id: str, company_id: str,
                                context_version: int, profile_fingerprint: str) -> str:
        identity = json.dumps([tenant_id, company_id, context_version, profile_fingerprint],
                              ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @classmethod
    def create(cls, *, parent_request_id: str, tenant_id: str, company_id: str,
               context_version: int, profile_fingerprint: str,
               expected_config_revision: int, reason: str) -> "FollowUpIntent":
        return cls(parent_request_id=parent_request_id, tenant_id=tenant_id,
                   company_id=company_id, context_version=context_version,
                   profile_fingerprint=profile_fingerprint,
                   expected_config_revision=expected_config_revision,
                   idempotency_key=cls.compute_idempotency_key(tenant_id, company_id,
                       context_version, profile_fingerprint),
                   reason=reason)

    @classmethod
    def from_configuration(cls, *, parent_request_id: str, tenant_id: str,
                           company_id: str, context_version: int,
                           company: CompanyContext, configuration: dict,
                           reason: str) -> "FollowUpIntent | None":
        """Build an intent only from a successful baseline/configuration record."""
        if configuration.get("status") in {"stale_context", "revision_conflict", "optimization_failed"}:
            return None
        revision = configuration.get("revision")
        fingerprint = configuration.get("context_hash")
        if type(revision) is not int or revision < 1:
            return None
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            return None
        from .analysts import CompanyAnalystStore
        from .schemas import CompanyContext
        company = company if isinstance(company, CompanyContext) else CompanyContext.model_validate(company)
        if fingerprint != CompanyAnalystStore._context_digest(company):
            return None
        return cls.create(parent_request_id=parent_request_id, tenant_id=tenant_id,
                          company_id=company_id, context_version=context_version,
                          profile_fingerprint=fingerprint,
                          expected_config_revision=revision, reason=reason)


class AutoOptimizationPolicy(StrictModel):
    """Explicit finite Core allocation; absent policy means no auto jobs."""

    allocation_id: str = Field(min_length=1, max_length=128)
    max_jobs: int = Field(ge=1, le=1000)
    max_model_calls: int = Field(ge=1, le=3)
    max_input_tokens: int = Field(ge=1, le=24_000)
    max_output_tokens: int = Field(ge=1, le=2_048)
    max_total_tokens: int = Field(ge=1, le=60_000)
    max_steps: int = Field(ge=1, le=12)
    cost_reservation_rub: float = Field(ge=0, allow_inf_nan=False)
    cost_reservation_usd: float = Field(ge=0, allow_inf_nan=False)
    cooldown_seconds: int = Field(default=3600, ge=0, le=604800)
    max_jobs_per_company_per_day: int = Field(default=2, ge=1, le=100)

    @model_validator(mode="after")
    def explicit_nonzero_allocation(self):
        if self.cost_reservation_rub == 0 and self.cost_reservation_usd == 0:
            raise ValueError("automatic optimization requires a positive explicit cost reservation")
        return self

    @property
    def total_reservation_cap(self) -> dict[str, Decimal]:
        """Maximum lifetime reservation across this finite job allocation."""
        return {"RUB": Decimal(str(self.cost_reservation_rub)) * self.max_jobs,
                "USD": Decimal(str(self.cost_reservation_usd)) * self.max_jobs}

    def budget(self) -> dict:
        """Return a fresh explicit per-job envelope; never copy a parent budget."""
        return {
            "max_model_calls": self.max_model_calls,
            "max_tokens": self.max_total_tokens,
            "cost_reservation_rub": self.cost_reservation_rub,
            "cost_reservation_usd": self.cost_reservation_usd,
        }

    def task_budget(self) -> dict:
        return {
            "max_model_calls": self.max_model_calls,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_total_tokens": self.max_total_tokens,
            "max_steps": self.max_steps,
        }
