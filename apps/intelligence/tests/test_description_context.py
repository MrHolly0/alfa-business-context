from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from intelligence.description_context import DescriptionPrepareRequest, prepare_description


AS_OF = datetime(2026, 10, 25, tzinfo=timezone.utc)


def request(description, **overrides):
    values = {"tenant_id": "tenant-demo", "company_id": "company-demo", "name": "ООО Пример",
              "description": description, "provenance": "user description", "as_of": AS_OF,
              "data_classification": "synthetic"}
    values.update(overrides)
    return DescriptionPrepareRequest(**values)


def test_explicit_mixed_business_produces_unconfirmed_quoted_proposal():
    source = "Компания производит датчики; разрабатывает ПО и импортирует керамические компоненты."
    proposal = prepare_description(request(source))
    assert proposal.extraction_mode == "deterministic"
    assert [a.label for a in proposal.context.activities] == [
        "Компания производит датчики", "разрабатывает ПО", "импортирует керамические компоненты"]
    assert [a.normalized_class for a in proposal.context.activities] == ["manufacturing", "software", "import"]
    assert all(a.status == "unknown" for a in proposal.context.activities)
    assert all(source[int(ref.split(":")[1]):int(ref.split(":")[2])] == item.label
               for item in proposal.context.activities for ref in item.evidence_refs)
    assert proposal.context.facts == []
    assert len(proposal.search_directions) == 3
    assert all(signal.confidence < 1 for signal in proposal.significance)
    assert any("Подтвердите" in question for question in proposal.questions)


def test_core_confirmed_only_confirms_literal_claims_and_dependency():
    source = "Направления: производство датчиков; разработка ПО\nЗависит от: импортных микросхем"
    proposal = prepare_description(request(source, statement_status="core_confirmed"))
    assert len(proposal.context.activities) == 2
    assert all(item.status == "confirmed" for item in proposal.context.activities)
    assert len(proposal.context.dependencies) == 1
    dependency = proposal.context.dependencies[0]
    assert dependency.status == "confirmed"
    assert dependency.label == "импортных микросхем"
    assert dependency.provenance == "Core-confirmed user statement, not independent verification"


def test_dependency_field_applies_relation_header_to_every_exact_list_item():
    source = "Зависит от: поставок через Северный порт; доступности OrionCloud."
    proposal = prepare_description(request(source, statement_status="core_confirmed"))
    assert [item.label for item in proposal.context.dependencies] == [
        "поставок через Северный порт", "доступности OrionCloud"]
    assert all(item.status == "confirmed" for item in proposal.context.dependencies)
    second = proposal.context.dependencies[1]
    item_ref, field_ref = second.evidence_refs
    assert source[int(item_ref.split(":")[1]):int(item_ref.split(":")[2])] == "доступности OrionCloud"
    assert source[int(field_ref.split(":")[1]):int(field_ref.split(":")[2])] == source


def test_dependency_list_items_are_pending_until_confirmation():
    source = "Зависит от: поставок через Северный порт; доступности OrionCloud."
    proposal = prepare_description(request(source))
    assert proposal.context.dependencies == []
    assert [item.label for item in proposal.context.assertions] == [
        "поставок через Северный порт", "доступности OrionCloud"]


@pytest.mark.parametrize("source", [
    "Не зависит от: поставок через Северный порт; доступности OrionCloud.",
    "Возможно зависит от: поставок через Северный порт; доступности OrionCloud.",
])
def test_qualified_dependency_field_header_does_not_confirm_any_list_items(source):
    proposal = prepare_description(request(source, statement_status="core_confirmed"))
    assert proposal.context.dependencies == []
    assert len(proposal.context.assertions) == 2


@pytest.mark.parametrize("item", ["не зависит от OrionCloud", "возможно зависит от OrionCloud"])
def test_negative_or_modal_dependency_list_item_is_not_confirmed(item):
    source = f"Зависит от: поставок через Северный порт; {item}."
    proposal = prepare_description(request(source, statement_status="core_confirmed"))
    assert [row.label for row in proposal.context.dependencies] == ["поставок через Северный порт"]
    assert any(row.label == item and row.status == "assumption" for row in proposal.context.assertions)


def test_unconfirmed_literal_dependency_is_assumption_not_fact():
    proposal = prepare_description(request("Компания зависит от единственного логистического маршрута."))
    assert proposal.context.dependencies == []
    assert len(proposal.context.assertions) == 1
    assert proposal.context.assertions[0].status == "assumption"
    assert len(proposal.hypotheses) == 1


def test_free_prose_is_preserved_without_sector_guessing():
    proposal = prepare_description(request("Крупная компания из промышленной отрасли."))
    assert proposal.context.description == "Крупная компания из промышленной отрасли."
    assert proposal.context.activities == []
    assert proposal.context.dependencies == []
    assert proposal.context.facts == []
    assert proposal.search_directions == []
    assert proposal.questions


def test_closed_request_timezone_and_limits():
    with pytest.raises(ValidationError):
        request("x", as_of=datetime(2026, 10, 25))
    with pytest.raises(ValidationError):
        request("x", surprise="field")


class FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"content": self.content}


