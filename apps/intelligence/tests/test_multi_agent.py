import json

from intelligence.multi_agent import MultiAgentPipeline
from intelligence.pipeline import AnalysisPipeline
from test_pipeline import req, store
from intelligence.llm.types import Usage


class Role:
    def __init__(self, role, responder):
        self.role_name, self.responder, self.calls = role, responder, []
    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"content": json.dumps(self.responder(messages), ensure_ascii=False), "model": self.role_name,
                "response_id": f"{self.role_name}-{len(self.calls)}",
                "usage": Usage(prompt_tokens=40, completion_tokens=20), "cost": 0, "cost_currency": "USD"}


def roles(corpus, *, conflict=False):
    def research(_):
        return {"searches": [{"role": "external", "query": "порт шторм"},
                             {"role": "bank", "query": "аналитика логистика порт"}], "missing_context": []}
    def analyst(messages):
        payload = json.loads(messages[-1]["content"].rsplit("\n", 1)[-1])
        evidence = payload["RETRIEVED_EVIDENCE"]
        primary = next(e for e in evidence if e["source_role"] == "external")
        bank = next(e for e in evidence if e["source_role"] == "bank")
        return {"summary": "Закрытие может повлиять на маршрут; нужна проверка.", "clarifications": [],
                "claims": [{"claim_id": "impact", "kind": "model_hypothesis",
                    "text": "Задержка может повлиять на срок доставки.", "evidence_refs": [primary["evidence_id"]],
                    "context_refs": ["dep-port"], "assumptions": ["Маршрут проходит через порт."],
                    "confirmation_conditions": ["Фактический срок доставки по маршруту вырос."],
                    "alternatives": ["Есть обходной маршрут."], "falsifiers": ["Маршрут не использует порт."]},
                    {"claim_id": "bank", "kind": "bank_view", "text": bank["quote"],
                     "evidence_refs": [bank["evidence_id"]], "context_refs": [], "origin": "bank"}],
                "influence_chain": []}
    def critic(messages):
        payload = json.loads(messages[-1]["content"])
        if conflict:
            ev = next(e for e in payload["evidence"] if e["source_role"] == "external")
            return {"decision": "review", "issues": [{"target_id": "impact", "reason": "Цитата отрицает задержку.",
                                                          "evidence_refs": [ev["evidence_id"]]}], "clarifications": ["Сверьте дату."]}
        return {"decision": "accept", "issues": [], "clarifications": []}
    return {"researcher": Role("researcher", research), "analyst": Role("analyst", analyst),
            "critic": Role("critic", critic)}


def test_three_roles_share_one_session_and_planner_queries_drive_search(tmp_path, monkeypatch):
    corpus = store(tmp_path)
    role_services = roles(corpus)
    queries = []
    original = corpus.search
    def capture(**kwargs):
        queries.append(kwargs["query"])
        return original(**kwargs)
    monkeypatch.setattr(corpus, "search", capture)
    result = MultiAgentPipeline(corpus, role_services=role_services).run(req(budget={"max_model_calls": 3}))
    assert [len(role_services[name].calls) for name in ("researcher", "analyst", "critic")] == [1, 1, 1]
    assert "порт шторм" in queries
    assert result.usage["model_calls"] == 3
    assert result.usage["total_tokens"] == 180
    assert result.status == "ready"


def test_analyst_profile_expansions_are_bounded_bank_search_terms(tmp_path, monkeypatch):
    from intelligence.analysts import AnalystConfig
    corpus = store(tmp_path)
    role_services = roles(corpus)
    queries = []
    original = corpus.search
    def capture(**kwargs):
        queries.append((kwargs["role"], kwargs["query"]))
        return original(**kwargs)
    monkeypatch.setattr(corpus, "search", capture)
    config = AnalystConfig(config_id="bounded-test", retrieval_expansions=["резервный маршрут"])
    MultiAgentPipeline(corpus, analyst_config=config, role_services=role_services).run(
        req(budget={"max_model_calls": 3}))
    bank_queries = [query for role, query in queries if role == "bank"]
    assert any("аналитика логистика порт" in query for query in bank_queries)
    assert any("резервный маршрут" in query for query in bank_queries)
    assert all(len(query) <= 320 for query in bank_queries)


