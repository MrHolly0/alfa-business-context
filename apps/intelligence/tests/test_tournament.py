import json
from pathlib import Path
from decimal import Decimal

import pytest

from intelligence.analysts import AnalystConfig, PromptRegistry
from intelligence.pipeline import AnalysisPipeline
from intelligence.tournament import TournamentRunner, TournamentStore


def case(case_id, event_id, company_id, *, synthetic=True):
    request = json.loads((Path(__file__).parents[1] / "examples" / "analysis-request.json").read_text(encoding="utf-8"))
    request["request_id"] = "req-" + case_id
    request["company_id"] = company_id
    request["event"]["event_id"] = event_id
    request["synthetic"] = synthetic
    request["event"]["synthetic"] = synthetic
    if case_id.startswith(("h","hold")):
        request["company"]["name"]="Ритейл Восток (fixture)"
        request["company"]["industry"]="Торговля"
        request["event"]["title"]="Изменение правил маркировки"
        request["event"]["text"]="Регулятор перенёс срок введения маркировки товаров на следующий год. Действующие требования сохраняются."
    else:
        request["company"]["name"]="Производство Север (fixture)"
    return {"case_id": case_id, "request": request, "documents": [], "expected": {}}


def shadow_case(case_id, event_id, company_id):
    result=case(case_id,event_id,company_id,synthetic=False)
    result["request"]["company"]["name"]="Shadow company "+company_id
    result["request"]["event"]["title"]="Отдельная теневая публикация "+case_id
    if "hold" in case_id:
        result["request"]["event"]["text"]="Теневая проверка сообщает: Центральный банк изменил требования к раскрытию валютных рисков для компаний, имеющих займы в иностранной валюте."
    else:
        result["request"]["event"]["text"]="Теневая публикация: производитель оборудования перенёс плановое обслуживание производственных линий и обновил график запуска новых станков."
    return result


def test_split_identity_leak_fails_before_generation():
    dataset = {"schema_version":"tournament-dataset-v1", "synthetic":True,
        "train":[case("t1", "evt1", "co1")], "holdout":[case("h1", "evt1", "co2")]}
    with pytest.raises(ValueError, match="train_holdout_identity_leak"):
        TournamentRunner._validate_dataset(dataset)


def test_reserved_cell_fails_closed_after_interruption(tmp_path):
    store = TournamentStore(tmp_path / "tournament.sqlite")
    assert store._reserve("run1", "cell1") is None
    with pytest.raises(ValueError, match="cell_interrupted_or_in_progress"):
        store._reserve("run1", "cell1")


def test_review_requires_exact_blind_ids_and_frozen_report(tmp_path):
    store = TournamentStore(tmp_path / "tournament.sqlite")
    with store._db() as db:
        report = {"schema_version":"tournament-report-v1", "winner_ref":"baseline@1", "synthetic":True,
            "training":{"baseline@1":{"wins":0,"losses":0,"ties":1}},
            "holdout":{"passed":True}, "hard_failures":[],
            "human_review":{"approved":False,"reviewer":None,"ratings":None,
                "pack":[{"blind_id":"blind-1"}]},
            "promotion_eligible":False}
        from intelligence.tournament import _digest, _json
        frozen = {k:v for k,v in report.items() if k not in {"human_review","report_hash","promotion_eligible"}}
        db.execute("INSERT INTO runs VALUES(?,?,?)", ("run", _json({"frozen_report_hash":_digest(frozen),"review_label_map":{"blind-1":{"A":"baseline@1","B":"other@1"}}}), _json(report)))
    with pytest.raises(ValueError, match="invalid_blind_review_ratings"):
        store.review("run", reviewer="reviewer", ratings={"wrong-id":"A"})


class FakeLLM:
    config = type("Config", (), {"max_output_tokens": 2048})()
    def complete(self, messages, **kwargs):
        return {"content":json.dumps({"summary":"Проверяемый разбор.","claims":[],"influence_chain":[],"clarifications":[]}),
                "model":"fake-model","response_id":"fake-1","usage":{"prompt_tokens":20,"completion_tokens":10},
                "cost":"0.001","cost_currency":"USD","cost_source":"provider_reported"}


