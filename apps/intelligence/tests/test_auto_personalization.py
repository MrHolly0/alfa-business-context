"""Automatic analyst optimization stays Core-owned, finite, and replay-safe."""
from datetime import datetime, timedelta, timezone
import json
import pytest

from intelligence.analysts import CompanyAnalystStore, PromptRegistry
from intelligence.artifacts import ArtifactStore
from intelligence.core_client import CoreClient
from intelligence.core_stand import CoreStand, create_app
from intelligence.llm.config import ProviderConfig
from intelligence.personalization import AutoOptimizationPolicy, FollowUpIntent
from intelligence.workflow import DataTask, DataWorkflow
from intelligence.worker import Worker
from test_core_stand import _ASGIClient
from test_workflow import company, policy, grant


class ModelFixture:
    def __init__(self):
        self.config = ProviderConfig(profile="hackathon", base_url="https://changellenge.up.railway.app/v1",
            api_key_env="FIXTURE_MODEL_KEY", model="google/gemma-4-26b-a4b-it", budget_scope="fixture",
            currency="RUB", daily_limit=100, input_per_million=1, output_per_million=1,
            timeout_seconds=5, max_output_tokens=2048)
        self.calls = 0

    def complete(self, *_args, **_kwargs):
        self.calls += 1
        return {"content": json.dumps({"config": {"focus": ["supply", "uncertainty"],
            "retrieval_strategy": "dependencies_first", "max_depth": 3, "horizon": "short"}}),
            "model": self.config.model, "usage": {"prompt_tokens": 20, "completion_tokens": 30},
            "cost": "0.001", "cost_currency": "RUB", "cost_source": "fixture"}


def _job(task, now, name):
    raw = task.model_dump(mode="json")
    return {"schema_version": "ai-job-v1", "message_id": "seed-" + name, "job_id": name,
        "tenant_scope": task.tenant_id, "company_id": task.company_id, "trace_id": "trace-" + name,
        "lease_generation": 1,
        "input_versions": {"context_version": task.context_version,
                           "source_policy_version": task.source_policy_version, "event_versions": []},
        "deadline": (now + timedelta(minutes=5)).isoformat(),
        "lease_until": (now + timedelta(seconds=30)).isoformat(),
        "budget": {"max_model_calls": 0, "max_tokens": task.budget.max_total_tokens,
                   "cost_reservation_rub": 0, "cost_reservation_usd": 0},
        "scope": {"source_ids": task.source_policy.effective_source_ids(), "document_revision_refs": []},
        "task": raw}


def _result(leased_job, candidate, message_id):
    return {"schema_version": "ai-result-v1", "message_id": message_id,
        "job_id": leased_job["job_id"], "tenant_scope": leased_job["tenant_scope"],
        "company_id": leased_job["company_id"], "trace_id": leased_job["trace_id"],
        "lease_generation": leased_job["lease_generation"], "input_versions": leased_job["input_versions"],
        "candidate": candidate}


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def test_intent_key_is_stable_and_excludes_parent_delivery_id(tmp_path):
    co = company()["company"]
    store = CompanyAnalystStore(tmp_path / "analysts.sqlite", PromptRegistry(tmp_path / "registry.sqlite"))
    record = store.onboard("tenant-a", "транссевер", 1, co)
    first = FollowUpIntent.from_configuration(parent_request_id="delivery-1", tenant_id="tenant-a",
        company_id="транссевер", context_version=1, company=co, configuration=record, reason="onboard")
    retry = FollowUpIntent.from_configuration(parent_request_id="delivery-2", tenant_id="tenant-a",
        company_id="транссевер", context_version=1, company=co, configuration=record, reason="onboard")
    assert first.idempotency_key == retry.idempotency_key
    assert first.parent_request_id != retry.parent_request_id
    with pytest.raises(ValueError, match="idempotency_key"):
        FollowUpIntent.model_validate({**first.model_dump(), "idempotency_key": "0" * 64})


