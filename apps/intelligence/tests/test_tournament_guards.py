import pytest
from intelligence.analysts import AnalystConfig, PromptRegistry
from intelligence.pipeline import AnalysisPipeline
from intelligence.tournament import TournamentRunner,TournamentStore
from test_tournament import case,FakeLLM,FlipJudge


def setup(tmp_path,judge):
    registry=PromptRegistry(tmp_path/'registry.sqlite')
    registry.register(AnalystConfig(config_id='candidate'))
    store=TournamentStore(tmp_path/'runs.sqlite')
    calls=[]
    def factory(corpus,config):
        calls.append(config.ref)
        return AnalysisPipeline(corpus,FakeLLM(),analyst_config=config)
    runner=TournamentRunner(store,registry,pipeline_factory=factory,judge=judge)
    dataset={'schema_version':'tournament-dataset-v1','synthetic':True,
        'train':[case('train','train-event','train-company')],
        'holdout':[case('hold','hold-event','hold-company')]}
    args=dict(run_id='guard',dataset=dataset,candidate_refs=['baseline@1','candidate@1'],mode='model',execution_identity={'model':'fake-fixture'})
    return runner,store,args,calls


def test_position_bias_is_tie_and_replays_do_not_call_models(tmp_path):
    class BiasedJudge:
        calls=0
        def judge(self,**kwargs):
            self.calls+=1
            return {'winner':'A','advisory':True}
    judge=BiasedJudge();runner,store,args,calls=setup(tmp_path,judge)
    report=runner.run(**args)
    assert report['winner_ref']=='baseline@1'
    assert report['pairings']['candidate@1'][0]['flip_disagreement'] is True
    assert len(calls)==3 and judge.calls==2
    assert runner.run(**args)==report
    assert len(calls)==3 and judge.calls==2
    # Completed cells also survive a new run, whose inputs are identical.
    runner.run(**{**args,'run_id':'next'})
    assert len(calls)==3 and judge.calls==2


@pytest.mark.parametrize('fail',['regression','timeout'])
def test_holdout_failure_keeps_baseline(tmp_path,fail):
    class Judge:
        def judge(self,*,case,orientation,**kwargs):
            hold=case['question']['event_id']=='hold-event'
            if hold and fail=='timeout': raise TimeoutError()
            challenger=not hold
            winner=('A' if orientation=='candidate_first' else 'B') if challenger else ('B' if orientation=='candidate_first' else 'A')
            return {'winner':winner,'advisory':True}
    runner,store,args,calls=setup(tmp_path,Judge())
    report=runner.run(**args)
    assert report['winner_ref']=='baseline@1'
    assert report['holdout']['passed'] is False
    assert report['promotion_eligible'] is False
    if fail=='timeout':
        judgment=report['holdout']['judgments'][0]
        assert judgment['reason']=='judge_failed_or_interrupted'
        assert judgment['judge_error_type']=='TimeoutError'
        assert judgment['judge_error_reason']=='judge_timeout'
        assert judgment['failed_orientation']=='candidate_first'


def test_synthetic_flags_cannot_be_hidden_by_dataset_label(tmp_path):
    runner,store,args,calls=setup(tmp_path,FlipJudge())
    args['dataset']['synthetic']=False
    with pytest.raises(ValueError,match='synthetic_case_in_real_dataset'): runner.run(**args)
    assert calls==[]


def test_expected_failure_excludes_candidate_before_judging(tmp_path):
    judge=FlipJudge();runner,store,args,calls=setup(tmp_path,judge)
    args['dataset']['train'][0]['expected']={'require_hypothesis':True}
    report=runner.run(**args)
    assert report['winner_ref']=='baseline@1'
    assert report['hard_failures']
    assert judge.calls==[]


def test_review_can_be_repeated_without_changing_frozen_results(tmp_path):
    runner,store,args,calls=setup(tmp_path,FlipJudge())
    report=runner.run(**args)
    pack=store.review_pack(args['run_id'])
    assert pack and all('evidence' in p and 'company' in p for p in pack)
    assert 'candidate@1' not in str(pack) and 'fake-fixture' not in str(pack)
    ratings={p['blind_id']:'tie' for p in pack}
    first=store.review(args['run_id'],reviewer='team',ratings=ratings)
    second=store.review(args['run_id'],reviewer='team',ratings=ratings)
    assert first==second
    assert second['promotion_eligible'] is False
