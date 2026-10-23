"""Versioned, closed JSON contracts for bounded business analysis."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value


NonEmpty = Annotated[str, Field(min_length=1, max_length=8000)]


class ProvenancedDependency(StrictModel):
    dependency_id: NonEmpty
    label: NonEmpty
    kind: NonEmpty
    status: Literal["confirmed", "hypothesis"]
    provenance: NonEmpty


class CompanyFact(StrictModel):
    fact_id: NonEmpty
    text: NonEmpty
    provenance: NonEmpty


class CompanyContext(StrictModel):
    name: NonEmpty
    industry: Annotated[str, Field(max_length=1000)] = ""
    description: Annotated[str, Field(max_length=4000)] = ""
    role: Annotated[str, Field(max_length=1000)] = ""
    dependencies: list[ProvenancedDependency] = Field(default_factory=list, max_length=100)
    facts: list[CompanyFact] = Field(default_factory=list, max_length=100)


class EventInput(StrictModel):
    event_id: NonEmpty
    revision: int = Field(default=1, ge=1)
    title: NonEmpty
    text: Annotated[str, Field(max_length=100_000)]
    source_id: NonEmpty
    published_at: datetime
    synthetic: bool = False

    _published_aware = field_validator("published_at")(_aware)


class AnalysisBudget(StrictModel):
    max_model_calls: int = Field(default=2, ge=0, le=3)
    max_input_tokens: int = Field(default=24_000, ge=1, le=24_000)
    max_output_tokens: int = Field(default=2_048, ge=1, le=2_048)
    max_total_tokens: int = Field(default=60_000, ge=1, le=60_000)
    max_steps: int = Field(default=12, ge=1, le=12)


class AnalysisRequest(StrictModel):
    schema_version: Literal["analysis-request-v1"] = "analysis-request-v1"
    request_id: NonEmpty
    tenant_id: NonEmpty
    company_id: NonEmpty
    as_of: datetime
    context_version: int = Field(ge=1)
    source_policy_version: int = Field(ge=1)
    source_ids: list[NonEmpty] = Field(min_length=1, max_length=100)
    document_revision_refs: list[NonEmpty] = Field(default_factory=list, max_length=500)
    company: CompanyContext
    event: EventInput
    budget: AnalysisBudget = Field(default_factory=AnalysisBudget)
    synthetic: bool = False

    _as_of_aware = field_validator("as_of")(_aware)

    @model_validator(mode="after")
    def event_source_is_allowed(self) -> "AnalysisRequest":
        if self.event.source_id not in self.source_ids:
            raise ValueError("event source_id must be included in source_ids")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source_ids must be unique")
        if len(set(self.document_revision_refs)) != len(self.document_revision_refs):
            raise ValueError("document_revision_refs must be unique")
        if self.event.published_at > self.as_of:
            raise ValueError("event cannot be published after as_of")
        identifiers = [d.dependency_id for d in self.company.dependencies]
        identifiers += [f.fact_id for f in self.company.facts]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("company context identifiers must be unique")
        return self


ClaimKind = Literal["external_fact", "bank_view", "model_hypothesis", "recommendation", "unknown"]


class CandidateClaim(StrictModel):
    claim_id: NonEmpty
    kind: ClaimKind
    text: NonEmpty
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    context_refs: list[str] = Field(default_factory=list, max_length=50)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    alternatives: list[str] = Field(default_factory=list, max_length=30)
    falsifiers: list[str] = Field(default_factory=list, max_length=30)
    origin: str | None = None


class EvidenceRecord(StrictModel):
    evidence_id: NonEmpty
    document_id: NonEmpty
    document_revision_ref: NonEmpty
    source_id: NonEmpty
    source_role: Literal["external", "bank"]
    title: str
    canonical_uri: str
    published_at: datetime
    fetched_at: datetime
    locator: NonEmpty
    quote: Annotated[str, Field(min_length=1, max_length=1000)]
    content_hash: NonEmpty
    synthetic: bool
    score: float = Field(ge=0)

    _evidence_dates_aware = field_validator("published_at", "fetched_at")(_aware)


class InfluenceEdge(StrictModel):
    edge_id: NonEmpty
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    context_refs: list[str] = Field(default_factory=list, max_length=20)
    provenance: NonEmpty
    status: Literal["confirmed", "hypothesis"]
    alternatives: list[str] = Field(default_factory=list, max_length=20)
    falsifiers: list[str] = Field(default_factory=list, max_length=20)


class ModelAnswer(StrictModel):
    """Only the fields the model is allowed to author."""
    summary: Annotated[str, Field(max_length=5000)]
    claims: list[CandidateClaim] = Field(max_length=100)
    influence_chain: list[InfluenceEdge] = Field(max_length=100)
    clarifications: list[Annotated[str, Field(max_length=1000)]] = Field(max_length=30)


class Coverage(StrictModel):
    external: int = Field(ge=0)
    bank: int = Field(ge=0)
    confirmed_dependencies: int = Field(ge=0)
    company_facts: int = Field(ge=0)


class Versions(StrictModel):
    prompt: NonEmpty
    analyst_config: NonEmpty = "baseline@1"
    pipeline: NonEmpty
    model: str | None = None
    context: int = Field(ge=1)
    source_policy: int = Field(ge=1)
    as_of: datetime
    event_id: NonEmpty
    event_revision: int = Field(ge=1)
    event_document_revision_ref: NonEmpty
    synthetic: bool

    _as_of_aware = field_validator("as_of")(_aware)


class CandidateAssessment(StrictModel):
    schema_version: Literal["candidate-assessment-v1"] = "candidate-assessment-v1"
    request_id: str
    tenant_id: str
    company_id: str
    status: Literal["ready", "partial", "unknown"]
    execution_mode: Literal["model", "baseline"]
    summary: Annotated[str, Field(max_length=5000)]
    claims: list[CandidateClaim] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    influence_chain: list[InfluenceEdge] = Field(default_factory=list)
    clarifications: list[Annotated[str, Field(max_length=1000)]] = Field(default_factory=list, max_length=30)
    coverage: Coverage
    versions: Versions
    usage: dict
    trace: list[dict]
    created_at: datetime

    _created_at_aware = field_validator("created_at")(_aware)
