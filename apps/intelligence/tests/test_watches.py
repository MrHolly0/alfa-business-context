from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from intelligence.watches import WatchCondition, WatchObservation, evaluate_conditions


AS_OF = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def test_metric_watch_met_includes_only_current_observation_evidence():
    condition = WatchCondition(condition_id="delay-threshold", company_id="co-1",
        kind="metric_threshold", provenance="операционный отчёт", evidence_refs=["context:route"],
        metric="delivery_delay", unit="days", operator="ge", threshold=Decimal("5"))
    observations = [WatchObservation(observation_id="old", company_id="co-1", metric="delivery_delay",
            value=2, unit="days", observed_at="2026-10-20T00:00:00Z", available_at="2026-10-20T01:00:00Z",
            source_refs=["receipt:old"]),
        WatchObservation(observation_id="future", company_id="co-1", metric="delivery_delay",
            value=8, unit="days", observed_at="2026-10-25T00:00:00Z", available_at="2026-10-25T01:00:00Z",
            source_refs=["receipt:future"]),
        WatchObservation(observation_id="current", company_id="co-1", metric="delivery_delay",
            value=6, unit="days", observed_at="2026-10-24T10:00:00Z", available_at="2026-10-24T11:00:00Z",
            source_refs=["receipt:current"])]
    [result] = evaluate_conditions([condition], as_of=AS_OF, context_versions={}, document_versions={}, observations=observations)
    assert result.status == "met"
    assert result.requires_reassessment is True
    assert result.core_action == "request_reassessment"
    assert result.observed_value == "6"
    assert result.evidence_refs == ["context:route", "receipt:current"]


def test_missing_or_future_metric_is_unknown_and_asks_for_data():
    condition = {"condition_id": "stock", "company_id": "co-1", "kind": "metric_threshold",
        "provenance": "анкета", "metric": "stock", "unit": "units", "operator": "le", "threshold": 10}
    future = {"observation_id": "future", "company_id": "co-1", "metric": "stock", "value": 0,
        "unit": "units", "observed_at": "2026-10-25T00:00:00Z", "available_at": "2026-10-24T10:00:00Z",
        "source_refs": ["receipt:future"]}
    [result] = evaluate_conditions([condition], as_of=AS_OF, context_versions={}, document_versions={}, observations=[future])
    assert result.status == "unknown"
    assert result.core_action == "await_data"
    assert result.requires_reassessment is False
    assert result.next_question
    assert "receipt:future" not in result.evidence_refs


def test_metric_unit_mismatch_is_unknown_instead_of_converting_or_claiming_false():
    condition = {"condition_id": "stock", "company_id": "co-1", "kind": "metric_threshold",
        "provenance": "анкета", "metric": "stock", "unit": "units", "operator": "le", "threshold": 10}
    observation = {"observation_id": "stock", "company_id": "co-1", "metric": "stock", "value": 8,
        "unit": "boxes", "observed_at": "2026-10-24T10:00:00Z", "available_at": "2026-10-24T11:00:00Z",
        "source_refs": ["receipt:stock"]}
    [result] = evaluate_conditions([condition], as_of=AS_OF, context_versions={}, document_versions={},
        observations=[observation])
    assert result.status == "unknown"
    assert result.reason == "metric_unit_mismatch"
    assert result.core_action == "await_data"


def test_context_and_document_revision_conditions_use_exact_core_snapshots():
    conditions = [
        WatchCondition(condition_id="context", company_id="co-1", kind="context_revision",
            target_ref="company-context", expected_revision=3, provenance="Core context snapshot"),
        WatchCondition(condition_id="document", company_id="co-1", kind="document_revision",
            target_ref="doc-1", expected_revision="doc-1:2", provenance="Core document version"),
        WatchCondition(condition_id="missing", company_id="co-1", kind="document_revision",
            target_ref="doc-absent", expected_revision="doc-absent:1", provenance="Core document version")]
    results = evaluate_conditions(conditions, as_of=AS_OF, context_versions={"co-1": 4},
        document_versions={"doc-1": "doc-1:2"}, observations=[])
    assert [r.status for r in results] == ["met", "not_met", "unknown"]
    assert [r.core_action for r in results] == ["request_reassessment", "none", "await_data"]


def test_due_date_is_a_deterministic_trigger_and_bad_conditions_are_rejected():
    [result] = evaluate_conditions([{"condition_id": "due", "company_id": "co-1", "kind": "date_due",
        "provenance": "Core schedule", "due_at": "2026-10-24T11:00:00Z"}], as_of=AS_OF,
        context_versions={}, document_versions={}, observations=[])
    assert result.status == "met" and result.requires_reassessment
    with pytest.raises(ValidationError, match="date_due_requires_due_at"):
        WatchCondition(condition_id="bad", company_id="co-1", kind="date_due", provenance="schedule")
    with pytest.raises(ValidationError):
        WatchObservation(observation_id="nan", company_id="co-1", metric="stock", value="NaN", unit="u",
            observed_at=AS_OF, available_at=AS_OF, source_refs=["receipt:1"])
    with pytest.raises(ValidationError):
        WatchCondition(condition_id="injection", company_id="co-1", kind="date_due", provenance="schedule",
            due_at=AS_OF, threshold="__import__('os').system('true')")
