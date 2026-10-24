from datetime import datetime, timezone

from intelligence.periods import PeriodPublication, analyze_period_research


START = "2026-10-01T00:00:00Z"
END = "2026-10-31T23:59:59Z"
KNOWN = "2026-10-20T12:00:00Z"


def publication(event_id, *, event_time="2026-10-10T09:00:00Z", published_at="2026-10-10T10:00:00Z",
                received="2026-10-10T10:05:00Z", revision=1):
    return {"event_id": event_id, "revision": revision, "event_time": event_time,
        "published_at": published_at, "server_received_at": received,
        "source_id": "news", "document_revision_ref": f"{event_id}:{revision}",
        "title": "Событие", "quote": "Источник сообщает точный фрагмент."}


def test_period_timeline_uses_event_time_and_only_server_received_pre_cutoff():
    report = analyze_period_research([], period_start=START, period_end=END, known_at=KNOWN,
        mode="as_known", publications=[publication("eligible"),
            publication("late-receipt", received="2026-10-21T00:00:00Z"),
            publication("late-published", published_at="2026-10-21T00:00:00Z", received="2026-10-21T00:01:00Z"),
            publication("missing-receipt", received=None),
            publication("future-event", event_time="2026-10-22T00:00:00Z")])
    assert [entry.event_id for entry in report.publication_timeline] == ["eligible"]
    assert report.publication_timeline[0].document_revision_ref == "eligible:1"
    assert report.coverage["excluded_by_reason"] == {
        "event_after_knowledge_cutoff": 1,
        "not_received_by_knowledge_cutoff": 1,
        "publication_after_knowledge_cutoff": 1,
        "server_receipt_time_missing": 1,
    }
    assert any("причинность" in text for text in report.limitations)
    assert any("сезонность" in text for text in report.limitations)


def test_event_time_falls_back_to_published_time_and_timeline_keeps_exact_revision():
    report = analyze_period_research([], period_start=START, period_end=END, known_at=KNOWN,
        mode="as_known", publications=[publication("corrected", event_time=None, revision=2)])
    assert len(report.publication_timeline) == 1
    assert report.publication_timeline[0].revision == 2
    assert report.publication_timeline[0].event_time.isoformat() == "2026-10-10T10:00:00+00:00"
    assert report.publication_timeline[0].quote == "Источник сообщает точный фрагмент."


def test_metric_series_remain_descriptive_and_select_latest_available_correction():
    observations = [
        {"observation_id": "delivery-1", "metric": "delivery delay", "observed_at": "2026-10-10T00:00:00Z",
         "available_at": "2026-10-11T00:00:00Z", "value": 5, "unit": "days", "source_ref": "receipt:1"},
        {"observation_id": "delivery-1", "metric": "delivery delay", "observed_at": "2026-10-10T00:00:00Z",
         "available_at": "2026-10-15T00:00:00Z", "value": 7, "unit": "days", "source_ref": "receipt:1-correction", "revision": 2},
        {"observation_id": "stock-1", "metric": "stock", "observed_at": "2026-10-10T00:00:00Z",
         "available_at": "2026-10-11T00:00:00Z", "value": 3, "unit": "units", "source_ref": "receipt:2"},
    ]
    report = analyze_period_research(observations, period_start=START, period_end=END, known_at=KNOWN,
        mode="as_known", publications=[])
    by_metric = {item.metric: item for item in report.metrics.metrics}
    assert by_metric["delivery delay"].first_value == 7
    assert by_metric["delivery delay"].source_refs == ["receipt:1-correction"]
    assert {item.unit for item in report.metrics.metrics} == {"days", "units"}
    assert any("описательно" in text for text in report.limitations)
    assert "Статистическая значимость" in " ".join(report.limitations)