def test_auto_optimization_policy_has_finite_nonzero_total_allocation():
    policy_value = AutoOptimizationPolicy(allocation_id="bounded", max_jobs=3,
        max_model_calls=1, max_input_tokens=100, max_output_tokens=50, max_total_tokens=150,
        max_steps=2, cost_reservation_rub=0.5, cost_reservation_usd=0)
    assert policy_value.total_reservation_cap["RUB"] == 1.5
    assert policy_value.cooldown_seconds == 3600
    assert policy_value.max_jobs_per_company_per_day == 2
    with pytest.raises(ValueError, match="positive explicit cost reservation"):
        AutoOptimizationPolicy(allocation_id="zero", max_jobs=1, max_model_calls=1,
            max_input_tokens=100, max_output_tokens=50, max_total_tokens=150, max_steps=2,
            cost_reservation_rub=0, cost_reservation_usd=0)


def test_core_client_worker_onboard_auto_optimize_and_next_analysis(tmp_path):
    now = datetime.now(timezone.utc)
    clock = lambda: now
    business = company()
    onboard = DataTask(request_id="first-onboard", tenant_id="tenant-a", company_id=business["company_id"],
        context_version=1, source_policy_version=7, operation="onboard", trigger="manual", as_of=now,
        source_policy=policy(grant()), businesses=[business], data_classification="public", budget={"max_model_calls": 0})
    allocation = AutoOptimizationPolicy(allocation_id="operator-allocation-1", max_jobs=1,
        max_model_calls=1, max_input_tokens=24_000, max_output_tokens=2048,
        max_total_tokens=60_000, max_steps=12, cost_reservation_rub=1, cost_reservation_usd=0)
    app = create_app(tmp_path / "core.sqlite", fixture_mode=True, operator_token="fixture",
                     clock=clock, auto_optimization_policy=allocation)
    stand: CoreStand = app.state.core_stand
    stand.seed_job(_job(onboard, now, "onboard"))
    core = CoreClient("http://core.test", "fixture", client=_ASGIClient(app, "fixture"))
    generator = ModelFixture()
    workflow = DataWorkflow(tmp_path / "workflow", role_services={"generator": generator}, clock=clock)
    worker = Worker(core, workflow, ArtifactStore(tmp_path / "worker.sqlite"), heartbeat_interval=10,
                    tenant_ids={"tenant-a"}, data_workflow=workflow)

    first = worker.run_once()
    assert first["status"] == "submitted", first
    state = stand.inspect()
    assert len(state["auto_optimization_intents"]) == 1
    assert state["auto_optimization_intents"][0]["status"] == "queued"
    assert len([item for item in state["jobs"] if item["job_id"].startswith("autoopt-")]) == 1
    with stand._connect() as db:
        accepted_parent_result = json.loads(db.execute(
            "SELECT accepted_result FROM jobs WHERE job_id='onboard'").fetchone()[0])
    duplicate_ack, duplicate_status = stand.submit_result(accepted_parent_result)
    assert duplicate_status == 200 and duplicate_ack["duplicate"] is True
    assert len([item for item in stand.inspect()["jobs"] if item["job_id"].startswith("autoopt-")]) == 1

    # Claim and execute the Core-created optimize job via the same CoreClient/Worker path.
    optimized = worker.run_once()
    assert optimized["status"] == "submitted"
    assert generator.calls == 1
    optimized_record = CompanyAnalystStore(workflow.company_dir("tenant-a", "транссевер") /
        "company-analysts.sqlite3", workflow.registry).get("tenant-a", "транссевер")
    assert optimized_record["config"].retrieval_strategy == "dependencies_first"

    # A distinct parent request for the same company/profile/context cannot create
    # another auto job after the first intent has been handled.
    with stand._connect() as db:
        optimized_result = json.loads(db.execute(
            "SELECT accepted_result FROM jobs WHERE job_id LIKE 'autoopt-%'").fetchone()[0])
    assert optimized_result["candidate"].get("follow_up_intents", []) == []
    replay_task = onboard.model_copy(update={"request_id": "second-onboard"})
    stand.seed_job(_job(replay_task, now, "second-onboard"))
    replay_job = stand.claim()["job"]
    replay_candidate = dict(optimized_result["candidate"])
    replay_candidate["request_id"] = replay_task.request_id
    replay_candidate["follow_up_intents"] = [dict(accepted_parent_result["candidate"]["follow_up_intents"][0],
        parent_request_id=replay_task.request_id, expected_config_revision=optimized_record["revision"])]
    replay_ack, replay_status = stand.submit_result(_result(replay_job, replay_candidate, "result-second-onboard"))
    assert replay_status == 200 and replay_ack["accepted"]
    assert len([item for item in stand.inspect()["jobs"] if item["job_id"].startswith("autoopt-")]) == 1

    foreign_task = onboard.model_copy(update={"request_id": "foreign-config"})
    stand.seed_job(_job(foreign_task, now, "foreign-config"))
    foreign_job = stand.claim()["job"]
    foreign_candidate = dict(optimized_result["candidate"])
    foreign_candidate["request_id"] = foreign_task.request_id
    foreign_candidate["configurations"] = [*foreign_candidate["configurations"],
        {**foreign_candidate["configurations"][0], "company_id": "other-company"}]
    foreign_ack, foreign_status = stand.submit_result(_result(foreign_job, foreign_candidate, "result-foreign"))
    assert foreign_status == 403 and foreign_ack["reason"] == "configuration_company_outside_parent_scope"
    assert not any(item["job_id"] == "foreign-config" and item["status"] == "complete"
                   for item in stand.inspect()["jobs"])

    proposal_task = onboard.model_copy(update={"request_id": "foreign-proposal"})
    stand.seed_job(_job(proposal_task, now, "foreign-proposal"))
    proposal_job = stand.claim()["job"]
    proposal_candidate = dict(optimized_result["candidate"])
    proposal_candidate.update(request_id=proposal_task.request_id,
        context_proposals=[{"tenant_id": "tenant-a", "company_id": "other-company"}])
    proposal_ack, proposal_status = stand.submit_result(
        _result(proposal_job, proposal_candidate, "result-foreign-proposal"))
    assert proposal_status == 403 and proposal_ack["reason"] == "context_proposal_outside_parent_scope"

    workflow.store.ingest({"tenant_id": "tenant-a", "source_id": "news", "source_role": "external",
        "data_classification": "public", "canonical_uri": "https://news.example/story-1",
        "title": "Учебный источник сообщает о задержке поставок оборудования",
        "content": "Поставки промышленного оборудования временно задержаны из-за закрытия порта.",
        "content_type": "text/plain", "published_at": (now - timedelta(minutes=30)).isoformat(),
        "fetched_at": now.isoformat(), "synthetic": False, "publisher": "Fixture News"})
    analysis = DataTask(request_id="next-analysis", tenant_id="tenant-a", company_id=business["company_id"],
        context_version=1, source_policy_version=7, operation="analyze", trigger="manual", as_of=now,
        source_policy=policy(grant()), businesses=[business], data_classification="public", budget={"max_model_calls": 0})
    stand.seed_job(_job(analysis, now, "next-analysis"))
    applied = worker.run_once()
    assert applied["status"] == "submitted"
    analyzed = workflow.state.cached(analysis)
    assert analyzed.candidates
    assert analyzed.configurations[0]["config_ref"] == optimized_record["config_ref"]
    assert analyzed.configurations[0]["config"]["max_depth"] == 3
    assert analyzed.candidates[0].versions.analyst_config == optimized_record["config_ref"]

    stale_optimize = DataTask.model_validate({**onboard.model_dump(), "request_id": "stale-config-revision",
        "operation": "optimize", "expected_config_revisions": {business["company_id"]: 1},
        "budget": {"max_model_calls": 0}})
    stand.seed_job(_job(stale_optimize, now, "stale-config-revision"))
    stale_job = stand.claim()["job"]
    stale_candidate = dict(accepted_parent_result["candidate"])
    stale_candidate.update(request_id=stale_optimize.request_id, follow_up_intents=[])
    stale_ack, stale_status = stand.submit_result(_result(stale_job, stale_candidate, "result-stale-config"))
    assert stale_status == 409 and stale_ack["reason"] == "stale_configuration_revision"


