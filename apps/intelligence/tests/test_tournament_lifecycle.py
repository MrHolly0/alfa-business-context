from intelligence.analysts import AnalystConfig, PromptRegistry
from intelligence.pipeline import AnalysisPipeline
from intelligence.tournament import TournamentRunner, TournamentStore
from test_tournament import FakeLLM, case, shadow_case


class ShadowPrefersBaselineJudge:
    def judge(self, *, case, a, b, orientation):
        if case["question"]["title"].startswith("Отдельная теневая публикация"):
            winner = "B" if orientation == "candidate_first" else "A"
        else:
            winner = "A" if orientation == "candidate_first" else "B"
        return {"winner": winner, "scores": {}, "reason": "fixture", "advisory": True}


def test_shadow_is_fresh_post_selection_comparison_and_cannot_change_winner(tmp_path):
    registry = PromptRegistry(tmp_path / "registry.sqlite")
    registry.register(AnalystConfig(config_id="candidate"))
    training = {
        "schema_version": "tournament-dataset-v1", "synthetic": False,
        "train": [case("train", "event-train", "company-train", synthetic=False)],
        "holdout": [case("hold", "event-hold", "company-hold", synthetic=False)],
    }
    shadow = {
        "schema_version": "tournament-dataset-v1", "synthetic": False,
        "train": [shadow_case("shadow-train", "event-shadow-train", "company-shadow-train")],
        "holdout": [shadow_case("shadow-hold", "event-shadow-hold", "company-shadow-hold")],
    }
    runner = TournamentRunner(TournamentStore(tmp_path / "arena.sqlite"), registry,
        pipeline_factory=lambda corpus, config: AnalysisPipeline(corpus, FakeLLM(), analyst_config=config),
        judge=ShadowPrefersBaselineJudge())
    report = runner.run(run_id="shadow-run", dataset=training, shadow_dataset=shadow,
        candidate_refs=["baseline@1", "candidate@1"], mode="model",
        execution_identity={"model":"fake-model"}, target_scope="impact:any:*")

    assert report["winner_ref"] == "candidate@1"
    validation = report["shadow_validation"]
    assert validation["validated"] is False and validation["fresh"] is True
    assert validation["case_count"] == 2
    assert len(validation["comparisons"]) == 2
    assert all(result["winner"] == "baseline" for result in validation["comparisons"])
    assert validation["candidate_no_regression"] is False
    assert validation["comparison_complete"] is True
    assert report["winner_ref"] == validation["candidate_ref"]


class ScriptedAnalyst:
    """LLM adapter returns evidence IDs/quotes from the actual pipeline prompt."""
    config = type("Config", (), {"model":"scripted-analyst", "max_output_tokens":2048})()

    def __init__(self):
        self.calls = []

    def complete(self, messages, **kwargs):
        import json
        self.calls.append(messages)
        payload = json.loads(messages[-1]["content"].rsplit("\n", 1)[-1])
        evidence = next(item for item in payload["RETRIEVED_EVIDENCE"] if item["source_role"] == "external")
        focus = ",".join(payload["ANALYST_PREFERENCES"]["focus"]) or "none"
        result = {
            "summary":f"focus={focus}; source reports a bounded delivery delay.",
            "claims":[{"claim_id":"source-fact","kind":"external_fact",
                       "text":f"The source reports: {evidence['quote']}",
                       "evidence_refs":[evidence["evidence_id"]]}],
            "influence_chain":[],"clarifications":[],
        }
        return {"content":json.dumps(result),"model":self.config.model,
                "usage":{"prompt_tokens":120,"completion_tokens":40},"cost":"0.001",
                "cost_currency":"USD","cost_source":"fixture"}


class ScriptedCriticAndRefiner:
    config = type("Config", (), {"model":"scripted-critic", "max_output_tokens":2400})()

    def __init__(self):
        self.critique_calls = 0
        self.refinement_calls = 0
        self.last_system = None

    def complete(self, messages, **kwargs):
        import json
        system = messages[0]["content"]
        self.last_system = system
        payload = json.loads(messages[-1]["content"])
        if system.startswith("Find unsupported"):
            self.critique_calls += 1
            claim = payload["answer"]["claims"][0]
            evidence_id = payload["case"]["evidence"][0]["evidence_id"]
            return {"content":json.dumps({"critiques":[{"claim_id":claim["claim_id"],
                "issue":"Keep the timing statement limited to what the source says.",
                "evidence_refs":[evidence_id]}]})}
        assert "Revise one model answer" in system, system
        self.refinement_calls += 1
        original = payload["original_candidate"]
        evidence_id = payload["allowed_evidence"][0]["evidence_id"]
        claims = [dict(claim) for claim in original["claims"]]
        claims[0]["text"] = "The source states that deliveries will be delayed by two weeks."
        claims[0]["evidence_refs"] = [evidence_id]
        answer = {"summary":"refined focus=costs; deliveries may be delayed by two weeks.",
                  "claims":claims,"influence_chain":original["influence_chain"],
                  "clarifications":original["clarifications"]}
        return {"content":json.dumps({"candidate":answer}),"model":self.config.model,
                "usage":{"prompt_tokens":180,"completion_tokens":60},"cost":"0.002",
                "cost_currency":"USD","cost_source":"fixture"}


