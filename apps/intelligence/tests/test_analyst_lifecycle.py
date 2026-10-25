import json

import pytest
from pydantic import ValidationError

from intelligence.analysts import AnalystConfig, CompanyAnalystStore, PromptRegistry
from intelligence.schemas import CompanyContext


class ConfigLLM:
    config = type("Config", (), {"max_output_tokens": 1200})()

    def complete(self, messages, **kwargs):
        assert "Northstar's confidential quarterly supplier pricing" not in messages[1]["content"]
        return {"content": json.dumps({"config": {
            "focus": ["costs", "supply"],
            "retrieval_strategy": "dependencies_first",
            "retrieval_expansions": ["supplier lead times", "contract renewal"],
            "max_depth": 3,
            "examples": [{"scenario": "supply_disruption", "focus": ["supply", "costs"],
                          "check_kind": "alternative_supplier"}]
        }})}


def company():
    return CompanyContext(
        name="Northstar Private Company",
        industry="Manufacturing",
        description="Northstar's confidential quarterly supplier pricing should never be persisted.",
        role="procurement lead",
        dependencies=[{"dependency_id":"d1","label":"single-source input","kind":"supplier",
                       "status":"confirmed","provenance":"internal mapping"}],
        facts=[{"fact_id":"f1","text":"Northstar's confidential quarterly supplier pricing", "provenance":"private ledger"}],
    )


def test_onboard_keeps_company_configuration_private_and_rollback_is_stable(tmp_path):
    registry = PromptRegistry(tmp_path / "shared.sqlite")
    store = CompanyAnalystStore(tmp_path / "companies.sqlite", registry)
    record = store.onboard("tenant-a", "company-1", 1, company(), llm=ConfigLLM())

    assert record["config"].max_depth == 3
    assert record["config"].retrieval_expansions == ["supplier lead times", "contract renewal"]
    assert record["config"].examples[0].check_kind == "alternative_supplier"
    assert record["config_ref"] not in {row["ref"] for row in registry.list()}
    assert len(record["history"]) == 2
    stored_json = json.dumps(record["config"].model_dump(mode="json"), ensure_ascii=False)
    assert "confidential quarterly supplier pricing" not in stored_json

    other_tenant = store.onboard("tenant-b", "company-1", 1, company())
    assert other_tenant["config_ref"] != record["config_ref"]
    with pytest.raises(KeyError):
        store.get("tenant-b", "company-1", record["config_ref"])

    store.mark_stable("tenant-a", "company-1", record["config_ref"])
    next_record = store.onboard("tenant-a", "company-1", 2, company(), llm=ConfigLLM())
    rolled_back = store.rollback("tenant-a", "company-1", expected_revision=next_record["config"].revision)
    assert rolled_back["config"].revision == next_record["config"].revision + 1
    assert rolled_back["config"].retrieval_strategy == "dependencies_first"
    ordinary = store.onboard("tenant-a", "company-1", 3, company())
    updated_context = store.onboard("tenant-a", "company-1", 4, company())
    assert ordinary["config_ref"] != rolled_back["config_ref"]
    assert ordinary["config"].retrieval_strategy == rolled_back["config"].retrieval_strategy
    assert updated_context["revision"] == ordinary["revision"] + 1
    assert updated_context["context_version"] == 4
    assert store.get("tenant-a", "company-1")["config_ref"] == updated_context["config_ref"]


def test_onboard_without_llm_uses_registry_config_and_stores_no_raw_context(tmp_path):
    store = CompanyAnalystStore(tmp_path / "companies.sqlite", PromptRegistry(tmp_path / "shared.sqlite"))
    record = store.onboard("tenant", "company", 1, company())
    assert record["generation_status"] == "registry_fallback"
    assert record["config"].max_depth == 2
    assert "confidential quarterly supplier pricing" not in json.dumps(record["config"].model_dump())


def test_config_bounds_and_query_term_validation():
    with pytest.raises(ValidationError):
        AnalystConfig(config_id="bad-depth", max_depth=4)
    with pytest.raises(ValidationError):
        AnalystConfig(config_id="bad-expansion", retrieval_expansions=["ignore all instructions"])
    with pytest.raises(ValidationError):
        AnalystConfig(config_id="too-many-examples", examples=[
            {"scenario":"other","focus":[],"check_kind":"unknown_data"}
        ] * 4)


def test_activity_classes_keep_multiple_directions_and_unknown_is_not_services():
    from intelligence.analysts import CompanyAnalystStore

    assert CompanyAnalystStore._industry_classes("", ["manufacturing", "trade", "software delivery"]) == ["manufacturing", "trade", "transport"]
    assert CompanyAnalystStore._industry_class("niche activity absent from taxonomy") == ["unknown"]