def test_llm_output_must_use_exact_offsets_and_invalid_output_falls_back():
    source = "Компания производит датчики."
    fake = FakeLLM('{"activities":[{"text":"выдуманные медицинские приборы","start":0,"end":29,"quote":"Компания производит датчики."}],"dependencies":[],"facts":[]}')
    proposal = prepare_description(request(source), fake)
    assert proposal.extraction_mode == "deterministic_fallback"
    assert proposal.context.activities[0].label == source[:-1]
    assert fake.calls[0][1] == {"max_input_tokens": 6000, "max_output_tokens": 1200, "json_mode": True}


def test_llm_is_not_called_for_confidential_input():
    fake = FakeLLM('{"activities":[],"dependencies":[],"facts":[]}')
    with pytest.raises(ValueError, match="not permitted"):
        prepare_description(request("Текст", data_classification="confidential"), fake)
    assert fake.calls == []


def test_llm_relation_is_always_assumption_even_after_core_confirmation():
    source = "Компания зависит от импорта микросхем."
    start = source.index("зависит")
    quote = source[start:]
    payload = {"activities": [], "dependencies": [{"text": quote, "start": start, "end": len(source),
                "quote": quote, "inferred": False}], "facts": []}
    import json
    proposal = prepare_description(request(source, statement_status="core_confirmed"), FakeLLM(json.dumps(payload, ensure_ascii=False)))
    assert proposal.context.dependencies == []
    assert proposal.context.assertions[0].status == "assumption"


@pytest.mark.parametrize("source", [
    "Компания не зависит от порта.",
    "Компания возможно зависит от порта.",
])
def test_negative_or_modal_dependency_is_never_core_confirmed(source):
    proposal = prepare_description(request(source, statement_status="core_confirmed"))
    assert proposal.context.dependencies == []
    assert proposal.context.assertions
    assert proposal.context.assertions[0].status == "assumption"
    assert "не зависит" in proposal.context.assertions[0].label or "возможно зависит" in proposal.context.assertions[0].label


@pytest.mark.parametrize("source", [
    "Компания не производит датчики.",
    "Компания планирует производить датчики.",
])
def test_negative_or_planned_activity_is_not_core_confirmed(source):
    proposal = prepare_description(request(source, statement_status="core_confirmed"))
    assert proposal.context.activities
    assert all(activity.status != "confirmed" for activity in proposal.context.activities)


def test_llm_cannot_confirm_activity_even_when_its_exact_span_omits_negation():
    source = "Компания не производит датчики."
    quote = "производит датчики"
    start = source.index(quote)
    payload = {"activities": [{"text": quote, "start": start, "end": start + len(quote), "quote": quote}],
               "dependencies": [], "facts": []}
    import json
    proposal = prepare_description(request(source, statement_status="core_confirmed"),
                                   FakeLLM(json.dumps(payload, ensure_ascii=False)))
    assert proposal.context.activities[0].status == "unknown"
    assert "не производит" in source[:start + len(quote)]


def test_search_prioritizes_confirmed_dependencies_before_activity_leads_and_hypotheses():
    source = "Компания производит датчики. Зависит от: импортных микросхем."
    proposal = prepare_description(request(source, statement_status="core_confirmed"))
    assert proposal.search_directions[0].rule == "confirmed_dependency_match"
    assert proposal.search_directions[0].priority == "high"
    assert "0.80" in proposal.significance[0].reason
    assert proposal.search_directions[1].rule == "activity_lead"



def test_observation_timestamp_and_uncertain_fact_remain_explicit():
    request = DescriptionPrepareRequest(tenant_id="t", company_id="c", name="Компания",
        description="Производит приборы.\nЗависит от: маршрута А.\nФакты: возможно, запас рассчитан на 4 дня.",
        provenance="user form", as_of="2026-10-25T10:00:00Z", data_classification="synthetic",
        statement_status="core_confirmed")
    result = prepare_description(request)
    assert all(item.observed_at == request.as_of for item in [*result.context.activities,
        *result.context.facts, *result.context.dependencies])
    assert result.context.facts[0].status == "unknown"
    assert result.questions


def test_unique_exact_quote_offsets_are_resolved_locally_without_confirmation():
    import json
    source = "Мы выпускаем приборы для теплиц."
    quote = "выпускаем приборы для теплиц"
    fake = FakeLLM(json.dumps({"activities": [{"text": quote, "quote": quote, "start": 0, "end": 1}],
        "dependencies": [], "facts": []}, ensure_ascii=False))
    result = prepare_description(request(source, statement_status="core_confirmed"), fake)
    assert result.extraction_mode == "llm"
    item = result.context.activities[0]
    assert item.status == "unknown"
    assert item.evidence_refs == [f"description:{source.index(quote)}:{source.index(quote)+len(quote)}"]


def test_ambiguous_quote_with_invalid_offsets_is_rejected():
    import json
    source = "Приборы и приборы"
    fake = FakeLLM(json.dumps({"activities": [{"text": "приборы", "quote": "приборы", "start": 99, "end": 100}],
        "dependencies": [], "facts": []}, ensure_ascii=False))
    result = prepare_description(request(source.lower()), fake)
    assert result.extraction_mode == "deterministic_fallback"
    assert "llm_extraction_output_rejected" in result.limitations