def test_initial_analyze_for_new_company_queues_auto_optimize(tmp_path):
    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    clock = MutableClock(now)
    business = company("Новая компания")
    first_analysis = DataTask(request_id="first-analysis", tenant_id="tenant-a",
        company_id=business["company_id"], context_version=1, source_policy_version=7,
        operation="analyze", trigger="manual", as_of=now, source_policy=policy(grant()),
        businesses=[business], data_classification="public", budget={"max_model_calls": 0})
    allocation = AutoOptimizationPolicy(allocation_id="initial-analysis-budget", max_jobs=1,
        max_model_calls=1, max_input_tokens=1000, max_output_tokens=500,
        max_total_tokens=1500, max_steps=3, cost_reservation_rub=1, cost_reservation_usd=0)
    app = create_app(tmp_path / "core.sqlite", fixture_mode=True, operator_token="fixture",
                     clock=clock, auto_optimization_policy=allocation)
    stand: CoreStand = app.state.core_stand
    first_job = _job(first_analysis, now, "first-analysis")
    for defaulted_field in ("schema_version", "operation", "auto_optimize"):
        del first_job["task"][defaulted_field]
    stand.seed_job(first_job)
    core = CoreClient("http://core.test", "fixture", client=_ASGIClient(app, "fixture"))
    generator = ModelFixture()
    workflow = DataWorkflow(tmp_path / "workflow", role_services={"generator": generator}, clock=clock)
    worker = Worker(core, workflow, ArtifactStore(tmp_path / "worker.sqlite"), heartbeat_interval=10,
                    tenant_ids={"tenant-a"}, data_workflow=workflow)

    result = worker.run_once()
    assert result["status"] == "submitted", result
    state = stand.inspect()
    assert len(state["auto_optimization_intents"]) == 1
    assert state["auto_optimization_intents"][0]["status"] == "queued"
    with stand._connect() as db:
        accepted = json.loads(db.execute(
            "SELECT accepted_result FROM jobs WHERE job_id='first-analysis'").fetchone()[0])
    assert accepted["candidate"]["follow_up_intents"][0]["reason"] == "context_changed"

    optimized = worker.run_once()
    assert optimized["status"] == "submitted"
    assert generator.calls == 1
    record = CompanyAnalystStore(workflow.company_dir("tenant-a", business["company_id"]) /
        "company-analysts.sqlite3", workflow.registry).get("tenant-a", business["company_id"])
    assert record["revision"] == 2
    assert worker.run_once() is None
    assert generator.calls == 1

    def submit_analysis(request_id, snapshot):
        parsed = DataTask(request_id=request_id, tenant_id="tenant-a", company_id=snapshot["company_id"],
            context_version=snapshot["context_version"], source_policy_version=7, operation="analyze",
            trigger="context_changed", as_of=clock(), source_policy=policy(grant()), businesses=[snapshot],
            data_classification="public", budget={"max_model_calls": 0})
        stand.seed_job(_job(parsed, clock(), request_id))
        result = worker.run_once()
        assert result["status"] == "submitted", result
        return parsed

    def intent_status(version, company_id):
        return next(item["status"] for item in reversed(stand.inspect()["auto_optimization_intents"])
                    if item["company_id"] == company_id and item["context_version"] == version)

    # A profile update during cooldown is kept at baseline and recorded as skipped.
    update2 = company("Новая компания", version=2)
    update2["company"]["description"] = "Изменённый профиль v2"
    submit_analysis("profile-v2", update2)
    assert intent_status(2, business["company_id"]) == "skipped_frequency"
    assert worker.run_once() is None
    assert generator.calls == 1

    # Changing allocation IDs cannot bypass the Core-owned company cooldown.
    stand.auto_optimization_policy = AutoOptimizationPolicy(allocation_id="reset-allocation",
        max_jobs=10, max_model_calls=1, max_input_tokens=1000, max_output_tokens=500,
        max_total_tokens=1500, max_steps=3, cost_reservation_rub=1, cost_reservation_usd=0)
    update3 = company("Новая компания", version=3)
    update3["company"]["description"] = "Изменённый профиль v3"
    submit_analysis("profile-v3-during-cooldown", update3)
    assert intent_status(3, business["company_id"]) == "skipped_frequency"
    assert generator.calls == 1

    # An unrelated company has an independent cooldown and may optimize now.
    other_company = company("Вторая компания")
    submit_analysis("other-company-first-analysis", other_company)
    assert intent_status(1, other_company["company_id"]) == "queued"
    assert worker.run_once()["status"] == "submitted"
    assert generator.calls == 2

    # After cooldown, a source refresh with the same profile does not retry either
    # skipped intent. It has no new intent and does not create a job.
    clock.advance(3601)
    submit_analysis("profile-v3-source-refresh", update3)
    assert len(stand.inspect()["auto_optimization_intents"]) == 4
    assert worker.run_once() is None
    assert generator.calls == 2

    # A later distinct profile change can use the second daily slot; the next one
    # is frequency-limited by the UTC-day count, independent of allocation ID.
    update4 = company("Новая компания", version=4)
    update4["company"]["description"] = "Изменённый профиль v4"
    submit_analysis("profile-v4-after-cooldown", update4)
    assert intent_status(4, business["company_id"]) == "queued"
    assert worker.run_once()["status"] == "submitted"
    assert generator.calls == 3
    clock.advance(3601)
    update5 = company("Новая компания", version=5)
    update5["company"]["description"] = "Изменённый профиль v5"
    submit_analysis("profile-v5-daily-limit", update5)
    assert intent_status(5, business["company_id"]) == "skipped_frequency"
    assert worker.run_once() is None
    assert generator.calls == 3


