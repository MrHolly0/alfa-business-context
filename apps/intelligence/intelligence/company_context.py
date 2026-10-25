"""Safe, time-bounded projection of a company profile for analysis and retrieval."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from .schemas import (
    CompanyActivity, CompanyAssertion, CompanyContext, CompanyFact,
    ProvenancedDependency, StrictModel, _aware,
)


class ContextQuestion(StrictModel):
    item_id: str
    kind: str
    question: str
    provenance: str | None = None


class CompanyContextProjection(StrictModel):
    schema_version: str = "company-context-projection-v1"
    as_of: datetime
    activities: list[CompanyActivity] = Field(default_factory=list)
    assertions: list[CompanyAssertion] = Field(default_factory=list)
    dependencies: list[ProvenancedDependency] = Field(default_factory=list)
    facts: list[CompanyFact] = Field(default_factory=list)
    unresolved: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[ContextQuestion] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    fingerprint: str

    _aware_as_of = field_validator("as_of")(_aware)


def _in_effect(item: Any, as_of: datetime) -> bool:
    start, end = getattr(item, "valid_from", None), getattr(item, "valid_until", None)
    observed = getattr(item, "observed_at", None)
    return (observed is None or observed <= as_of) and (start is None or start <= as_of) and (end is None or as_of <= end)


def _not_yet_known(item: Any, as_of: datetime) -> bool:
    """Whether the profile item itself was unavailable at this historical time."""
    observed = getattr(item, "observed_at", None)
    start = getattr(item, "valid_from", None)
    return (observed is not None and observed > as_of) or (start is not None and start > as_of)


def _freshness_unknown(item: Any) -> bool:
    return getattr(item, "valid_from", None) is None and getattr(item, "valid_until", None) is None


def _json(item: Any) -> dict[str, Any]:
    return item.model_dump(mode="json")


def project_company_context(context: CompanyContext | dict, *, as_of: datetime) -> CompanyContextProjection:
    """Return confirmed, in-effect profile items and explicit gaps for a point in time.

    Legacy facts/dependencies without validity dates remain visible as confirmed
    for compatibility, but are marked freshness_unknown in the projection and
    paired with a question. Unknown, assumed, contradictory, expired, and future
    items never enter the confirmed lists.
    """
    as_of = _aware(as_of)
    company = context if isinstance(context, CompanyContext) else CompanyContext.model_validate(context)
    confirmed: dict[str, list[Any]] = {name: [] for name in ("activities", "assertions", "dependencies", "facts")}
    unresolved: list[dict[str, Any]] = []
    questions: list[ContextQuestion] = []
    limitations: list[str] = []

    groups = (
        ("activities", company.activities, "Действует ли направление деятельности «{label}»?"),
        ("assertions", company.assertions, "Подтверждается ли сейчас утверждение «{label}»?"),
        ("dependencies", company.dependencies, "Подтверждена ли сейчас зависимость «{label}»?"),
        ("facts", company.facts, "Актуально ли сейчас сведение «{label}»?"),
    )
    for group, items, question_template in groups:
        for item in items:
            if _not_yet_known(item, as_of):
                # Do not expose the future item's ID, text, provenance or refs
                # through either unresolved rows or generated questions.
                limitations.append("profile_item_not_known_at_as_of")
                continue
            status = getattr(item, "status", "confirmed")
            current = _in_effect(item, as_of)
            stale_unknown = _freshness_unknown(item)
            item_id = getattr(item, "activity_id", getattr(item, "assertion_id", getattr(item, "dependency_id", getattr(item, "fact_id", ""))))
            label = getattr(item, "label", getattr(item, "text", ""))
            if status == "confirmed" and current:
                confirmed[group].append(item)
                if stale_unknown:
                    limitations.append(f"freshness_unknown:{group}:{item_id}")
                    questions.append(ContextQuestion(item_id=item_id, kind=f"freshness:{group}", question=question_template.format(label=label), provenance=getattr(item, "provenance", None)))
            else:
                record = {"group": group, **_json(item), "freshness_unknown": stale_unknown, "in_effect": current}
                unresolved.append(record)
                why = "неизвестен статус/актуальность" if current else "сведение вне указанного периода действия"
                questions.append(ContextQuestion(item_id=item_id, kind=f"resolve:{group}", question=f"Уточните, {why}: «{label}».", provenance=getattr(item, "provenance", None)))
                limitations.append(f"unresolved:{group}:{item_id}")

    if not confirmed["activities"]:
        qualifier = f"Помимо отраслевого признака «{company.industry}», " if company.industry else ""
        questions.append(ContextQuestion(item_id="activities-missing", kind="missing:activities", question=f"{qualifier}какие направления деятельности сейчас ведёт компания? Укажите производство, торговлю, разработку и логистику, если применимо."))
        limitations.append("activities_unknown")
    if not confirmed["dependencies"]:
        questions.append(ContextQuestion(item_id="dependencies-missing", kind="missing:dependencies", question="Какие критичные ресурсы, поставщики, рынки или логистические маршруты могут изменить оценку воздействия? Отсутствие этих сведений не означает отсутствия зависимости."))
        limitations.append("dependencies_not_confirmed")

    payload = {key: [_json(x) for x in values] for key, values in confirmed.items()}
    payload["unresolved"] = unresolved
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return CompanyContextProjection(as_of=as_of, **confirmed, unresolved=unresolved,
                                    questions=questions, limitations=list(dict.fromkeys(limitations)), fingerprint=digest)


def iter_active_confirmed_context(context: CompanyContext | dict, as_of: datetime):
    """Yield projected confirmed activities, assertions, dependencies, and facts."""
    projection = project_company_context(context, as_of=as_of)
    for group in (projection.activities, projection.assertions, projection.dependencies, projection.facts):
        yield from group