def test_selected_config_and_projected_multiactivity_reach_research_stage(tmp_path):
    from intelligence.analysts import AnalystConfig
    corpus = store(tmp_path)
    role_services = roles(corpus)
    config = AnalystConfig(config_id="procurement-mix", focus=["supply", "costs"],
        retrieval_strategy="dependencies_first", retrieval_expansions=["резервные комплектующие"], max_depth=3,
        examples=[{"scenario": "supply_disruption", "focus": ["supply"], "check_kind": "alternative_supplier"}])
    request = req(company={"name": "СеверТранс", "industry": "", "role": "операционный директор",
        "activities": [
            {"activity_id": "a-production", "label": "производство узлов", "normalized_class": "manufacturing", "provenance": "profile:1"},
            {"activity_id": "a-import", "label": "импорт комплектующих", "normalized_class": "import", "provenance": "profile:2"}],
        "dependencies": [], "facts": []}, budget={"max_model_calls": 3})
    MultiAgentPipeline(corpus, analyst_config=config, role_services=role_services).run(request)
    research_payload = json.loads(role_services["researcher"].calls[0][0][-1]["content"])
    assert research_payload["analyst_preferences"]["retrieval_strategy"] == "dependencies_first"
    assert research_payload["analyst_preferences"]["retrieval_expansions"] == ["резервные комплектующие"]
    assert research_payload["analyst_preferences"]["max_depth"] == 3
    assert research_payload["analyst_preferences"]["examples"][0]["scenario"] == "supply_disruption"
    assert {item["activity_id"] for item in research_payload["company"]["projection"]["activities"]} == {"a-production", "a-import"}


def test_critic_contradiction_is_advisory_and_downgrades_ready(tmp_path):
    corpus = store(tmp_path)
    result = MultiAgentPipeline(corpus, role_services=roles(corpus, conflict=True)).run(req(budget={"max_model_calls": 3}))
    assert result.status == "partial"
    assert result.usage["critic_review"]["issues"][0]["target_id"] == "impact"
    assert any("Сверьте дату" in text for text in result.clarifications)


def test_budget_caps_aggregate_calls_and_does_not_claim_complete_cycle(tmp_path):
    corpus = store(tmp_path)
    role_services = roles(corpus)
    request = req(budget={"max_model_calls": 2, "max_steps": 12})
    result = MultiAgentPipeline(corpus, role_services=role_services).run(request)
    assert result.usage["model_calls"] <= 2
    assert not role_services["critic"].calls
    assert result.status != "ready"
    assert any(t["stage"] == "critic_unavailable" for t in result.trace)


def test_malformed_research_plan_does_not_return_full_success(tmp_path):
    corpus = store(tmp_path)
    role_services = roles(corpus)
    role_services["researcher"] = Role("researcher", lambda _: {"searches": [{"role": "sql", "query": "drop"}]})
    result = MultiAgentPipeline(corpus, role_services=role_services).run(req())
    assert result.status != "ready"
    assert any(t["stage"] == "research_unavailable" for t in result.trace)


def test_shared_package_is_scope_checked_and_only_its_reference_is_returned(tmp_path):
    corpus = store(tmp_path)
    role_services = roles(corpus)
    package = {"package_id": "pkg-example", "tenant_id": "tenant-a", "event_id": "event-1", "revision": 1,
        "claims": [], "sources": [], "entities": [], "territories": [], "resources": [],
        "possible_mechanisms": [], "conditions": [], "alternatives": [], "unknowns": [],
        "coverage": {"permitted_publications": 0, "independent_origin_groups": 0,
            "policy_source_count": 2, "policy_id": "policy-a", "policy_version": "2",
            "originals_available": False, "extraction_mode": "deterministic", "limitations": []},
        "model_version": "fixture", "prompt_version": "event-claims-v1", "policy_version": "2",
        "created_at": "2026-10-24T00:00:00Z"}
    result = MultiAgentPipeline(corpus, role_services=role_services).run(
        req(budget={"max_model_calls": 3}), shared_event_package=package)
    assert result.shared_package_ref == "pkg-example"
    sent = "\n".join(message["content"] for service in role_services.values()
                      for call, _ in service.calls for message in call)
    assert "pkg-example" not in sent
    package["tenant_id"] = "tenant-other"
    rejected = MultiAgentPipeline(corpus, role_services=roles(corpus)).run(
        req(budget={"max_model_calls": 3}), shared_event_package=package)
    assert rejected.shared_package_ref is None
    assert rejected.usage["shared_package_error"] == "ValueError"
    assert "shared_event_package_rejected" in rejected.coverage.limitations


def test_tight_total_token_budget_stops_all_role_calls(tmp_path):
    corpus = store(tmp_path)
    role_services = roles(corpus)
    request = req(budget={"max_model_calls": 3, "max_total_tokens": 16})
    result = MultiAgentPipeline(corpus, role_services=role_services).run(request)
    assert result.usage["model_calls"] == 0
    assert result.usage["reserved_tokens"] == 0
    assert result.status != "ready"
    assert not any(service.calls for service in role_services.values())


