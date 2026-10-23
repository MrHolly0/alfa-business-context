from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from intelligence.schemas import AnalysisRequest


def request(**changes):
    data = {
        "request_id": "r1", "tenant_id": "t1", "company_id": "c1",
        "as_of": "2026-10-24T12:00:00Z", "context_version": 1,
        "source_policy_version": 1, "source_ids": ["news"],
        "company": {"name": "Компания", "industry": "логистика",
                    "dependencies": [{"dependency_id": "dep-1", "label": "подтверждённая перевозка",
                                      "kind": "supplier", "status": "confirmed", "provenance": "CRM:42"}],
                    "facts": []},
        "event": {"event_id": "e1", "title": "Событие", "text": "Текст",
                  "source_id": "news", "published_at": "2026-10-24T11:00:00Z"},
    }
    data.update(changes)
    return AnalysisRequest.model_validate(data)


def test_request_schema_is_closed_and_source_scoped():
    with pytest.raises(ValidationError):
        request(untrusted_extra="ignored?")
    with pytest.raises(ValidationError):
        request(event={"event_id": "e1", "title": "Событие", "text": "Текст",
                       "source_id": "other", "published_at": "2026-10-24T11:00:00Z"})


def test_request_rejects_ambiguous_time_and_duplicate_context_ids():
    with pytest.raises(ValidationError):
        request(as_of="2026-10-24T12:00:00")
    with pytest.raises(ValidationError):
        request(company={"name": "Компания", "dependencies": [],
                         "facts": [{"fact_id": "same", "text": "А", "provenance": "x"},
                                   {"fact_id": "same", "text": "Б", "provenance": "y"}]})


def test_event_after_as_of_is_rejected():
    with pytest.raises(ValidationError):
        request(event={"event_id": "e1", "title": "Событие", "text": "Текст",
                       "source_id": "news", "published_at": "2026-10-24T13:00:00Z"})
