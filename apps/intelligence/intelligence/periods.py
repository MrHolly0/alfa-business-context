"""Period reports combining supplied numeric series and eligible publication history.

This layer keeps the underlying arithmetic in ``research.analyze_period`` and adds
an auditable publication timeline. It does not infer event causality from coincident
metric movements or claim seasonality/significance from short or incomplete series.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Literal, Mapping, Sequence

from pydantic import Field, field_validator

from .research import PeriodObservation, PeriodReport, analyze_period
from .schemas import StrictModel, _aware


class PeriodPublication(StrictModel):
    event_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(default=1, ge=1)
    event_time: datetime | None = None
    published_at: datetime
    server_received_at: datetime | None = None
    source_id: str = Field(min_length=1, max_length=256)
    document_revision_ref: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=1000)
    quote: str = Field(min_length=1, max_length=1000)

    _aware_dates = field_validator("event_time", "published_at", "server_received_at")(
        lambda value: _aware(value) if value is not None else value)


class PeriodTimelineEntry(StrictModel):
    event_id: str
    revision: int
    event_time: datetime
    published_at: datetime
    server_received_at: datetime
    source_id: str
    document_revision_ref: str
    title: str
    quote: str

    _aware_dates = field_validator("event_time", "published_at", "server_received_at")(_aware)


class PeriodResearchReport(StrictModel):
    schema_version: Literal["period-research-v1"] = "period-research-v1"
    metrics: PeriodReport
    publication_timeline: list[PeriodTimelineEntry] = Field(default_factory=list, max_length=5000)
    coverage: dict = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list, max_length=30)


def analyze_period_research(
    observations: Sequence[PeriodObservation | Mapping], *,
    period_start: str | datetime, period_end: str | datetime,
    known_at: str | datetime, mode: Literal["retrospective", "as_known"],
    publications: Sequence[PeriodPublication | Mapping] = (),
    evaluated_at: datetime | None = None,
) -> PeriodResearchReport:
    """Compute descriptive observations and a server-receipt-bounded event timeline."""
    start = _as_aware(period_start, "period_start")
    end = _as_aware(period_end, "period_end")
    knowledge = _as_aware(known_at, "known_at")
    if evaluated_at is not None: _aware(evaluated_at)
    evaluated = evaluated_at or datetime.now(timezone.utc)
    cutoff = knowledge if mode == "as_known" else evaluated
    metric_report = analyze_period(observations, period_start=start.isoformat(),
        period_end=end.isoformat(), known_at=knowledge.isoformat(), mode=mode)

    included: list[PeriodTimelineEntry] = []
    excluded: Counter[str] = Counter()
    parsed = [p if isinstance(p, PeriodPublication) else PeriodPublication.model_validate(p) for p in publications]
    for item in parsed:
        event_time = item.event_time or item.published_at
        if not start <= event_time <= end:
            excluded["event_outside_period"] += 1
        elif event_time > cutoff:
            excluded["event_after_knowledge_cutoff"] += 1
        elif item.published_at > cutoff:
            excluded["publication_after_knowledge_cutoff"] += 1
        elif item.server_received_at is None:
            excluded["server_receipt_time_missing"] += 1
        elif item.server_received_at > cutoff:
            excluded["not_received_by_knowledge_cutoff"] += 1
        else:
            included.append(PeriodTimelineEntry(event_id=item.event_id, revision=item.revision,
                event_time=event_time, published_at=item.published_at,
                server_received_at=item.server_received_at, source_id=item.source_id,
                document_revision_ref=item.document_revision_ref, title=item.title, quote=item.quote))
    included.sort(key=lambda p: (p.event_time, p.server_received_at, p.event_id, p.revision))
    limitations = [
        "Показатели рассчитаны описательно по переданным наблюдениям.",
        "Совпадение событий и изменений показателей не доказывает причинность.",
        "Статистическая значимость, сезонность и серийная зависимость не оценивались.",
    ]
    if any(metric.observations_used < 6 for metric in metric_report.metrics):
        limitations.append("Временные ряды короткие для устойчивых статистических выводов.")
    return PeriodResearchReport(metrics=metric_report, publication_timeline=included,
        coverage={"input_publications": len(parsed), "eligible_publications": len(included),
            "excluded_by_reason": dict(sorted(excluded.items())), "cutoff": cutoff.isoformat(),
            "cutoff_basis": "known_at" if mode == "as_known" else "evaluated_at",
            "metric_observation_count": sum(m.observations_used for m in metric_report.metrics)},
        limitations=limitations)


def _as_aware(value: str | datetime, name: str) -> datetime:
    if isinstance(value, str):
        try: value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError: raise ValueError(f"{name}_must_be_aware_iso_datetime") from None
    if not isinstance(value, datetime): raise ValueError(f"{name}_must_be_aware_datetime")
    return _aware(value).astimezone(timezone.utc)