def test_shared_step_budget_stops_before_later_role_calls(tmp_path):
    corpus = store(tmp_path)
    role_services = roles(corpus)
    result = MultiAgentPipeline(corpus, role_services=role_services).run(
        req(budget={"max_model_calls": 3, "max_steps": 4}))
    assert result.status != "ready"
    assert result.usage["model_calls"] <= 2
    assert not role_services["critic"].calls


def test_lease_checkpoint_exception_propagates_without_starting_analysis(tmp_path):
    corpus = store(tmp_path)
    role_services = roles(corpus)
    class LostLease(RuntimeError): pass
    def checkpoint(stage):
        if stage == "before_research":
            raise LostLease("lease is gone")
    try:
        MultiAgentPipeline(corpus, role_services=role_services).run(
            req(budget={"max_model_calls": 3}), checkpoint=checkpoint)
    except LostLease:
        pass
    else:
        raise AssertionError("lease loss must abort the model workflow")
    assert not any(service.calls for service in role_services.values())


def test_expired_dependency_cannot_support_impact_hypothesis(tmp_path):
    corpus = store(tmp_path)
    role_services = roles(corpus)
    request = req(budget={"max_model_calls": 3}, company={
        "name": "СеверТранс", "industry": "логистика", "description": "Перевозки",
        "role": "операционный директор",
        "dependencies": [{"dependency_id": "dep-port", "label": "маршрут через порт", "kind": "route",
                          "status": "confirmed", "provenance": "анкета:4",
                          "valid_until": "2026-10-23T23:59:59Z"}],
        "facts": [{"fact_id": "fact-fleet", "text": "Есть автопарк", "provenance": "анкета:5"}]})
    result = MultiAgentPipeline(corpus, role_services=role_services).run(request)
    assert result.status == "partial"
    assert not any(c.kind == "model_hypothesis" for c in result.claims)


def test_researcher_missing_context_becomes_a_specific_follow_up(tmp_path):
    corpus = store(tmp_path)
    role_services = roles(corpus)
    role_services["researcher"] = Role("researcher", lambda _: {
        "searches": [{"role": "external", "query": "порт шторм"}],
        "missing_context": ["какая доля перевозок идёт через этот порт"]})
    result = MultiAgentPipeline(corpus, role_services=role_services).run(
        req(budget={"max_model_calls": 3}))
    assert any("Уточните: какая доля перевозок идёт через этот порт" in text
               for text in result.clarifications)


def test_critic_search_retrieves_new_scoped_evidence_and_runs_one_reanalysis(tmp_path):
    corpus = store(tmp_path)
    corpus.ingest({"tenant_id": "tenant-a", "source_id": "news", "source_role": "external",
        "canonical_uri": "https://example.invalid/news/navigation-restored",
        "title": "Ограничения движения судов сняты",
        "content": "Сняты ограничения движения судов; регулярные рейсы по фарватеру возобновлены.",
        "content_type": "text/plain", "published_at": "2026-10-24T10:30:00Z",
        "fetched_at": "2026-10-24T11:00:00Z", "synthetic": True})

    def analyst(messages):
        payload = json.loads(messages[-1]["content"].rsplit("\n", 1)[-1])
        evidence = payload["RETRIEVED_EVIDENCE"]
        # Make the second analysis respond to the new corpus item, based on the
        # actual evidence JSON supplied by AnalysisPipeline.
        fresh = next((item for item in evidence if "Сняты ограничения" in item["quote"]), None)
        external = fresh or next(item for item in evidence if item["source_role"] == "external")
        bank = next(item for item in evidence if item["source_role"] == "bank")
        return {"summary": "Есть сообщения о состоянии движения; влияние на компанию требует проверки.",
            "clarifications": [], "claims": [
                {"claim_id": "navigation", "kind": "external_fact", "text": external["quote"],
                 "evidence_refs": [external["evidence_id"]], "context_refs": [], "assumptions": [],
                 "alternatives": [], "falsifiers": []},
                {"claim_id": "bank", "kind": "bank_view", "text": bank["quote"],
                 "evidence_refs": [bank["evidence_id"]], "context_refs": [], "origin": "bank"}],
            "influence_chain": []}

    def critic(messages):
        payload = json.loads(messages[-1]["content"])
        evidence = next(item for item in payload["evidence"] if item["source_role"] == "external")
        claim = payload["claims"][0]
        return {"decision": "review", "issues": [{"target_id": claim["claim_id"],
            "reason": "Нужно проверить, не были ли ограничения уже сняты.",
            "evidence_refs": [evidence["evidence_id"]]}], "clarifications": [],
            "searches": [{"role": "external", "query": "сняты ограничения регулярные рейсы"}]}

    analyst_role, critic_role = Role("analyst", analyst), Role("critic", critic)
    role_services = {"researcher": None, "analyst": analyst_role, "critic": critic_role}
    searches = []
    original_search = corpus.search
    def capture_search(**kwargs):
        searches.append(kwargs)
        return original_search(**kwargs)
    corpus.search = capture_search
    checkpoints = []
    result = MultiAgentPipeline(corpus, role_services=role_services).run(
        req(budget={"max_model_calls": 3, "max_steps": 12}), checkpoint=checkpoints.append)

    assert len(analyst_role.calls) == 2
    assert len(critic_role.calls) == 1
    assert result.usage["model_calls"] == 3
    assert result.usage["reserved_tokens"] > 0
    assert any("сняты ограничения регулярные рейсы" in item["query"] for item in searches)
    assert all(item["tenant_id"] == "tenant-a" for item in searches)
    assert all(set(item["source_ids"]) <= {"news", "bank"} for item in searches)
    fresh = next(item for item in result.evidence if "Сняты ограничения" in item.quote)
    assert any(fresh.evidence_id in item[-1]["content"] for item, _ in analyst_role.calls[1:])
    assert result.usage["critic_review"]["adaptation"]["status"] == "complete_pending_recritique"
    assert result.status == "partial"
    assert "before_critic_adaptation" in checkpoints
    assert any(item["stage"] == "critic_adaptation_complete" for item in result.trace)