class FlipJudge:
    def __init__(self): self.calls=[]
    def judge(self, *, case, a, b, orientation):
        self.calls.append((case,orientation))
        return {"winner":"A" if orientation=="candidate_first" else "B","scores":{},"reason":"pair","advisory":True}


class AlwaysPrefersFirstJudge:
    def judge(self, *, case, a, b, orientation):
        return {"winner":"A" if orientation=="candidate_first" else "B","scores":{},"reason":"candidate preference fixture","advisory":True}


def test_model_tournament_evaluates_only_finalist_on_holdout_and_can_promote(tmp_path):
    registry=PromptRegistry(tmp_path / "registry.sqlite")
    registry.register(AnalystConfig(config_id="candidate",role_scope="any"))
    store=TournamentStore(tmp_path / "tournament.sqlite")
    judge=FlipJudge()
    dataset={"schema_version":"tournament-dataset-v1","synthetic":False,
        "train":[case("train1","train-event","train-company",synthetic=False)],
        "holdout":[case("hold1","hold-event","hold-company",synthetic=False)]}
    factory=lambda corpus,config:AnalysisPipeline(corpus,llm=FakeLLM(),analyst_config=config)
    report=TournamentRunner(store,registry,pipeline_factory=factory,judge=judge).run(
        run_id="model-run",dataset=dataset,shadow_dataset={"schema_version":"tournament-dataset-v1","synthetic":False,
            "train":[shadow_case("shadow1","shadow-event","shadow-company")],
            "holdout":[shadow_case("shadow-hold","shadow-hold-event","shadow-hold-company")]},
        candidate_refs=["baseline@1","candidate@1"],mode="model",execution_identity={"model":"fake-model"},target_scope="impact:any:*")
    assert report["winner_ref"]=="candidate@1"
    assert report["holdout"]["passed"] is True
    assert len(judge.calls)==8 and all("evidence" in call[0] for call in judge.calls)
    assert "review_label_map" not in report["human_review"]
    assert set(store.review_pack("model-run")[0]) >= {"company","question","evidence","A","B"}
    with store._db() as db:
        labels=json.loads(db.execute("SELECT manifest FROM runs WHERE run_id='model-run'").fetchone()[0])["review_label_map"]
        holdout_cells=db.execute("SELECT payload FROM cells WHERE run_id='model-run'").fetchall()
    assert sum(json.loads(row["payload"])["split"]=="holdout" for row in holdout_cells)==2
    blind_id=report["human_review"]["pack"][0]["blind_id"]
    winner_label=next(label for label,ref in labels[blind_id].items() if ref==report["winner_ref"])
    reviewed=store.review("model-run",reviewer="human",ratings={blind_id:winner_label})
    assert reviewed["promotion_eligible"] is True
    registry.promote(scope="impact:any:*",ref="candidate@1",report=reviewed,reviewer="human")
    assert registry.select(role="analyst",industry="logistics").ref=="candidate@1"


def test_tied_candidate_margins_keep_baseline_and_decimal_identity_is_repeatable(tmp_path):
    registry=PromptRegistry(tmp_path / "registry.sqlite")
    store=TournamentStore(tmp_path / "tournament.sqlite")
    dataset={"schema_version":"tournament-dataset-v1","synthetic":True,
        "train":[case("train1","event1","company1")],"holdout":[case("hold1","event2","company2")]}
    runner=TournamentRunner(store,registry,pipeline_factory=lambda corpus,config:AnalysisPipeline(corpus),judge=AlwaysPrefersFirstJudge())
    args={"run_id":"stable-run","dataset":dataset,"candidate_refs":["baseline@1","finance@1","executive@1"],"execution_identity":{"input_rate":Decimal("1.25")}}
    first=runner.run(**args)
    second=runner.run(**args)
    assert first["winner_ref"]=="baseline@1"
    assert first==second
    with store._db() as db:
        cells=db.execute("SELECT payload FROM cells WHERE run_id='stable-run'").fetchall()
    assert sum(json.loads(row["payload"])["split"]=="holdout" for row in cells)==1
