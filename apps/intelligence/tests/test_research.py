from types import SimpleNamespace

import pytest

from intelligence.research import DependencyEdge, analyze_period, impact_candidates, reassess


def edge(edge_id, subject, obj, **overrides):
    values=dict(edge_id=edge_id,subject=subject,predicate="depends_on",object=obj,status="confirmed",
        evidence_ref="evidence:"+edge_id,evidence_basis="contract")
    values.update(overrides)
    return DependencyEdge(**values)


def test_impact_paths_need_two_evidence_backed_hops_and_event_entity_match():
    package=SimpleNamespace(event_id="evt-1",revision=2,entities=["Северная область"],claims=[])
    edges=[edge("e1","company-1","Supplier LLC"),edge("e2","Supplier LLC","Северная область"),
           edge("direct","company-1","Северная область"),
           edge("payment","company-1","Unknown Supplier",evidence_basis="payment_observation")]
    paths=impact_candidates(package,"company-1",edges,as_of="2026-10-24T10:00:00+03:00")
    assert len(paths)==1
    assert paths[0].edge_ids==["e1","e2"]
    assert paths[0].hop_count==2
    assert paths[0].causal_claim is False


def test_dependency_edges_respect_validity_interval():
    package=SimpleNamespace(event_id="evt-1",revision=1,entities=["Target"],claims=[])
    edges=[edge("e1","company","mid",valid_to="2026-01-01T00:00:00Z"),edge("e2","mid","Target")]
    assert impact_candidates(package,"company",edges,as_of="2026-10-24T00:00:00Z")==[]


def observations():
    return [
      dict(observation_id="inventory",metric="inventory",observed_at="2026-10-10T00:00:00Z",available_at="2026-10-11T00:00:00Z",value="100",unit="tonne",source_ref="doc:1",revision=1),
      dict(observation_id="inventory",metric="inventory",observed_at="2026-10-10T00:00:00Z",available_at="2026-10-20T00:00:00Z",value="120",unit="tonne",source_ref="doc:2",revision=2),
      dict(observation_id="sales",metric="sales",observed_at="2026-10-20T00:00:00Z",available_at="2026-10-21T00:00:00Z",value="50",unit="order",source_ref="doc:3",revision=1),
    ]


def test_period_analysis_distinguishes_today_retrospective_from_as_known_snapshot():
    common=dict(period_start="2026-10-01T00:00:00Z",period_end="2026-10-31T23:59:59Z")
    as_known=analyze_period(observations(),**common,known_at="2026-10-15T00:00:00Z",mode="as_known")
    retrospective=analyze_period(observations(),**common,known_at="2026-10-15T00:00:00Z",mode="retrospective")
    assert as_known.metrics[0].first_value==100
    assert retrospective.metrics[0].first_value==120
    assert as_known.coverage["excluded_after_known_at"]==2
    assert "no causal attribution" in as_known.statement


def test_period_observations_reject_naive_dates_and_future_observations():
    values=observations()+[dict(observation_id="future",metric="future",observed_at="2099-01-01T00:00:00Z",available_at="2099-01-01T00:00:00Z",value=1,unit="x",source_ref="x")]
    result=analyze_period(values,period_start="2026-10-01T00:00:00Z",period_end="2099-12-31T00:00:00Z",
        known_at="2026-10-24T00:00:00Z",mode="retrospective")
    assert all(metric.metric!="future" for metric in result.metrics)
    bad=observations()
    bad[0]={**bad[0],"observed_at":"2026-10-10T00:00:00"}
    with pytest.raises(ValueError):
        analyze_period(bad,period_start="2026-10-01T00:00:00Z",period_end="2026-10-31T00:00:00Z",
            known_at="2026-10-24T00:00:00Z")


def test_reassessment_avoids_duplicate_publication_without_relevant_change():
    initial={"event_revision_refs":["evt:1"],"evidence_refs":["doc:1"],"company_context_version":"c1",
             "source_policy_id":"policy","policy_version":"v1","last_checked_at":"yesterday"}
    same={**initial,"last_checked_at":"now"}
    unchanged=reassess(initial,same)
    assert unchanged.status=="no_change"
    assert unchanged.publication_revision_required is False
    changed=reassess(initial,{**same,"evidence_refs":["doc:1","doc:2"]})
    assert changed.status=="changed"
    assert changed.publication_revision_required is True
    assert changed.changes[0]["field"]=="evidence_refs"
