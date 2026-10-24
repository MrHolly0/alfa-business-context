from types import SimpleNamespace

import pytest

from intelligence.research import DependencyEdge, impact_candidates, select_affected_businesses
from intelligence.schemas import CompanyContext, CompanyFact, ProvenancedDependency


AS_OF = "2026-10-24T12:00:00Z"
PACKAGE = SimpleNamespace(event_id="evt-1", revision=3,
    entities=["Северный порт"], territories=["Северная область"], resources=["дизельное топливо"], claims=[])


def graph_edge(edge_id, subject, target, **extra):
    values = {"edge_id": edge_id, "subject": subject, "predicate": "depends_on", "object": target,
        "status": "confirmed", "evidence_ref": f"contract:{edge_id}", "evidence_basis": "contract"}
    values.update(extra)
    return DependencyEdge(**values)


def business(company_id, *, industry="торговля", facts=None, dependencies=None, graph=None):
    return SimpleNamespace(company_id=company_id,
        company=CompanyContext(name=company_id, industry=industry, facts=facts or [], dependencies=dependencies or []),
        dependency_graph=graph or [])


def test_rank_prefers_direct_and_two_hop_evidence_graph_over_no_match():
    direct = business("co-direct", graph=[graph_edge("direct", "co-direct", "Северного порта")])
    two_hop = business("co-indirect", graph=[graph_edge("route", "co-indirect", "Транспортный подрядчик"),
        graph_edge("terminal", "Транспортный подрядчик", "Северный порт", status="user_reported",
            evidence_basis="user_statement", evidence_ref="interview:7")])
    unrelated = business("co-unrelated", industry="другой бизнес")
    selected = select_affected_businesses(PACKAGE, [unrelated, two_hop, direct], as_of=AS_OF)
    assert [item.company_id for item in selected] == ["co-direct", "co-indirect", "co-unrelated"]
    assert selected[0].score == 1.0
    assert selected[0].paths[0].hop_count == 1
    assert selected[0].paths[0].target == "Северный порт"
    assert selected[1].paths[0].hop_count == 2
    assert selected[1].paths[0].statuses == ["confirmed", "user_reported"]
    assert selected[1].score < selected[0].score
    assert selected[2].score == 0
    assert selected[2].status == "no_signal_found"
    assert selected[2].uncertainty and "does not establish absence" in selected[2].uncertainty[0]


def test_context_facts_and_provenanced_dependencies_are_used_with_weak_morphology():
    dep = ProvenancedDependency(dependency_id="dep-fuel", label="Закупки дизельного топлива",
        kind="resource", status="confirmed", provenance="purchase contract p4",
        evidence_refs=["contract:p4"])
    fact = CompanyFact(fact_id="fact-port", text="Регулярные рейсы через Северного порта",
        provenance="route registry:p9")
    selected = select_affected_businesses(PACKAGE,
        [business("co-context", dependencies=[dep], facts=[fact])], as_of=AS_OF)
    row = selected[0]
    assert row.score > 0.4
    assert set(row.matched_nodes) == {"дизельное топливо", "Северный порт"}
    assert "company_dependency_text_matches_event_term" in row.reasons
    assert "company_fact_text_matches_event_term" in row.reasons
    assert row.coverage["confirmed_company_dependencies"] == 1
    assert row.paths[0].evidence_refs == ["contract:p4"]


def test_inactive_and_payment_only_edges_do_not_create_paths():
    edges = [graph_edge("expired", "co-risk", "Северный порт", valid_to="2026-01-01T00:00:00Z"),
        graph_edge("payment", "co-risk", "Северный порт", evidence_basis="payment_observation")]
    [row] = select_affected_businesses(PACKAGE, [business("co-risk", graph=edges)], as_of=AS_OF)
    assert row.score == 0
    assert row.status == "no_signal_found"
    assert row.paths == []
    assert row.coverage["inactive_graph_edges"] == 1
    assert row.coverage["payment_graph_edges_excluded"] == 1
    assert any("validity interval" in item for item in row.uncertainty)
    assert any("Payment observations" in item for item in row.uncertainty)


def test_industry_overlap_is_low_confidence_and_not_a_dependency_path():
    package = SimpleNamespace(event_id="evt-1", revision=3, entities=[], territories=[], resources=["логистика"], claims=[])
    [row] = select_affected_businesses(package,
        [business("co-logistics", industry="логистикой")], as_of=AS_OF)
    assert row.score == 0.08
    assert row.paths == []
    assert "industry_term_only_weak_signal" in row.reasons


def test_existing_grounded_path_api_still_requires_two_hops():
    edges = [graph_edge("direct", "co-direct", "Северный порт")]
    [selection] = select_affected_businesses(PACKAGE, [business("co-direct", graph=edges)], as_of=AS_OF)
    assert selection.paths[0].hop_count == 1
    assert impact_candidates(PACKAGE, "co-direct", edges, as_of=AS_OF) == []
    with pytest.raises(ValueError, match="max_hops must be between 1 and 3"):
        select_affected_businesses(PACKAGE, [business("co-direct")], as_of=AS_OF, max_hops=4)


def test_no_package_terms_returns_coverage_and_never_claims_safe_exclusion():
    package = SimpleNamespace(event_id="evt-empty", revision=1, entities=[], resources=[], territories=[], claims=[])
    [row] = select_affected_businesses(package, [business("co-1")], as_of=AS_OF)
    assert row.score == 0
    assert row.coverage["event_term_count"] == 0
    assert any("no explicit entities" in item for item in row.uncertainty)
