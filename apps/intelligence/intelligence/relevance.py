"""Explainable lexical preselection for events; scores are not probabilities or impact claims."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schemas import CompanyContext, EventInput
from .company_context import project_company_context


class RankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    tenant_id: str = Field(min_length=1, max_length=128)
    company: CompanyContext
    events: list[EventInput] = Field(max_length=500)
    source_ids: list[str] = Field(max_length=100)
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        return value

    @model_validator(mode="after")
    def sources_unique(self) -> "RankRequest":
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source_ids must be unique")
        context_ids = [dep.dependency_id for dep in self.company.dependencies]
        context_ids.extend(fact.fact_id for fact in self.company.facts)
        context_ids.extend(item.activity_id for item in self.company.activities)
        context_ids.extend(item.assertion_id for item in self.company.assertions)
        if len(set(context_ids)) != len(context_ids):
            raise ValueError("company context identifiers must be unique")
        return self


class MatchReason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["confirmed_dependency", "company_fact", "company_activity", "company_assertion", "company_name", "industry", "role_tiebreak"]
    context_ref: str | None = None
    text: str = Field(min_length=1, max_length=500)


class RankedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1)
    score: int = Field(ge=0)
    relevance: Literal["high", "medium", "low"]
    reasons: list[MatchReason] = Field(max_length=100)


class ExcludedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1)
    reason: Literal["source_not_allowed", "published_after_as_of"]


class RankResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["event-ranking-v1"] = "event-ranking-v1"
    tenant_id: str = Field(min_length=1)
    ranked: list[RankedEvent]
    excluded: list[ExcludedEvent]
    unsupported_context: list[str]
    method: Literal["lexical-v1"] = "lexical-v1"
    limitations: list[str]


_STOP = set("a an and are as at be by for from in is it of on or that the this to was were with и в во не на я с со как а то все она он ты к у мы вы за ее их о но да мы вы из от до по для при его ее об что это как если чтобы либо где когда тогда уже еще такие такой".split())


def _tokens(value: str) -> set[str]:
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return {token for token in re.findall(r"[\w]+", text, flags=re.UNICODE) if len(token) >= 3 and token not in _STOP}


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _content_fingerprint(event: EventInput) -> str:
    raw = "\0".join((event.title, event.text, event.source_id, _stamp(event.published_at), str(event.synthetic)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _role_group(role: str) -> str | None:
    words = _tokens(role)
    finance = {"cfo", "finance", "financial", "финансовый", "финансовая", "финансы", "финансист", "экономист"}
    procurement = {"procurement", "purchasing", "buyer", "закупки", "закупщик", "снабжение", "закупок"}
    executive = {"ceo", "chief", "executive", "operations", "операционный", "операционный", "генеральный", "гендиректор", "руководитель"}
    if words & finance: return "finance"
    if words & procurement: return "procurement"
    if words & executive: return "executive"
    return None


_ROLE_WORDS = {
    "finance": {"cost", "costs", "currency", "exchange", "fx", "forex", "inflation", "rate", "rates", "tax", "budget", "финанс", "валют", "курс", "курсы", "инфляц", "ставк", "налог", "затрат", "расход"},
    "procurement": {"supply", "supplies", "supplier", "suppliers", "procurement", "shipping", "logistics", "raw", "material", "materials", "закупк", "постав", "снабжен", "сырье", "логист", "перевоз", "тариф"},
    "executive": {"operations", "operational", "production", "export", "investment", "regulation", "market", "strategy", "операц", "производ", "экспорт", "инвестиц", "регуляц", "рынок", "стратег"},
}


def _lexical_match(context_text: str, event_tokens: set[str]) -> bool:
    return bool(_tokens(context_text) & event_tokens)


def _matches(tokens: set[str], vocab: set[str]) -> bool:
    """Use documented token prefixes for small Russian role vocabularies only."""
    return any(token in vocab or any(token.startswith(term) or term.startswith(token) for term in vocab if len(term) >= 5) for token in tokens)


def rank_events(*, tenant_id: str, company: CompanyContext | dict, events: list[EventInput | dict], source_ids: list[str], as_of: str) -> dict:
    """Rank up to 500 allowed event revisions with fixed lexical weights and reasons."""
    request = RankRequest.model_validate({"tenant_id": tenant_id, "company": company, "events": events, "source_ids": source_ids, "as_of": as_of})
    as_of_utc = request.as_of.astimezone(timezone.utc)
    allowed_sources = set(request.source_ids)
    excluded: list[ExcludedEvent] = []
    eligible: list[EventInput] = []
    for event in request.events:
        if event.source_id not in allowed_sources:
            excluded.append(ExcludedEvent(event_id=event.event_id, reason="source_not_allowed"))
        elif event.published_at.astimezone(timezone.utc) > as_of_utc:
            excluded.append(ExcludedEvent(event_id=event.event_id, reason="published_after_as_of"))
        else:
            eligible.append(event)

    # Identical event ID/revision must have identical content; fail rather than choose arbitrarily.
    seen_revisions: dict[tuple[str, int], str] = {}
    for event in eligible:
        key = (event.event_id, event.revision)
        fingerprint = _content_fingerprint(event)
        if key in seen_revisions and seen_revisions[key] != fingerprint:
            raise ValueError(f"conflicting event content for {event.event_id} revision {event.revision}")
        seen_revisions[key] = fingerprint
    latest: dict[str, EventInput] = {}
    for event in eligible:
        prior = latest.get(event.event_id)
        if prior is None or event.revision > prior.revision:
            latest[event.event_id] = event
    company_context = request.company
    projection = project_company_context(company_context, as_of=request.as_of)
    dependencies = [d for d in projection.dependencies if not _is_noncausal_kind(d.kind)]
    facts = projection.facts
    activities = projection.activities
    assertions = projection.assertions
    unsupported = [str(item.get("dependency_id") or item.get("assertion_id") or item.get("activity_id") or item.get("fact_id"))
                   for item in projection.unresolved]
    unsupported.extend(d.dependency_id for d in company_context.dependencies if d.status == "confirmed" and _is_noncausal_kind(d.kind))
    unsupported = list(dict.fromkeys(unsupported))
    company_tokens = _tokens(company_context.name)
    industry_tokens = _tokens(company_context.industry)
    role_group = _role_group(company_context.role)
    ranked: list[tuple[int, bool, datetime, str, RankedEvent]] = []
    for event in latest.values():
        event_tokens = _tokens(event.title + " " + event.text)
        score = 0
        reasons: list[MatchReason] = []
        for dep in dependencies:
            if _lexical_match(dep.label, event_tokens):
                score += 5
                reasons.append(MatchReason(kind="confirmed_dependency", context_ref=dep.dependency_id, text=f"Совпали слова события и подтверждённой зависимости «{dep.label[:240]}»."))
        for fact in facts:
            if _lexical_match(fact.text, event_tokens):
                score += 2
                reasons.append(MatchReason(kind="company_fact", context_ref=fact.fact_id, text=f"Совпали слова события и факта компании «{fact.text[:240]}»."))
        for activity in activities:
            if _lexical_match(activity.label, event_tokens):
                score += 3
                reasons.append(MatchReason(kind="company_activity", context_ref=activity.activity_id, text=f"Совпали слова события и подтверждённого направления «{activity.label[:240]}»."))
        for assertion in assertions:
            if _lexical_match(assertion.label, event_tokens):
                score += 3
                reasons.append(MatchReason(kind="company_assertion", context_ref=assertion.assertion_id, text=f"Совпали слова события и подтверждённого сведения «{assertion.label[:240]}»."))
        if company_tokens & event_tokens:
            score += 3
            reasons.append(MatchReason(kind="company_name", context_ref=None, text="В событии встретились слова из названия компании."))
        if industry_tokens & event_tokens:
            score += 1
            reasons.append(MatchReason(kind="industry", context_ref=None, text="Есть лексическое совпадение только с отраслью; само по себе оно даёт слабый сигнал."))
        role_match = bool(role_group and _matches(event_tokens, _ROLE_WORDS[role_group]))
        if role_match:
            reasons.append(MatchReason(kind="role_tiebreak", context_ref=None, text=f"Лексическая подсказка роли {role_group}; она используется только для разрешения равного score."))
        relevance = "high" if score >= 5 else "medium" if score >= 2 else "low"
        ranked_item = RankedEvent(event_id=event.event_id, score=score, relevance=relevance, reasons=reasons)
        ranked.append((score, role_match, event.published_at.astimezone(timezone.utc), event.event_id, ranked_item))
    ranked.sort(key=lambda item: (-item[0], -int(item[1]), -item[2].timestamp(), item[3]))
    result = RankResult(tenant_id=request.tenant_id, ranked=[item[4] for item in ranked], excluded=excluded, unsupported_context=unsupported,
                        limitations=["Лексические совпадения — только предварительная сортировка, не вероятность и не оценка воздействия.",
                                     "Сопоставляются точные токены после нормализации регистра и ё/е; русские словоформы автоматически не сводятся.",
                                     "Отрасль и роль не доказывают зависимость; роль влияет только при равном score.",
                                     "Гипотезы, платежи и групповые признаки не повышают score и не подтверждают причинную связь.",
                                     *projection.limitations])
    return result.model_dump(mode="json")


def _is_noncausal_kind(kind: str) -> bool:
    normalized = _tokens(kind)
    return bool(normalized & {"payment", "payments", "transaction", "transactions", "group", "group_id", "платеж", "платежи", "транзакция", "транзакции", "группа", "групповой"})
