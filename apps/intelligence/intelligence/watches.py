"""Pure evaluation of Core-owned, explicitly typed watch snapshots.

This module cannot create, edit, enable, or close a watch. Core supplies the
conditions and current version/observation snapshots; this evaluator returns a
bounded recommendation to enqueue reassessment when an observable condition is
met. Missing data is unknown, never evidence that a condition is false.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Mapping, Sequence

from pydantic import Field, field_validator, model_validator

from .schemas import StrictModel, _aware


class WatchCondition(StrictModel):
    condition_id: str = Field(min_length=1, max_length=200)
    company_id: str = Field(min_length=1, max_length=256)
    kind: Literal["date_due", "metric_threshold", "context_revision", "document_revision"]
    provenance: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)
    target_ref: str | None = Field(default=None, max_length=512)
    metric: str | None = Field(default=None, max_length=300)
    unit: str | None = Field(default=None, max_length=100)
    operator: Literal["ge", "le", "eq"] | None = None
    threshold: Decimal | None = None
    due_at: datetime | None = None
    expected_revision: int | str | None = None

    _due_aware = field_validator("due_at")(lambda value: _aware(value) if value is not None else value)

    @field_validator("threshold", mode="before")
    @classmethod
    def finite_threshold(cls, value):
        if value is None: return value
        try: number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError): raise ValueError("threshold_must_be_finite_decimal") from None
        if not number.is_finite(): raise ValueError("threshold_must_be_finite_decimal")
        return number

    @model_validator(mode="after")
    def kind_fields(self):
        populated = {name for name in ("target_ref", "metric", "unit", "operator", "threshold", "due_at", "expected_revision")
                     if getattr(self, name) is not None}
        allowed = {
            "date_due": {"due_at"},
            "metric_threshold": {"metric", "unit", "operator", "threshold"},
            "context_revision": {"target_ref", "expected_revision"},
            "document_revision": {"target_ref", "expected_revision"},
        }[self.kind]
        if populated - allowed: raise ValueError("fields_not_allowed_for_watch_kind")
        if self.kind == "date_due" and self.due_at is None: raise ValueError("date_due_requires_due_at")
        if self.kind == "metric_threshold" and not all((self.metric, self.unit, self.operator, self.threshold is not None)):
            raise ValueError("metric_threshold_requires_metric_unit_operator_threshold")
        if self.kind in {"context_revision", "document_revision"} and not self.target_ref:
            raise ValueError("revision_watch_requires_target_ref")
        if self.kind in {"context_revision", "document_revision"} and self.expected_revision is None:
            raise ValueError("revision_watch_requires_expected_revision")
        if self.kind == "context_revision" and (isinstance(self.expected_revision, bool) or not isinstance(self.expected_revision, int)):
            raise ValueError("context_revision_requires_integer")
        if self.kind == "document_revision" and not isinstance(self.expected_revision, str):
            raise ValueError("document_revision_requires_string")
        return self


class WatchObservation(StrictModel):
    observation_id: str = Field(min_length=1, max_length=200)
    company_id: str = Field(min_length=1, max_length=256)
    metric: str = Field(min_length=1, max_length=300)
    value: Decimal
    unit: str = Field(min_length=1, max_length=100)
    observed_at: datetime
    available_at: datetime
    source_refs: list[str] = Field(min_length=1, max_length=30)

    _aware_dates = field_validator("observed_at", "available_at")(_aware)

    @field_validator("value", mode="before")
    @classmethod
    def finite_value(cls, value):
        try: number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError): raise ValueError("observation_value_must_be_finite_decimal") from None
        if not number.is_finite(): raise ValueError("observation_value_must_be_finite_decimal")
        return number


class WatchEvaluation(StrictModel):
    condition_id: str
    company_id: str
    status: Literal["met", "not_met", "unknown"]
    reason: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(default_factory=list, max_length=60)
    observed_value: str | None = None
    observed_at: datetime | None = None
    requires_reassessment: bool = False
    core_action: Literal["none", "await_data", "request_reassessment"] = "none"
    next_question: str | None = Field(default=None, max_length=500)

    _observed_aware = field_validator("observed_at")(lambda value: _aware(value) if value is not None else value)


def evaluate_conditions(
    watches: Sequence[WatchCondition | Mapping], *, as_of: datetime,
    context_versions: Mapping[str, int], document_versions: Mapping[str, str],
    observations: Sequence[WatchObservation | Mapping],
) -> list[WatchEvaluation]:
    """Evaluate supplied conditions against snapshots, without side effects or inference."""
    _aware(as_of)
    parsed_watches = [w if isinstance(w, WatchCondition) else WatchCondition.model_validate(w) for w in watches]
    parsed_observations = [o if isinstance(o, WatchObservation) else WatchObservation.model_validate(o) for o in observations]
    eligible = [o for o in parsed_observations if o.observed_at <= as_of and o.available_at <= as_of]
    results: list[WatchEvaluation] = []
    for watch in parsed_watches:
        refs = list(watch.evidence_refs)
        status: Literal["met", "not_met", "unknown"] = "unknown"
        reason = "required_current_signal_unavailable"
        observed_value = None
        observed_at = None
        question = None
        if watch.kind == "date_due":
            status = "met" if as_of >= watch.due_at else "not_met"
            reason = "due_time_reached" if status == "met" else "due_time_not_reached"
            observed_value = as_of.isoformat()
            observed_at = as_of
        elif watch.kind == "context_revision":
            current = context_versions.get(watch.company_id)
            if current is None:
                question = "Какова текущая версия подтверждённого контекста компании?"
            else:
                observed_value = str(current)
                status = "met" if current != watch.expected_revision else "not_met"
                reason = "context_revision_changed" if status == "met" else "context_revision_unchanged"
        elif watch.kind == "document_revision":
            current = document_versions.get(watch.target_ref)
            if current is None:
                question = "Доступна ли актуальная версия указанного документа?"
            else:
                observed_value = current
                status = "met" if current != watch.expected_revision else "not_met"
                reason = "document_revision_changed" if status == "met" else "document_revision_unchanged"
        else:
            signals = [o for o in eligible if o.company_id == watch.company_id and o.metric == watch.metric]
            if not signals:
                question = f"Каково актуальное значение показателя «{watch.metric}» в единицах «{watch.unit}»?"
            else:
                signals.sort(key=lambda o: (o.observed_at, o.available_at, o.observation_id))
                signal = signals[-1]
                observed_at = signal.observed_at
                refs.extend(signal.source_refs)
                if signal.unit != watch.unit:
                    question = f"Нужен показатель «{watch.metric}» в единицах «{watch.unit}»; получены «{signal.unit}»."
                    reason = "metric_unit_mismatch"
                else:
                    observed_value = str(signal.value)
                    is_met = {"ge": signal.value >= watch.threshold, "le": signal.value <= watch.threshold,
                              "eq": signal.value == watch.threshold}[watch.operator]
                    status = "met" if is_met else "not_met"
                    reason = "metric_threshold_reached" if is_met else "metric_threshold_not_reached"
        if status == "unknown" and question is None:
            question = "Какое актуальное подтверждённое наблюдение нужно для проверки условия?"
        requires_reassessment = status == "met"
        action = "request_reassessment" if requires_reassessment else "await_data" if status == "unknown" else "none"
        results.append(WatchEvaluation(condition_id=watch.condition_id, company_id=watch.company_id,
            status=status, reason=reason, evidence_refs=list(dict.fromkeys(refs)),
            observed_value=observed_value, observed_at=observed_at,
            requires_reassessment=requires_reassessment, core_action=action, next_question=question))
    return results