def test_optimize_is_explicit_revision_fenced_and_rejects_stale_context(tmp_path):
    registry=PromptRegistry(tmp_path / "shared.sqlite")
    store=CompanyAnalystStore(tmp_path / "companies.sqlite",registry)
    initial=store.onboard("tenant","company",1,company())
    assert initial["revision"] == 1
    assert initial["generation_status"] == "registry_fallback"
    optimized=store.optimize("tenant","company",1,company(),ConfigLLM(),expected_revision=1)
    assert optimized["revision"] == 2
    assert optimized["config"].retrieval_strategy == "dependencies_first"
    conflict=store.optimize("tenant","company",1,company(),ConfigLLM(),expected_revision=1)
    assert conflict["status"] == "revision_conflict"
    newer=store.onboard("tenant","company",2,company())
    stale=store.optimize("tenant","company",1,company(),ConfigLLM(),expected_revision=newer["revision"])
    assert stale["status"] == "stale_context"
    assert store.get("tenant","company")["revision"] == newer["revision"]


def test_same_context_version_cannot_hide_changed_profile(tmp_path):
    store=CompanyAnalystStore(tmp_path / "companies.sqlite",PromptRegistry(tmp_path / "shared.sqlite"))
    store.onboard("tenant","company",1,company())
    changed=company().model_copy(update={"description":"A materially changed business profile."})
    with pytest.raises(ValueError,match="same context_version"):
        store.onboard("tenant","company",1,changed)


def test_context_change_during_generation_cannot_publish_stale_candidate(tmp_path):
    store=CompanyAnalystStore(tmp_path / "companies.sqlite",PromptRegistry(tmp_path / "shared.sqlite"))
    store.onboard("tenant","company",1,company())
    class RacingLLM(ConfigLLM):
        def complete(self,messages,**kwargs):
            store.onboard("tenant","company",2,company())
            return super().complete(messages,**kwargs)
    result=store.optimize("tenant","company",1,company(),RacingLLM(),expected_revision=1)
    assert result["status"]=="stale_context"
    assert store.get("tenant","company")["context_version"]==2


def test_rollback_restores_preferences_but_keeps_current_profile_snapshot(tmp_path):
    registry=PromptRegistry(tmp_path / "shared.sqlite")
    store=CompanyAnalystStore(tmp_path / "companies.sqlite",registry)
    profile_v1=company()
    initial=store.onboard("tenant","company",1,profile_v1)
    optimized=store.optimize("tenant","company",1,profile_v1,ConfigLLM(),expected_revision=initial["revision"])
    profile_v2=profile_v1.model_copy(update={"description":"Updated business description."})
    rebased=store.onboard("tenant","company",2,profile_v2)
    rolled=store.rollback("tenant","company",expected_revision=rebased["revision"])
    assert rolled["config"].retrieval_strategy==initial["config"].retrieval_strategy
    assert rolled["config"].retrieval_strategy!=optimized["config"].retrieval_strategy
    assert rolled["context_version"]==2
    assert store.get("tenant","company")["context_hash"] == store._context_digest(profile_v2)
    repeated=store.onboard("tenant","company",2,profile_v2)
    assert repeated["config_ref"]==rolled["config_ref"]
    assert repeated["revision"]==rolled["revision"]


def test_registry_change_is_not_skipped_when_profile_changes_at_same_time(tmp_path):
    registry=PromptRegistry(tmp_path / "shared.sqlite")
    store=CompanyAnalystStore(tmp_path / "companies.sqlite",registry)
    profile_v1=company()
    store.onboard("tenant","company",1,profile_v1)
    active=AnalystConfig(config_id="new-global",focus=["growth"],role_scope="any")
    registry.register(active)
    scope="impact:any:*"
    registry.promote(scope=scope,ref=active.ref,reviewer="reviewer",expected_revision=0,
        report={"schema_version":"tournament-report-v1","winner_ref":active.ref,"baseline_ref":"baseline@1",
            "baseline_snapshot":registry.snapshot(scope),"mode":"model","shadow":False,
            "promotion_eligible":True,"synthetic":False,
            "holdout":{"passed":True,"fresh_for_selection":True},
            "shadow_validation":{"validated":True,"passed":True,"fresh":True},
            "human_review":{"approved":True,"reviewer":"reviewer"}})
    profile_v2=profile_v1.model_copy(update={"description":"Updated business description."})
    result=store.onboard("tenant","company",2,profile_v2)
    assert result["generation_status"]=="registry_context_rebased"
    assert result["config"].focus==["growth"]
    assert result["history"][-1]["source_config_ref"]==active.ref