def test_no_allocation_and_disabled_opt_in_do_not_enqueue(tmp_path):
    now = datetime.now(timezone.utc)
    business = company()
    for enabled, allocation, expected_status in ((True, None, "skipped_budget"), (False, None, None)):
        state = tmp_path / f"case-{enabled}"
        task = DataTask(request_id=f"onboard-{enabled}", tenant_id="tenant-a", company_id=business["company_id"],
            context_version=1, source_policy_version=7, operation="onboard", trigger="manual", as_of=now,
            source_policy=policy(grant()), businesses=[business], data_classification="public",
            budget={"max_model_calls": 0}, auto_optimize=enabled)
        workflow = DataWorkflow(state / "workflow", clock=lambda: now)
        data_result = workflow.run(task)
        app = create_app(state / "core.sqlite", fixture_mode=True, operator_token="fixture",
                         clock=lambda: now, auto_optimization_policy=allocation)
        stand: CoreStand = app.state.core_stand
        stand.seed_job(_job(task, now, f"job-{enabled}"))
        leased = stand.claim()["job"]
        ack, status = stand.submit_result(_result(leased, data_result.model_dump(mode="json"), f"result-{enabled}"))
        assert status == 200 and ack["accepted"]
        intents = stand.inspect()["auto_optimization_intents"]
        if expected_status:
            assert len(intents) == 1 and intents[0]["status"] == expected_status
        else:
            assert intents == []
        assert len([job for job in stand.inspect()["jobs"] if job["job_id"].startswith("autoopt-")]) == 0


