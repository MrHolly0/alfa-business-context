"""A new synthetic business scenario through collection, event and baseline analysis.

No model/network access: external materials are supplied by a deterministic
fetcher. Semantic expectations describe what evidence supports, not model quality.
"""
from datetime import datetime, timezone
import json
from pathlib import Path

from intelligence.collection import FetchResult, NewsCollector, SourceGrant, SourcePolicySnapshot
from intelligence.workflow import BusinessSnapshot, DataTask, DataWorkflow


NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
MAIN_FEED = '''<?xml version="1.0"?><rss version="2.0"><channel><title>Fixture A</title>
<item><title>Закрыт экспортный терминал Северного причала</title>
<description>Издание сообщает, что терминал Северного причала временно закрылся после сбоя оборудования. Отгрузка керамических изоляторов задерживается; дата возобновления неизвестна.</description>
<pubDate>Sun, 25 Oct 2026 09:00:00 +0000</pubDate><link>https://news-a.example/terminal-closure</link></item>
<item><title>На другом маршруте завод остановил выпуск после задержки поставок</title>
<description>В отдельном случае задержка оборудования предшествовала остановке завода. Компания, маршрут, причины и сопоставимость материалов не установлены.</description>
<pubDate>Sun, 25 Oct 2026 08:00:00 +0000</pubDate><link>https://news-a.example/weak-analogy</link></item>
</channel></rss>'''.encode("utf-8")
COUNTER_FEED = '''<?xml version="1.0"?><rss version="2.0"><channel><title>Fixture B</title>
<item><title>Закрыт экспортный терминал Северного причала</title>
<description>Оператор терминала сообщает, что грузовой терминал открыт и работает штатно. Ограничений отгрузки керамических изоляторов оператор не подтверждает.</description>
<pubDate>Sun, 25 Oct 2026 10:00:00 +0000</pubDate><link>https://news-b.example/terminal-status</link></item>
</channel></rss>'''.encode("utf-8")


class FixtureFetcher:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = []

    def __call__(self, url, headers, **kwargs):
        self.calls.append(url)
        return FetchResult(status=200, headers={"content-type": "application/rss+xml"}, body=self.bodies.pop(0))


def source(source_id, host):
    return SourceGrant(source_id=source_id, source_role="external", data_classification="synthetic",
        uri=f"https://{host}/feed.xml", content_type="application/rss+xml", title=f"Fixture {source_id}",
        publisher=f"Учебное издание {source_id}", language="ru", enabled=True,
        poll_interval_seconds=3600, timeout_seconds=5, max_bytes=100_000,
        fetch_articles=False, article_hosts=(host,))


def policy(sources, *, revision=1, excluded=(), tenant="scenario-tenant"):
    return SourcePolicySnapshot(policy_id="business-scenario-policy", revision=revision,
        tenant_id=tenant, issued_at=NOW, sources=tuple(sources), excluded_source_ids=tuple(excluded))


def businesses():
    return [
        BusinessSnapshot(company_id="sensorworks", context_version=1, company={
            "name": "Датчик и код (синтетический пример)", "industry": "производство датчиков",
            "role": "директор по операциям",
            "activities": [
                {"activity_id": "sensorworks-activity-1", "label": "Производство промышленных датчиков", "normalized_class": "manufacturing", "provenance": "scenario:profile:activity-1"},
                {"activity_id": "sensorworks-activity-2", "label": "Разработка встроенного программного обеспечения", "normalized_class": "software", "provenance": "scenario:profile:activity-2"},
                {"activity_id": "sensorworks-activity-3", "label": "Импорт керамических изоляторов", "normalized_class": "import", "provenance": "scenario:profile:activity-3"},
            ],
            "assertions": [
                {"assertion_id": "sensorworks-assertion-product", "kind": "product", "label": "Промышленные датчики", "status": "confirmed", "provenance": "scenario:profile:product"},
                {"assertion_id": "sensorworks-assertion-resource", "kind": "resource", "label": "Керамические изоляторы используются в части датчиков; доля и критичность неизвестны", "status": "confirmed", "provenance": "scenario:profile:resource"},
                {"assertion_id": "sensorworks-assertion-supplier", "kind": "supplier", "label": "Поставщик и конкретный терминал поставок неизвестны", "status": "unknown", "provenance": "scenario:profile:unknown"},
            ],
            "facts": [{"fact_id": "sensorworks-unknown-exposure", "text": "Нет подтверждённых данных о доле импорта, запасах или сроке покрытия", "provenance": "scenario:profile:known-gap"}],
        }),
        BusinessSnapshot(company_id="softnorth", context_version=1, company={
            "name": "СофтСевер (синтетический пример)", "industry": "разработка ПО", "role": "генеральный директор",
            "activities": [{"activity_id": "softnorth-activity-1", "label": "Разработка корпоративного ПО", "normalized_class": "software", "provenance": "scenario:profile:activity"}],
            "facts": [{"fact_id": "softnorth-no-ceramic", "text": "В учебном профиле сведения о керамике, терминале и поставщике отсутствуют; отсутствие сведений не означает отсутствия связей.", "provenance": "scenario:profile:known-gap"}],
        }),
    ]


def task(policy_snapshot, *, request_id="scenario-run", operation="collect", tenant="scenario-tenant"):
    companies = businesses()
    return DataTask(request_id=request_id, tenant_id=tenant, company_id="sensorworks", context_version=1,
        source_policy_version=policy_snapshot.revision, operation=operation, trigger="manual",
        knowledge_mode="as_of", as_of=NOW, source_policy=policy_snapshot,
        businesses=companies, data_classification="synthetic", synthetic=True,
        budget={"max_model_calls": 0})


