"""Integration proof for tournament publication through the real DataWorkflow.

Every model, judge, case and reviewer decision in this test is a test double.
The test proves routing and rollback behavior only; it is not a quality result.
"""
from datetime import datetime, timezone

from intelligence.analysts import AnalystConfig, PromptRegistry
from intelligence.collection import NewsCollector, SourcePolicySnapshot
from intelligence.schemas import CompanyContext
from intelligence.workflow import BusinessSnapshot, DataTask, DataWorkflow
from test_business_scenario import FixtureFetcher, MAIN_FEED, policy, source
from test_tournament_audit import approve, dataset, runner, shadow_dataset


AS_OF = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


def workflow_task(request_id, context_version, *, operation="onboard", source_policy=None, synthetic=False):
    tenant_id = "workflow-fixture-tenant"
    business = BusinessSnapshot(
        company_id="private-company",
        context_version=context_version,
        company=CompanyContext(
            name="Private fixture company",
            industry="manufacturing",
            role="procurement lead",
        ),
    )
    source_policy = source_policy or SourcePolicySnapshot(
        policy_id="workflow-fixture-policy", revision=1, tenant_id=tenant_id, issued_at=AS_OF)
    return DataTask(
        request_id=request_id,
        tenant_id=tenant_id,
        company_id=business.company_id,
        context_version=context_version,
        source_policy_version=source_policy.revision,
        operation=operation,
        as_of=AS_OF,
        source_policy=source_policy,
        businesses=[business],
        data_classification="synthetic" if synthetic else "public",
        synthetic=synthetic,
    )


def test_fake_tournament_publication_rebases_company_workflow_and_rollback(tmp_path):
    registry = PromptRegistry(tmp_path / "operator-registry.sqlite")
    candidate = AnalystConfig(
        config_id="workflow-candidate",
        role_scope="any",
        focus=["growth"],
    )
    registry.register(candidate)
    workflow = DataWorkflow(tmp_path / "workflow-state", analyst_registry=registry, clock=lambda: AS_OF)

    # Capture the pre-publication config in the private company store.
    before = workflow.run(workflow_task("onboard-before", 1))
    assert before.configurations[0]["source_config_ref"] == "baseline@1"

    # Populate the local corpus and event index before the operator publishes.
    external_policy = policy(
        [source("workflow-news", "news-a.example")], tenant="workflow-fixture-tenant")
    workflow.collector = NewsCollector(
        workflow.store,
        state_path=tmp_path / "workflow-poll-seed.sqlite",
        fetcher=FixtureFetcher([MAIN_FEED]),
        raw_dir=tmp_path / "workflow-state" / "raw" / "seed",
    )
    seeded = workflow.run(workflow_task(
        "fixture-ingest", 1, operation="collect", source_policy=external_policy, synthetic=True))
    assert seeded.collection["fetched"] == 1

    # This uses the established scripted judge/model path. Human approval is
    # intentionally a deterministic fixture in test_tournament_audit.approve.
    arena = runner(tmp_path, registry)
    report = arena.run(
        run_id="workflow-integration-run",
        dataset=dataset(),
        shadow_dataset=shadow_dataset(),
        candidate_refs=["baseline@1", candidate.ref],
        mode="model",
        execution_identity={"model": "fake-model"},
        target_scope="impact:any:*",
    )
    assert report["winner_ref"] == candidate.ref
    reviewed = approve(arena, "workflow-integration-run")
    assert reviewed["promotion_eligible"] is True
    registry.promote(
        scope="impact:any:*",
        ref=candidate.ref,
        report=reviewed,
        reviewer="alice",
    )

    after_publish = workflow.run(workflow_task(
        "analysis-after-publish", 2, operation="analyze", source_policy=external_policy, synthetic=True))
    published_config = after_publish.configurations[0]
    assert published_config["generation_status"] == "registry_context_rebased"
    assert published_config["source_config_ref"] == candidate.ref
    assert published_config["config"]["focus"] == ["growth"]
    assert after_publish.candidates, after_publish.model_dump(mode="json")
    for assessment in after_publish.candidates:
        assert assessment.versions.analyst_config == published_config["config_ref"]
        assert any(item["stage"] == "research_plan" and item["query_lengths"]["external"] > 0
                   for item in assessment.trace)

    registry.rollback(scope="impact:any:*", reviewer="fixture-operator", expected_revision=1)
    after_rollback = workflow.run(workflow_task(
        "analysis-after-rollback", 3, operation="analyze", source_policy=external_policy, synthetic=True))
    baseline_config = after_rollback.configurations[0]
    assert baseline_config["generation_status"] == "registry_context_rebased"
    assert baseline_config["source_config_ref"] == "baseline@1"
    assert baseline_config["config"]["focus"] == registry.get("baseline@1").focus
    assert after_rollback.candidates
    for assessment in after_rollback.candidates:
        assert assessment.versions.analyst_config == baseline_config["config_ref"]
        assert any(item["stage"] == "research_plan" and item["query_lengths"]["external"] > 0
                   for item in assessment.trace)
