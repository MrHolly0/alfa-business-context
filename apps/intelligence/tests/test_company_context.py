from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from intelligence.collection import SourceGrant, SourcePolicySnapshot
from intelligence.company_context import project_company_context
from intelligence.multi_agent import MultiAgentPipeline
from intelligence.schemas import AnalysisRequest
from intelligence.workflow import BusinessSnapshot, DataTask


AS_OF = datetime(2026, 10, 25, tzinfo=timezone.utc)


def test_projection_keeps_only_confirmed_effective_items_and_reports_gaps():
    context = {
        "name": "Компания",
        "activities": [
            {"activity_id": "a1", "label": "Изготовление деталей", "normalized_class": "manufacturing",
             "status": "confirmed", "provenance": "CSV#row=2"},
            {"activity_id": "a2", "label": "Импорт электроники", "status": "assumption", "provenance": "interview"},
            {"activity_id": "a3", "label": "Бывшее направление", "status": "confirmed", "provenance": "registry",
             "valid_until": "2026-01-01T00:00:00Z"},
        ],
        "assertions": [
            {"assertion_id": "c1", "kind": "supplier", "label": "Поставщик стали", "status": "contradiction", "provenance": "two records"},
            {"assertion_id": "c2", "kind": "contract", "label": "Цена фиксирована", "status": "unknown", "provenance": "no contract"},
        ],
        "dependencies": [
            {"dependency_id": "d1", "label": "Поставка металла", "kind": "supplier", "status": "confirmed", "provenance": "CRM"}
        ],
        "facts": [
            {"fact_id": "f1", "text": "Москва", "provenance": "CSV#row=2", "observed_at": "2026-10-01T00:00:00Z"}
        ],
    }
    projection = project_company_context(context, as_of=AS_OF)
    assert [item.activity_id for item in projection.activities] == ["a1"]
    assert [item.dependency_id for item in projection.dependencies] == ["d1"]
    assert [item.fact_id for item in projection.facts] == ["f1"]
    assert {item["assertion_id"] for item in projection.unresolved if item["group"] == "assertions"} == {"c1", "c2"}
    assert any("dependencies-missing" == item.item_id for item in projection.questions) is False
    assert any(item.item_id == "a2" for item in projection.questions)
    assert any(item.item_id == "a3" for item in projection.questions)


def test_missing_activity_and_dependency_create_useful_questions_without_negating_them():
    projection = project_company_context({"name": "Профиль без направлений"}, as_of=AS_OF)
    assert "activities_unknown" in projection.limitations
    assert "dependencies_not_confirmed" in projection.limitations
    assert any("какие направления" in item.question.casefold() for item in projection.questions)
    assert any("не означает отсутствия зависимости" in item.question for item in projection.questions)


def test_legacy_fact_is_projected_with_explicit_unknown_freshness():
    projection = project_company_context({"name": "Компания", "facts": [
        {"fact_id": "old", "text": "Старый факт", "provenance": "legacy"}
    ]}, as_of=AS_OF)
    assert [item.fact_id for item in projection.facts] == ["old"]
    assert "freshness_unknown:facts:old" in projection.limitations
    assert any(item.item_id == "old" for item in projection.questions)


def test_closed_enums_and_timezone_validation():
    with pytest.raises(ValidationError):
        project_company_context({"name": "x", "activities": [
            {"activity_id": "a", "label": "Неизвестно", "normalized_class": "services", "provenance": "x"}
        ]}, as_of=AS_OF)
    with pytest.raises(ValueError):
        project_company_context({"name": "x"}, as_of=datetime(2026, 1, 1))