def test_stale_parent_context_cannot_accept_or_enqueue_follow_up(tmp_path):
    now = datetime.now(timezone.utc)
    business = company()
    task = DataTask(request_id="stale-context", tenant_id="tenant-a", company_id=business["company_id"],
        context_version=1, source_policy_version=7, operation="onboard", trigger="manual", as_of=now,
        source_policy=policy(grant()), businesses=[business], data_classification="public", budget={"max_model_calls": 0})
    workflow = DataWorkflow(tmp_path / "workflow", clock=lambda: now)
    data_result = workflow.run(task)
    stand = CoreStand(tmp_path / "core.sqlite", clock=lambda: now,
        auto_optimization_policy=AutoOptimizationPolicy(allocation_id="finite", max_jobs=1,
            max_model_calls=1, max_input_tokens=100, max_output_tokens=50, max_total_tokens=150,
            max_steps=2, cost_reservation_rub=1, cost_reservation_usd=0))
    stand.seed_job(_job(task, now, "stale-context"))
    leased = stand.claim()["job"]
    stand.update_versions("tenant-a", business["company_id"], 2, 7)
    ack, status = stand.submit_result(_result(leased, data_result.model_dump(mode="json"), "stale-context-result"))
    assert status == 409 and ack["reason"] == "stale_input_versions"
    assert stand.inspect()["auto_optimization_intents"] == []
