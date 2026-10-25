from __future__ import annotations

import pytest
from pydantic import ValidationError

from intelligence.relevance import RankRequest, RankResult, rank_events

AS_OF = "2026-10-24T12:00:00Z"


def company(**overrides):
    value = {"name": "ООО Север", "industry": "логистика", "description": "", "role": "CEO", "dependencies": [], "facts": []}
    value.update(overrides)
    return value


def event(event_id, text, *, source="news", published="2026-10-23T00:00:00Z", revision=1, title="Событие"):
    return {"event_id": event_id, "revision": revision, "title": title, "text": text, "source_id": source, "published_at": published}


def run(company_data, events, sources=("news",)):
    return rank_events(tenant_id="tenant", company=company_data, events=events, source_ids=list(sources), as_of=AS_OF)


def test_same_event_ranks_differently_for_confirmed_company_dependencies():
    news = [event("e1", "Импорт газа остановлен после новых ограничений")]
    energy = company(dependencies=[{"dependency_id": "dep-gas", "label": "импорт природного газа", "kind": "supplier", "status": "confirmed", "provenance": "profile:1"}])
    software = company(name="ООО Код", industry="разработка ПО", dependencies=[{"dependency_id": "dep-api", "label": "облачные API", "kind": "technology", "status": "confirmed", "provenance": "profile:1"}])
    assert run(energy, news)["ranked"][0]["score"] == 5
    assert run(software, news)["ranked"][0]["score"] == 0


def test_source_and_as_of_filters_precede_ranking():
    result = run(company(), [
        event("future", "логистика логистика логистика", published="2026-10-25T00:00:00Z"),
        event("blocked", "логистика логистика логистика", source="private"),
        event("ok", "обычная заметка"),
    ])
    assert [row["event_id"] for row in result["ranked"]] == ["ok"]
    assert {(row["event_id"], row["reason"]) for row in result["excluded"]} == {
        ("future", "published_after_as_of"), ("blocked", "source_not_allowed")
    }


def test_industry_only_match_is_low_and_never_claims_impact():
    result = run(company(industry="транспорт логистика"), [event("e", "Проблемы в транспорт логистика") ])
    item = result["ranked"][0]
    assert item["score"] == 1 and item["relevance"] == "low"
    assert [r["kind"] for r in item["reasons"]] == ["industry"]
    assert all("влияни" not in r["text"].casefold() for r in item["reasons"])


def test_latest_eligible_revision_wins_and_same_revision_conflict_fails():
    result = run(company(), [event("e", "нет совпадений", revision=1), event("e", "логистика в работе", revision=2)])
    assert len(result["ranked"]) == 1
    with pytest.raises(ValueError, match="conflicting event content"):
        run(company(), [event("e", "первый текст", revision=1), event("e", "другой текст", revision=1)])


def test_role_is_only_tie_breaker_and_cannot_change_score():
    events = [event("fx", "Изменился курс валют"), event("supply", "Задержаны поставки деталей")]
    company_data = company(role="директор закупок", industry="")
    result = run(company_data, events)
    assert [r["event_id"] for r in result["ranked"]] == ["supply", "fx"]
    assert [r["score"] for r in result["ranked"]] == [0, 0]
    assert result["ranked"][0]["relevance"] == "low"
    assert "role_tiebreak" in [reason["kind"] for reason in result["ranked"][0]["reasons"]]


def test_hypothesis_and_payment_or_group_context_never_raise_score():
    company_data = company(dependencies=[
        {"dependency_id": "maybe-oil", "label": "поставки нефти", "kind": "supplier", "status": "hypothesis", "provenance": "unverified"},
        {"dependency_id": "group", "label": "группа север", "kind": "group", "status": "confirmed", "provenance": "registry"},
        {"dependency_id": "payment", "label": "поставщик деталей", "kind": "payment", "status": "confirmed", "provenance": "transactions"},
    ])
    result = run(company_data, [event("e", "Поставки нефти и деталей прерваны")])
    item = result["ranked"][0]
    assert item["score"] == 0
    assert item["relevance"] == "low"
    assert item["reasons"] == []
    assert set(item for item in result["unsupported_context"]) == {"maybe-oil", "group", "payment"}


def test_ё_normalizes_to_e_and_output_models_forbid_extra_fields():
    result = run(company(dependencies=[{"dependency_id": "d", "label": "берёзовая древесина", "kind": "supplier", "status": "confirmed", "provenance": "profile"}]), [event("e", "Березовая древесина дорожает")])
    assert result["ranked"][0]["score"] == 5
    with pytest.raises(ValidationError):
        RankResult.model_validate({**result, "unexpected": True})
    with pytest.raises(ValidationError):
        RankRequest.model_validate({"tenant_id": "t", "company": company(), "events": [], "source_ids": [], "as_of": AS_OF, "unexpected": 1})


def test_all_confirmed_activities_participate_and_unconfirmed_activity_does_not():
    company_data = company(industry="", activities=[
        {"activity_id": "act-build", "label": "производство редукторов", "normalized_class": "manufacturing",
         "status": "confirmed", "provenance": "profile:activity:1"},
        {"activity_id": "act-software", "label": "разработка ПО для складов", "normalized_class": "software",
         "status": "confirmed", "provenance": "profile:activity:2"},
        {"activity_id": "act-assumed", "label": "импорт турбин", "normalized_class": "import",
         "status": "assumption", "provenance": "model-proposal"},
    ])
    result = run(company_data, [event("e", "Разработка ПО для складов расширяется")])
    item = result["ranked"][0]
    assert item["score"] == 3
    assert item["reasons"][0]["kind"] == "company_activity"
    assert item["reasons"][0]["context_ref"] == "act-software"
    assert "act-assumed" in result["unsupported_context"]