def test_future_observation_is_not_returned_for_historical_projection():
    projection = project_company_context({"name": "Компания",
        "facts": [{"fact_id": "future-secret-fact", "text": "FUTURE_SECRET_FACT", "provenance": "FUTURE_SECRET_SOURCE",
                   "evidence_refs": ["FUTURE_SECRET_REF"], "observed_at": "2026-10-26T00:00:00Z"}],
        "dependencies": [{"dependency_id": "future-secret-dep", "label": "FUTURE_SECRET_DEP", "kind": "supplier",
                          "status": "confirmed", "provenance": "FUTURE_SECRET_DEP_SOURCE",
                          "observed_at": "2026-10-26T00:00:00Z"}],
        "activities": [{"activity_id": "future-secret-activity", "label": "FUTURE_SECRET_ACTIVITY",
                        "provenance": "FUTURE_SECRET_ACTIVITY_SOURCE", "valid_from": "2026-10-26T00:00:00Z"}],
        "assertions": [{"assertion_id": "future-secret-assertion", "kind": "supplier", "label": "FUTURE_SECRET_ASSERTION",
                        "status": "confirmed", "provenance": "FUTURE_SECRET_ASSERTION_SOURCE",
                        "valid_from": "2026-10-26T00:00:00Z"}],
    }, as_of=AS_OF)
    assert projection.facts == projection.dependencies == projection.activities == projection.assertions == []
    assert projection.unresolved == []
    assert projection.limitations == ["profile_item_not_known_at_as_of", "activities_unknown", "dependencies_not_confirmed"]
    assert not any("FUTURE_SECRET" in repr(item) or "future-secret-id" in repr(item)
                   for item in (projection.unresolved, projection.questions, projection.limitations))


def test_future_profile_data_is_absent_from_serialized_research_messages():
    request = AnalysisRequest.model_validate({
        "request_id": "historical-review", "tenant_id": "tenant", "company_id": "company",
        "as_of": "2026-10-25T00:00:00Z", "context_version": 1, "source_policy_version": 1,
        "source_ids": ["news"],
        "company": {
            "name": "Компания",
            "facts": [{"fact_id": "future-secret-id", "text": "FUTURE_SECRET_LABEL",
                       "provenance": "FUTURE_SECRET_SOURCE", "evidence_refs": ["FUTURE_SECRET_REF"],
                       "observed_at": "2026-10-26T00:00:00Z"}],
        },
        "event": {"event_id": "event", "title": "Новость", "text": "Текст",
                  "source_id": "news", "published_at": "2026-10-24T00:00:00Z"},
    })
    messages = MultiAgentPipeline._research_messages(request)
    serialized = "\n".join(message["content"] for message in messages)
    assert "FUTURE_SECRET" not in serialized
    assert "future-secret-id" not in serialized
    assert "profile_item_not_known_at_as_of" in serialized


@pytest.mark.parametrize("group,item", [
    ("facts", {"fact_id": "x", "text": "x", "provenance": "x"}),
    ("activities", {"activity_id": "x", "label": "x", "provenance": "x"}),
    ("assertions", {"assertion_id": "x", "kind": "other", "label": "x", "status": "confirmed", "provenance": "x"}),
    ("dependencies", {"dependency_id": "x", "label": "x", "kind": "other", "status": "confirmed", "provenance": "x"}),
])
def test_all_context_item_types_reject_reversed_validity_window(group, item):
    item.update(valid_from="2026-10-10T00:00:00Z", valid_until="2026-10-09T00:00:00Z")
    with pytest.raises(ValidationError):
        project_company_context({"name": "Компания", group: [item]}, as_of=AS_OF)


def test_duplicate_ids_are_rejected_in_datatask_input():
    grant = SourceGrant(source_id="news", source_role="external", uri="https://news.example/rss",
        content_type="application/rss+xml", publisher="Example", article_hosts=("news.example",),
        data_classification="public")
    policy = SourcePolicySnapshot(policy_id="policy", revision=1, tenant_id="tenant",
        issued_at=AS_OF, sources=(grant,))
    duplicate_context = {"name": "Компания", "facts": [
        {"fact_id": "same", "text": "Факт", "provenance": "CSV"}
    ], "dependencies": [
        {"dependency_id": "same", "label": "Зависимость", "kind": "supplier", "status": "confirmed", "provenance": "CRM"}
    ]}
    with pytest.raises(ValidationError, match="company context identifiers must be unique"):
        DataTask(schema_version="ai-data-task-v1", request_id="r", tenant_id="tenant", company_id="c",
            context_version=1, source_policy_version=1, as_of=AS_OF, source_policy=policy,
            businesses=[BusinessSnapshot(company_id="c", context_version=1, company=duplicate_context)])
