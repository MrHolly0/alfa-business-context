"""Bounded extraction proposals from a user supplied business description.

This module proposes profile items for Core review. It does not own or persist a
company profile. Unconfirmed text remains explicitly unconfirmed; an optional
LLM can only return claims that are anchored to exact source spans.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from .schemas import (
    CompanyActivity, CompanyAssertion, CompanyContext, CompanyFact,
    ProvenancedDependency, StrictModel, _aware,
)

MAX_DESCRIPTION_CHARS = 4_000
MAX_SEARCH_DIRECTIONS = 3
MAX_ASSERTIONS = 100


class DescriptionPrepareRequest(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=256)
    company_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=8000)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    provenance: str = Field(min_length=1, max_length=1000)
    as_of: datetime
    data_classification: Literal["public", "synthetic", "confidential"]
    statement_status: Literal["unconfirmed", "core_confirmed"] = "unconfirmed"

    _aware = field_validator("as_of")(_aware)


class SourceSpan(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)


class ContextHypothesis(StrictModel):
    hypothesis_id: str
    text: str = Field(min_length=1, max_length=1000)
    basis: list[SourceSpan] = Field(min_length=1, max_length=4)
    confidence: float = Field(ge=0, le=1)
    status: Literal["assumption"] = "assumption"


class SignificanceSignal(StrictModel):
    item_id: str
    rule: Literal["confirmed_dependency_match", "activity_lead", "assumptions"]
    priority: Literal["high", "normal", "explore"]
    reason: str
    confidence: float = Field(ge=0, le=1)


class SearchDirection(StrictModel):
    direction: str
    rule: Literal["confirmed_dependency_match", "activity_lead", "assumptions"]
    priority: Literal["high", "normal", "explore"]
    basis_item_ids: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)


@dataclass(frozen=True, slots=True)
class DependencyCandidate:
    """One quoted dependency item plus how its relation was stated in source."""
    label: str
    span: SourceSpan
    source_kind: Literal["literal_relation", "dependency_field", "llm"]
    field_span: SourceSpan | None = None


class DescriptionProposal(StrictModel):
    schema_version: Literal["company-description-proposal-v1"] = "company-description-proposal-v1"
    tenant_id: str
    company_id: str
    context: CompanyContext
    hypotheses: list[ContextHypothesis] = Field(default_factory=list, max_length=100)
    questions: list[str] = Field(default_factory=list, max_length=100)
    significance: list[SignificanceSignal] = Field(default_factory=list, max_length=100)
    search_directions: list[SearchDirection] = Field(default_factory=list, max_length=MAX_SEARCH_DIRECTIONS)
    extraction_mode: Literal["deterministic", "llm", "deterministic_fallback"]
    limitations: list[str] = Field(default_factory=list, max_length=20)


_ACTIVITY_RE = re.compile(
    r"(?P<quote>(?:Компания\s+)?(?:(?:не|возможно|может|планирует|намерена|намеревается)\s+)?(?:занимается|производит|производить|изготавливает|изготавливать|разрабатывает|разрабатывать|импортирует|импортировать|экспортирует|экспортировать|продаёт|продавать|оказывает|оказывать|перевозит|перевозить|выращивает|выращивать|строит|строить)\s+[^.;!?\n]{2,500})",
    re.IGNORECASE,
)
_DEPENDENCY_RE = re.compile(
    r"(?P<quote>(?:Компания\s+)?(?:(?:не|возможно|может|планирует|намерена|намеревается)\s+)?(?:зависит|зависима)\s+от\s+[^.;!?\n]{2,500})", re.IGNORECASE
)
_FIELD_RE = re.compile(r"(?P<key>направления|деятельность|зависимости|зависит от|факты)\s*:\s*(?P<value>[^\n]+)", re.IGNORECASE)
_SPLIT_ITEMS_RE = re.compile(r"\s*(?:;|\||\s+и\s+)\s*")
_VERB_RE = re.compile(r"занимается|производит|производить|изготавливает|изготавливать|разрабатывает|разрабатывать|импортирует|импортировать|экспортирует|экспортировать|продаёт|продавать|оказывает|оказывать|перевозит|перевозить|выращивает|выращивать|строит|строить", re.IGNORECASE)
_ACTIVITY_VERBS = (
    ("производ", "manufacturing"), ("изготавлива", "manufacturing"),
    ("разрабатыва", "software"), ("импортир", "import"),
    ("экспортир", "export"), ("прода", "trade"), ("оказыва", "other"),
    ("перевоз", "logistics"), ("выращива", "agriculture"), ("стро", "construction"),
)


def _id(prefix: str, text: str, offset: int) -> str:
    digest = hashlib.sha256(f"{prefix}:{offset}:{text}".encode()).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _span(source: str, start: int, end: int) -> SourceSpan:
    return SourceSpan(start=start, end=end, quote=source[start:end])


def _classify_activity(value: str) -> str | None:
    folded = value.casefold()
    return next((kind for token, kind in _ACTIVITY_VERBS if token in folded), None)


_QUALIFIER_RE = re.compile(r"(?:не|возможно|может|планирует|намерен(?:а|о|ы)?|предполагает)\s*$", re.IGNORECASE)


def _qualified(source: str, start: int, quote: str = "") -> bool:
    """Detect negation/modality immediately before a model or regex quote span."""
    if re.match(r"(?:Компания\s+)?(?:не|возможно|может|планирует|намерен(?:а|о|ы)?|предполагает)\s+", quote, re.IGNORECASE):
        return True
    prefix = source[max(0, start - 100):start]
    # Keep the check within the current clause so an earlier sentence cannot taint this one.
    prefix = re.split(r"[.;!?\n]", prefix)[-1]
    return bool(_QUALIFIER_RE.search(prefix))


def _explicit_extractions(source: str) -> tuple[list[tuple[str, SourceSpan]], list[DependencyCandidate], list[tuple[str, SourceSpan]]]:
    activities: list[tuple[str, SourceSpan]] = []
    dependencies: list[DependencyCandidate] = []
    facts: list[tuple[str, SourceSpan]] = []
    for match in _ACTIVITY_RE.finditer(source):
        start, end = match.span("quote")
        phrase = source[start:end]
        verbs = list(_VERB_RE.finditer(phrase))
        for index, verb in enumerate(verbs):
            clause_start = start if index == 0 else start + verb.start()
            clause_end = start + (verbs[index + 1].start() if index + 1 < len(verbs) else len(phrase))
            clause_end = len(source[:clause_end].rstrip(" ,;"))
            if source[clause_start:clause_end].endswith(" и"):
                clause_end -= 2
            quote = source[clause_start:clause_end].strip()
            # If the matcher began after a qualifier (for example, "планирует"),
            # retain the complete quote while preserving the negation/modality.
            prefix = source[max(0, clause_start - 100):clause_start]
            qualifier = _QUALIFIER_RE.search(prefix)
            if qualifier:
                clause_start -= qualifier.end() - qualifier.start()
                quote = source[clause_start:clause_end].strip()
            activities.append((quote, _span(source, clause_start, clause_end)))
    for match in _DEPENDENCY_RE.finditer(source):
        start, end = match.span("quote")
        prefix = source[max(0, start - 100):start]
        qualifier = _QUALIFIER_RE.search(re.split(r"[.;!?\n]", prefix)[-1])
        if qualifier:
            start -= qualifier.end() - qualifier.start()
        quote = source[start:end].strip()
        dependencies.append(DependencyCandidate(quote, _span(source, start, end), "literal_relation"))
    for match in _FIELD_RE.finditer(source):
        key, value = match.group("key").casefold(), match.group("value").strip()
        start, end = match.span("value")
        if key in {"направления", "деятельность"}:
            cursor = start
            for item in _SPLIT_ITEMS_RE.split(value):
                pos = source.find(item, cursor, end)
                if pos >= 0:
                    activities.append((item, _span(source, pos, pos + len(item))))
                    cursor = pos + len(item)
        elif key in {"зависимости", "зависит от"}:
            label_start = match.start("key")
            prefix = source[max(0, label_start - 100):label_start]
            qualifier = _QUALIFIER_RE.search(re.split(r"[.;!?\n]", prefix)[-1])
            if qualifier:
                label_start -= qualifier.end() - qualifier.start()
            cursor = start
            field_span = _span(source, label_start, end)
            for raw_item in _SPLIT_ITEMS_RE.split(value):
                item = raw_item.rstrip(".!?").rstrip()
                pos = source.find(item, cursor, end)
                if pos >= 0:
                    # Each list item inherits the literal header, with separate
                    # exact offsets for the item and its relation-bearing field.
                    dependencies.append(DependencyCandidate(item, _span(source, pos, pos + len(item)),
                                                            "dependency_field", field_span))
                    cursor = pos + len(item)
        elif key == "факты":
            facts.append((value, _span(source, start, end)))
    # Preserve order and avoid duplicate extraction of a verb phrase repeated by explicit fields.
    def unique(rows):
        seen = set()
        result = []
        for row in rows:
            item_start = row.span.start if isinstance(row, DependencyCandidate) else row[1].start
            if item_start not in seen:
                seen.add(item_start)
                result.append(row)
        return result
    return unique(activities), unique(dependencies), unique(facts)


def _model_extractions(request: DescriptionPrepareRequest, llm: Any):
    if request.data_classification == "confidential":
        raise ValueError("LLM extraction is not permitted for confidential descriptions")
    prompt = {
        "task": "Extract only explicit company activities, facts, and literal dependencies from description.",
        "rules": ["Return JSON only", "Every claim must quote an exact substring and offsets into description",
                  "Never infer companies, amounts, shares, or facts", "Inferred relations must be marked assumption"],
        "description": request.description,
        "schema": {"activities": [{"text": "string", "start": "int", "end": "int", "quote": "string"}],
                   "dependencies": [{"text": "string", "start": "int", "end": "int", "quote": "string", "inferred": "boolean"}],
                   "facts": [{"text": "string", "start": "int", "end": "int", "quote": "string"}]},
    }
    messages = [{"role": "system", "content": "You are a strict extractor. Output JSON matching the requested structure."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]
    response = llm.complete(messages, max_input_tokens=6000, max_output_tokens=1200, json_mode=True)
    raw = response.get("content") if isinstance(response, dict) else getattr(response, "content", None)
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict) or set(data) != {"activities", "dependencies", "facts"}:
        raise ValueError("LLM extraction returned an invalid closed object")
    for group_name, group in data.items():
        if not isinstance(group, list) or len(group) > MAX_ASSERTIONS:
            raise ValueError("LLM extraction exceeded bounded list limits")
        for row in group:
            if not isinstance(row, dict):
                raise ValueError("LLM extraction row must be an object")
            expected_keys = {"text", "start", "end", "quote"} | ({"inferred"} if group_name == "dependencies" else set())
            if set(row) != expected_keys:
                raise ValueError("LLM extraction row has unexpected or missing fields")
            if group_name == "dependencies" and not isinstance(row["inferred"], bool):
                raise ValueError("LLM dependency inferred flag must be boolean")
            start, end, quote, text = row.get("start"), row.get("end"), row.get("quote"), row.get("text")
            if (isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int)
                    or not isinstance(quote, str) or not quote or not isinstance(text, str) or text != quote):
                raise ValueError("LLM claim is not anchored to an exact source span")
            if not (0 <= start < end <= len(request.description)) or request.description[start:end] != quote:
                # Models often count characters incorrectly. Resolve an exact,
                # unique quote locally; never guess among repeated occurrences.
                position = request.description.find(quote)
                if position < 0 or request.description.find(quote, position + 1) >= 0:
                    raise ValueError("LLM claim is not anchored to an exact source span")
                row["start"], row["end"] = position, position + len(quote)
    return data


def prepare_description(request: DescriptionPrepareRequest | dict, llm: Any = None) -> DescriptionProposal:
    """Build an unpersisted context proposal from explicit description claims."""
    req = request if isinstance(request, DescriptionPrepareRequest) else DescriptionPrepareRequest.model_validate(request)
    if llm is not None and req.data_classification == "confidential":
        raise ValueError("LLM extraction is not permitted for confidential descriptions")
    extraction_limitations = []
    if llm is None:
        activities, dependencies, facts = _explicit_extractions(req.description)
        mode = "deterministic"
    else:
        try:
            data = _model_extractions(req, llm)
            activities = [(r["text"], SourceSpan(start=r["start"], end=r["end"], quote=r["quote"])) for r in data["activities"]]
            dependencies = [DependencyCandidate(r["text"], SourceSpan(start=r["start"], end=r["end"], quote=r["quote"]), "llm")
                            for r in data["dependencies"]]
            facts = [(r["text"], SourceSpan(start=r["start"], end=r["end"], quote=r["quote"])) for r in data["facts"]]
            mode = "llm"
        except Exception as exc:
            extraction_limitations.append("llm_extraction_output_rejected" if isinstance(exc, (ValueError, TypeError, KeyError)) else "llm_extraction_failed")
            activities, dependencies, facts = _explicit_extractions(req.description)
            mode = "deterministic_fallback"
    if len(activities) + len(dependencies) + len(facts) > MAX_ASSERTIONS:
        activities, dependencies, facts = activities[:MAX_ASSERTIONS], dependencies[:MAX_ASSERTIONS], facts[:MAX_ASSERTIONS]
        remaining = MAX_ASSERTIONS - len(activities) - len(dependencies)
        facts = facts[:max(0, remaining)]
    confirmed = req.statement_status == "core_confirmed" and mode != "llm"
    confirmation = "Core-confirmed user statement, not independent verification" if confirmed else req.provenance
    status = "confirmed" if confirmed else "unknown"
    activity_items = [CompanyActivity(observed_at=req.as_of,activity_id=_id("activity", quote, span.start), label=quote,
        normalized_class=_classify_activity(quote), status=("unknown" if _qualified(req.description, span.start, quote) else status),
        provenance=(confirmation if status == "confirmed" and not _qualified(req.description, span.start, quote) else req.provenance),
        evidence_refs=[f"description:{span.start}:{span.end}"]) for quote, span in activities]
    fact_items = [CompanyFact(observed_at=req.as_of,fact_id=_id("fact", quote, span.start), text=quote,
        status=("unknown" if re.search(r"\b(?:возможно|вероятно|предположительно|планирует|планируется|предполагается|намерена)\b", quote, re.IGNORECASE) else status),
        provenance=confirmation, evidence_refs=[f"description:{span.start}:{span.end}"])
        for quote, span in facts]
    dependency_items = []
    assertion_items = []
    hypotheses: list[ContextHypothesis] = []
    for candidate in dependencies:
        quote, span = candidate.label, candidate.span
        is_literal_relation = candidate.source_kind in {"literal_relation", "dependency_field"} and (
            candidate.source_kind == "dependency_field" or
            bool(re.search(r"\b(?:зависит|зависима)\s+от\b", quote, re.IGNORECASE)))
        evidence_refs = [f"description:{span.start}:{span.end}"]
        if candidate.field_span:
            evidence_refs.append(f"description:{candidate.field_span.start}:{candidate.field_span.end}")
        qualified_field = bool(candidate.field_span and _qualified(
            req.description, candidate.field_span.start, candidate.field_span.quote))
        if (is_literal_relation and confirmed and mode != "llm"
                and not _qualified(req.description, span.start, quote) and not qualified_field):
            dependency_items.append(ProvenancedDependency(observed_at=req.as_of,dependency_id=_id("dependency", quote, span.start),
                label=quote, kind="dependency", status="confirmed", provenance=confirmation,
                evidence_refs=evidence_refs))
        else:
            item_id = _id("assertion", quote, span.start)
            assertion_items.append(CompanyAssertion(observed_at=req.as_of,assertion_id=item_id, kind="dependency", label=quote,
                status="assumption", provenance=req.provenance, evidence_refs=evidence_refs))
            hypotheses.append(ContextHypothesis(hypothesis_id=_id("hypothesis", quote, span.start), text=quote,
                basis=[span], confidence=0.45))
    context = CompanyContext(name=req.name, description=req.description, activities=activity_items,
        assertions=assertion_items, dependencies=dependency_items, facts=fact_items)
    significance: list[SignificanceSignal] = []
    for item in dependency_items:
        significance.append(SignificanceSignal(item_id=item.dependency_id,
            rule="confirmed_dependency_match", priority="high",
            reason=("select_affected_businesses: если _matching_terms находит совпадение слов этой подтверждённой зависимости "
                    "с явным термином события, добавляет текстовый путь с сигналом 0.80; применимость требует проверки."),
            confidence=0.8))
    for item in activity_items:
        significance.append(SignificanceSignal(item_id=item.activity_id, rule="activity_lead", priority="normal",
            reason=("select_affected_businesses сопоставляет слова направления с явными терминами события и даёт сигнал 0.55; "
                    "направление должно быть подтверждено, чтобы попасть в активную проекцию."), confidence=0.55))
    for item in assertion_items:
        significance.append(SignificanceSignal(item_id=item.assertion_id, rule="assumptions", priority="explore",
            reason="Это предположение; активная проекция select_affected_businesses его исключает, нужна проверка.", confidence=0.45))

    search: list[SearchDirection] = []
    for item in dependency_items:
        search.append(SearchDirection(direction=f"Сопоставить сообщения о зависимостях с явными терминами событий: «{item.label}».",
            rule="confirmed_dependency_match", priority="high", basis_item_ids=[item.dependency_id], confidence=0.8))
    for item in activity_items:
        search.append(SearchDirection(direction=f"Проверить сообщения по направлению «{item.label}»; сигнал появляется при совпадении слов.",
            rule="activity_lead", priority="normal", basis_item_ids=[item.activity_id], confidence=0.55))
    for item in assertion_items:
        search.append(SearchDirection(direction=f"Исследовать как неподтверждённую гипотезу: «{item.label}».",
            rule="assumptions", priority="explore", basis_item_ids=[item.assertion_id], confidence=0.45))
    search = search[:MAX_SEARCH_DIRECTIONS]
    questions = []
    if not activity_items:
        questions.append("Какие направления деятельности компания ведёт сейчас? Опишите каждое отдельной фразой или полем «направления: …».")
    if not dependency_items:
        questions.append("Какие критичные зависимости от ресурсов, поставщиков, клиентов или маршрутов подтверждены? Отсутствие сведений не означает отсутствия зависимости.")
    if any(item.status != "confirmed" for item in [*activity_items, *fact_items]):
        questions.append("Подтвердите каждое дословно извлечённое направление и сведение перед сохранением профиля Core.")
    for hypothesis in hypotheses:
        questions.append(f"Подтверждается ли зависимость «{hypothesis.text}»? Укажите основание и срок актуальности.")
    questions = questions[:100]
    return DescriptionProposal(tenant_id=req.tenant_id, company_id=req.company_id, context=context,
        hypotheses=hypotheses, questions=questions, significance=significance, search_directions=search,
        extraction_mode=mode, limitations=["proposal_only_core_owns_profile", "confidence_is_not_truth",
        "no_external_search_performed", "unparsed_free_prose_is_preserved_without_claims", *extraction_limitations])