def test_critic_adaptation_checkpoint_cancellation_propagates(tmp_path):
    corpus = store(tmp_path)
    def analyst(messages):
        payload = json.loads(messages[-1]["content"].rsplit("\n", 1)[-1])
        evidence = payload["RETRIEVED_EVIDENCE"]
        external = next(item for item in evidence if item["source_role"] == "external")
        bank = next(item for item in evidence if item["source_role"] == "bank")
        return {"summary": "Нужна проверка.", "clarifications": [], "influence_chain": [], "claims": [
            {"claim_id": "closure", "kind": "external_fact", "text": external["quote"],
             "evidence_refs": [external["evidence_id"]], "context_refs": [], "assumptions": [],
             "alternatives": [], "falsifiers": []},
            {"claim_id": "bank", "kind": "bank_view", "text": bank["quote"],
             "evidence_refs": [bank["evidence_id"]], "context_refs": [], "origin": "bank"}]}
    def critic(messages):
        payload = json.loads(messages[-1]["content"])
        evidence = next(item for item in payload["evidence"] if item["source_role"] == "external")
        return {"decision": "review", "issues": [{"target_id": "closure", "reason": "Проверьте.",
            "evidence_refs": [evidence["evidence_id"]]}], "clarifications": [],
            "searches": [{"role": "external", "query": "сняты ограничения рейсы"}]}
    analyst_role, critic_role = Role("analyst", analyst), Role("critic", critic)
    class LeaseCancelled(RuntimeError):
        pass
    def checkpoint(stage):
        if stage == "before_critic_adaptation":
            raise LeaseCancelled("lease cancelled")
    try:
        MultiAgentPipeline(corpus, role_services={"researcher": None, "analyst": analyst_role,
            "critic": critic_role}).run(req(budget={"max_model_calls": 3, "max_steps": 12}), checkpoint=checkpoint)
    except LeaseCancelled:
        pass
    else:
        raise AssertionError("adaptation must propagate lease cancellation")
    assert len(analyst_role.calls) == 1
    assert len(critic_role.calls) == 1


def test_critic_company_projection_is_synthetic_only_by_default(tmp_path):
    corpus=store(tmp_path)
    candidate=MultiAgentPipeline(corpus).run(req())
    assert "synthetic_company" not in json.loads(MultiAgentPipeline._critic_messages(req(),candidate)[-1]["content"])
    configured=roles(corpus)
    MultiAgentPipeline(corpus,role_services=configured).run(req(budget={"max_model_calls":3}),data_classification="synthetic")
    messages=configured["critic"].calls[0][0]
    assert json.loads(messages[-1]["content"])["synthetic_company"]==AnalysisPipeline._context_payload(req())["company"]
    assert "отсутствующее звено" in messages[0]["content"]