def new_workflow(tmp_path, bodies):
    fetcher = FixtureFetcher(bodies)
    instance = DataWorkflow(tmp_path, clock=lambda: NOW)
    instance.collector = NewsCollector(instance.store, state_path=tmp_path / "poll.sqlite3",
        fetcher=fetcher, raw_dir=tmp_path / "raw")
    return instance, fetcher


def test_multi_activity_news_conflict_analogy_and_baseline_are_scoped(tmp_path):
    sources = [source("wire-a", "news-a.example"), source("wire-b", "news-b.example")]
    allowed = policy(sources)
    instance, fetcher = new_workflow(tmp_path, [MAIN_FEED, COUNTER_FEED])
    result = instance.run(task(allowed))

    assert len(fetcher.calls) == 2
    assert result.collection["fetched"] == 2
    assert len(result.events) == 2
    assert {candidate.company_id for candidate in result.candidates} == {"sensorworks", "softnorth"}
    assert all(candidate.execution_mode == "baseline" for candidate in result.candidates)
    selection_by_pair = {(item.coverage["package_id"], item.company_id): item for item in result.selections}
    closure_packages = [item for item in result.events if any("терминал" in source["title"].casefold()
        for source in item["sources"])]
    assert closure_packages
    for package in closure_packages:
        assert selection_by_pair[(package["package_id"], "sensorworks")].status == "signal_found"
        assert selection_by_pair[(package["package_id"], "softnorth")].status == "no_signal_found"
    analogy_package = next(item for item in result.events if any("другом маршруте" in source["title"].casefold()
        for source in item["sources"]))
    assert selection_by_pair[(analogy_package["package_id"], "sensorworks")].status == "no_signal_found"
    assert selection_by_pair[(analogy_package["package_id"], "softnorth")].status == "no_signal_found"
    assert all(candidate.tenant_id == "scenario-tenant" for candidate in result.candidates)
    assert all(candidate.status in {"partial", "unknown", "ready"} for candidate in result.candidates)

    activities = result.configurations[0]["config"]
    assert activities  # An onboarded config exists even when no LLM is configured.
    assert [item.label for item in businesses()[0].company.activities] == [
        "Производство промышленных датчиков", "Разработка встроенного программного обеспечения", "Импорт керамических изоляторов"
    ]
    unknown_class = {"activity_id": "unknown", "label": "Изготовление астрономических приборов", "provenance": "fixture"}
    from intelligence.schemas import CompanyActivity
    assert CompanyActivity.model_validate(unknown_class).normalized_class is None

    closure_events = [event for event in result.events if any("терминал" in item["title"].casefold() for item in event["sources"])]
    closure_sources = {item["source_id"]: " ".join(item["title"] + " " + claim["quote"]
        for claim in event["claims"] for item in event["sources"] if claim["source_id"] == item["source_id"])
        for event in closure_events for item in event["sources"]}
    assert "wire-a" in closure_sources and "wire-b" in closure_sources
    assert "временно закрылся" in closure_sources["wire-a"]
    assert "работает штатно" in closure_sources["wire-b"]
    assert all(event["coverage"]["independence_status"] == "unknown" for event in closure_events)
    assert all(candidate.execution_mode == "baseline" for candidate in result.candidates)
    assert all(not (claim.kind == "bank_view") for candidate in result.candidates for claim in candidate.claims)

    # The independent story is a retrieval signal at most; no company-specific
    # causal pathway or measured loss can be asserted from this analogy.
    analogy_event = next(event for event in result.events if any("другом маршруте" in item["title"].casefold() for item in event["sources"]))
    analogy_candidates = [candidate for candidate in result.candidates if candidate.versions.event_id == analogy_event["event_id"]]
    for candidate in analogy_candidates:
        assert not any(edge.causal_claim for edge in candidate.influence_chain)
        assert not any("процент" in claim.text.casefold() or "долю импорта" in claim.text.casefold() for claim in candidate.claims)

    # Re-run from the same cached corpus under a stricter policy. The excluded
    # contradictory source must not reappear in any package or candidate.
    restricted = policy(sources, revision=2, excluded=("wire-b",))
    restricted_result = instance.run(task(restricted, request_id="scenario-source-excluded", operation="analyze"))
    assert "wire-b" not in {item["source_id"] for event in restricted_result.events for item in event["sources"]}
    assert all("wire-b" not in {item.source_id for item in candidate.evidence} for candidate in restricted_result.candidates)

    # Tenant-scoped storage cannot expose this corpus to a different tenant.
    isolated = policy(sources, revision=1, tenant="other-tenant")
    tenant_result = instance.run(task(isolated, request_id="scenario-other-tenant", operation="analyze", tenant="other-tenant"))
    assert tenant_result.events == []
    assert tenant_result.candidates == []


def test_cli_example_and_evaluation_fixture_are_well_formed():
    root = Path(__file__).parents[1]
    example = json.loads((root / "examples/business-scenario.json").read_text(encoding="utf-8"))
    evaluation = json.loads((root / "evaluation/business-scenario.json").read_text(encoding="utf-8"))
    parsed = DataTask.model_validate(example)
    assert parsed.operation == "analyze"
    assert parsed.budget.max_model_calls == 0
    assert len(parsed.businesses[0].company.activities) == 3
    assert evaluation["classification"] == "synthetic_fixture"
    assert any("доля импорта" in item for item in evaluation["forbidden_assertions"])
