import copy,json
from concurrent.futures import ThreadPoolExecutor

import pytest

from intelligence.analysts import AnalystConfig,PromptRegistry
from intelligence.pipeline import AnalysisPipeline
from intelligence.tournament import TournamentRunner,TournamentStore
from test_analysts import eligible_report
from test_tournament import case,shadow_case,FakeLLM,FlipJudge


def dataset():
    return {'schema_version':'tournament-dataset-v1','synthetic':False,
            'train':[case('train','train-event','train-company',synthetic=False)],
            'holdout':[case('hold','hold-event','hold-company',synthetic=False)]}


def shadow_dataset():
    return {'schema_version':'tournament-dataset-v1','synthetic':False,
            'train':[shadow_case('shadow','shadow-event','shadow-company')],
            'holdout':[shadow_case('shadow-hold','shadow-hold-event','shadow-hold-company')]}


def runner(tmp_path,registry):
    store=TournamentStore(tmp_path/'arena.sqlite')
    return TournamentRunner(store,registry,pipeline_factory=lambda corpus,config:AnalysisPipeline(corpus,FakeLLM(),analyst_config=config),judge=FlipJudge())


def approve(arena,run_id):
    report=arena.store.get(run_id)
    with arena.store._db() as db:
        labels=json.loads(db.execute('SELECT manifest FROM runs WHERE run_id=%s',(run_id,)).fetchone()[0])['review_label_map']
    ratings={item['blind_id']:next(letter for letter,ref in labels[item['blind_id']].items() if ref==report['winner_ref']) for item in arena.store.review_pack(run_id)}
    return arena.store.review(run_id,reviewer='alice',ratings=ratings)


def test_same_content_with_new_ids_is_not_holdout():
    d=dataset();original=d['train'][0]['request']
    hold=d['holdout'][0]['request']
    hold['event']['text']='  '+original['event']['text'].upper()+'  '
    with pytest.raises(ValueError,match='content_or_origin'):TournamentRunner._validate_dataset(d)
    d=dataset();d['holdout'][0]['request']['company']['name']=d['train'][0]['request']['company']['name']
    with pytest.raises(ValueError,match='content_or_origin'):TournamentRunner._validate_dataset(d)


def test_related_declared_group_cannot_cross_split():
    d=dataset()
    d['train'][0]['group_id']=d['holdout'][0]['group_id']='same-original'
    with pytest.raises(ValueError,match='content_or_origin'):TournamentRunner._validate_dataset(d)


def test_compare_to_active_baseline_and_publish_only_while_snapshot_current(tmp_path):
    registry=PromptRegistry(tmp_path/'registry.sqlite')
    for ref in ['stable','candidate','other']: registry.register(AnalystConfig(config_id=ref))
    scope='impact:any:*'
    registry.promote(scope=scope,ref='stable@1',reviewer='alice',report=eligible_report(registry,scope,'stable@1'))
    arena=runner(tmp_path,registry)
    report=arena.run(run_id='against-active',dataset=dataset(),shadow_dataset=shadow_dataset(),candidate_refs=['stable@1','candidate@1'],mode='model',execution_identity={'model':'fake'},target_scope=scope)
    assert report['baseline_ref']=='stable@1'
    assert 'baseline@1' not in report['training']
    reviewed=approve(arena,'against-active')
    assert reviewed['promotion_eligible'] is True
    registry.promote(scope=scope,ref='other@1',expected_revision=1,reviewer='alice',report=eligible_report(registry,scope,'other@1'))
    with pytest.raises(ValueError):
        registry.promote(scope=scope,ref='candidate@1',expected_revision=1,reviewer='alice',report=reviewed)
    assert registry.select(role='analyst',industry='logistics').ref=='other@1'


def test_fallback_activation_change_also_invalidates_comparison(tmp_path):
    registry=PromptRegistry(tmp_path/'registry.sqlite')
    registry.register(AnalystConfig(config_id='candidate'))
    scope='impact:finance:логистика'
    report=eligible_report(registry,scope,'candidate@1')
    registry.promote(scope='impact:finance:*',ref='finance@1',reviewer='alice',report=eligible_report(registry,'impact:finance:*','finance@1'))
    with pytest.raises(ValueError,match='active baseline changed'):
        registry.promote(scope=scope,ref='candidate@1',reviewer='alice',report=report)


def test_reused_holdout_is_visible_and_never_eligible_for_second_selection(tmp_path):
    registry=PromptRegistry(tmp_path/'registry.sqlite');registry.register(AnalystConfig(config_id='candidate'))
    arena=runner(tmp_path,registry)
    args=dict(dataset=dataset(),shadow_dataset=shadow_dataset(),candidate_refs=['baseline@1','candidate@1'],mode='model',execution_identity={'model':'fake'},target_scope='impact:any:*')
    first=arena.run(run_id='first',**args)
    assert first['holdout']['fresh_for_selection'] is True
    assert approve(arena,'first')['promotion_eligible'] is True
    second=arena.run(run_id='second',**args)
    assert second['holdout']['prior_run_count']==1
    assert approve(arena,'second')['promotion_eligible'] is False
    # IDs cannot turn previously exposed content into a new holdout.
    new=copy.deepcopy(args);new['dataset']['holdout'][0]['case_id']='hold-renamed'
    new['dataset']['holdout'][0]['request']['event']['event_id']='renamed'
    third=arena.run(run_id='third',**new)
    assert third['holdout']['fresh_for_selection'] is False


def test_missing_fresh_shadow_comparison_blocks_promotion(tmp_path):
    registry=PromptRegistry(tmp_path/'registry.sqlite');registry.register(AnalystConfig(config_id='candidate'))
    arena=runner(tmp_path,registry)
    report=arena.run(run_id='without-shadow',dataset=dataset(),candidate_refs=['baseline@1','candidate@1'],mode='model',
        execution_identity={'model':'fake'},target_scope='impact:any:*')
    assert report['shadow_validation']['validated'] is False
    assert approve(arena,'without-shadow')['promotion_eligible'] is False


def test_legacy_report_without_active_comparison_is_not_publishable(tmp_path):
    registry=PromptRegistry(tmp_path/'registry.sqlite')
    report=eligible_report(registry,'impact:any:*','baseline@1')
    del report['baseline_snapshot']
    with pytest.raises(ValueError,match='active scope baseline'):
        registry.promote(scope='impact:any:*',ref='baseline@1',report=report,reviewer='alice')


def test_concurrent_holdout_registration_has_only_one_fresh_run(tmp_path):
    store=TournamentStore(tmp_path/'exposure.sqlite')
    cases=dataset()['holdout']
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda run:store.expose_holdout(run,cases),['first','second']))
    assert sum(result['fresh_for_selection'] for result in results)==1