class FocusJudge:
    def judge(self, *, case, a, b, orientation):
        challenger_side = "A" if orientation == "candidate_first" else "B"
        challenger = a if challenger_side == "A" else b
        if "focus=costs" in challenger.get("summary", ""):
            winner = challenger_side
        else:
            winner = "B" if challenger_side == "A" else "A"
        return {"winner":winner,"scores":{},"reason":"deterministic fixture comparator", "advisory":True}


def test_generated_candidate_refines_rechecks_holds_shadow_promotes_runs_and_rolls_back(tmp_path):
    import json
    from pathlib import Path
    from intelligence.analysts import AnalystConfig
    from intelligence.experiments import CandidateGenerator
    from intelligence.pipeline import AnalysisPipeline
    from intelligence.tournament import TournamentRunner, TournamentStore
    from intelligence.corpus import CorpusStore
    from test_tournament import case, shadow_case

    registry = PromptRegistry(tmp_path / "operator-registry.sqlite")
    generated = CandidateGenerator().generate(base=registry.get("baseline@1"), count=3)
    assert len(generated) == 3
    for config in generated:
        registry.register(config)
    candidates = ["baseline@1", *(config.ref for config in generated)]
    train = case("training-main", "event-train-main", "company-train-main", synthetic=False)
    holdout = case("holdout-main", "event-holdout-main", "company-holdout-main", synthetic=False)
    shadow_train = shadow_case("shadow-training", "event-shadow-training", "company-shadow-training")
    shadow_holdout = shadow_case("shadow-holdout", "event-shadow-holdout", "company-shadow-holdout")
    dataset = {"schema_version":"tournament-dataset-v1","synthetic":False,
               "train":[train],"holdout":[holdout]}
    shadow = {"schema_version":"tournament-dataset-v1","synthetic":False,
              "train":[shadow_train],"holdout":[shadow_holdout]}
    analyst, critic = ScriptedAnalyst(), ScriptedCriticAndRefiner()
    arena = TournamentRunner(TournamentStore(tmp_path / "operator-tournament.sqlite"), registry,
        pipeline_factory=lambda corpus, config:AnalysisPipeline(corpus, analyst, analyst_config=config),
        judge=FocusJudge(), critic=critic)
    report = arena.run(run_id="generation-3-cycle", dataset=dataset, shadow_dataset=shadow,
        candidate_refs=candidates, mode="model", execution_identity={"model":"scripted-fixture"},
        target_scope="impact:finance:*")

    winner = report["winner_ref"]
    assert winner == "finance@1"
    refinement = report["refinement"]["refinement"]
    assert refinement["attempted"] is True and refinement["applied"] is True, {"refinement":refinement,"critic_calls":(critic.critique_calls,critic.refinement_calls),"system":critic.last_system}
    assert refinement["hard_checks_passed"] is True
    assert refinement["cost"] == "0.002" and refinement["cost_currency"] == "USD"
    assert critic.critique_calls == critic.refinement_calls == 1
    assert report["holdout"]["fresh_for_selection"] is True and report["holdout"]["passed"] is True
    assert report["shadow_validation"]["fresh"] is True and report["shadow_validation"]["passed"] is True
    assert report["shadow_validation"]["candidate_no_regression"] is True
    assert report["shadow_validation"]["grounded_evidence_preserved"] is True
    with arena.store._db() as db:
        cells=[json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM cells WHERE run_id=%s AND state='complete'",("generation-3-cycle",))]
    assert any(cell.get("split")=="train_refined" and cell.get("passed") is True for cell in cells)
    assert sum(cell.get("split")=="holdout" for cell in cells)==2
    assert sum(cell.get("split")=="shadow" for cell in cells)==4

    pack = arena.store.review_pack("generation-3-cycle")
    with arena.store._db() as db:
        manifest=json.loads(db.execute("SELECT manifest FROM runs WHERE run_id=%s",("generation-3-cycle",)).fetchone()[0])
    ratings={item["blind_id"]:next(label for label,ref in manifest["review_label_map"][item["blind_id"]].items()
                                    if ref==winner) for item in pack}
    reviewed=arena.store.review("generation-3-cycle",reviewer="release-reviewer",ratings=ratings)
    assert reviewed["human_review"]["approved"] is True and reviewed["promotion_eligible"] is True
    activation=registry.promote(scope="impact:finance:*",ref=winner,report=reviewed,reviewer="release-reviewer")
    assert activation["ref"] == winner

    # A standard pipeline using the operator registry now selects the winner.
    followup_request=train["request"]
    result=AnalysisPipeline(CorpusStore(tmp_path / "post-publish-corpus.sqlite"),
                            ScriptedAnalyst(),registry=registry).run(followup_request)
    assert result.versions.analyst_config == winner
    assert registry.rollback(scope="impact:finance:*",reviewer="operator",expected_revision=1)["revision"]==2
    after_rollback=AnalysisPipeline(CorpusStore(tmp_path / "post-rollback-corpus.sqlite"),
                                   ScriptedAnalyst(),registry=registry).run(followup_request)
    assert after_rollback.versions.analyst_config == "baseline@1"
