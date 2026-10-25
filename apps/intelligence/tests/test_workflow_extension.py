"""Core snapshots, explicit configuration jobs and personal period analysis."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient

from intelligence.analysts import CompanyAnalystStore
from intelligence.api import create_app
from intelligence.artifacts import ArtifactStore
from intelligence.pipeline import AnalysisPipeline
from intelligence.workflow import DataWorkflow, PeriodSpec, WorkflowSnapshotConflict
from test_workflow import NOW, company, policy, grant, task, workflow


class ConfigModel:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {'config': {'focus': ['supply', 'uncertainty'],
            'retrieval_strategy': 'dependencies_first', 'max_depth': 3, 'horizon': 'short'}}

    def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return {'content': json.dumps(self.payload), 'model': 'configuration-fixture'}


def configuration_store(instance):
    return CompanyAnalystStore(instance.company_dir('tenant-a','транссевер') / 'company-analysts.sqlite3', instance.registry)


def test_explicit_optimization_is_budgeted_used_by_next_analysis_and_rollback(tmp_path):
    instance, _, initial = _seed(tmp_path)
    first = initial.configurations[0]
    generator = ConfigModel()
    instance.role_services = {'generator': generator}
    optimized = instance.run(task(tmp_path, request_id='optimize', operation='optimize',
        expected_config_revisions={'транссевер': first['revision']}, budget={'max_model_calls':1}))
    record = optimized.configurations[0]
    assert optimized.coverage['model_calls'] == len(generator.calls) == 1
    assert record['config']['retrieval_strategy'] == 'dependencies_first'
    assert record['config_ref'] != first['config_ref']
    analysis = instance.run(task(tmp_path, request_id='after-optimization', operation='analyze'))
    assert analysis.candidates[0].versions.analyst_config == record['config_ref']
    assert analysis.configurations[0]['config']['max_depth'] == 3
    # Config-only generator is not called again by normal event processing.
    assert len(generator.calls) == 1
    rolled = instance.run(task(tmp_path, request_id='rollback', operation='rollback_config',
        expected_config_revisions={'транссевер':record['revision']}))
    restored = instance.run(task(tmp_path, request_id='after-rollback', operation='analyze'))
    assert restored.candidates[0].versions.analyst_config == rolled.configurations[0]['config_ref']
    assert restored.configurations[0]['config']['retrieval_strategy'] == first['config']['retrieval_strategy']


def _seed(tmp_path):
    instance, fetcher = workflow(tmp_path)
    result = instance.run(task(tmp_path))
    return instance, fetcher, result


@pytest.mark.parametrize('availability', ['missing', 'invalid', 'exhausted'])
def test_unsuccessful_optimization_keeps_working_configuration(tmp_path, availability):
    instance, _, first = _seed(tmp_path)
    ref = first.configurations[0]['config_ref']
    model = ConfigModel({'bad': 'unusable'})
    if availability != 'missing': instance.role_services = {'generator':model}
    result = instance.run(task(tmp_path, request_id='failed-optimize', operation='optimize',
        expected_config_revisions={'транссевер':1},
        budget={'max_model_calls': 0 if availability == 'exhausted' else 1}))
    assert result.status == 'partial'
    assert configuration_store(instance).get('tenant-a','транссевер')['config_ref'] == ref
    assert not model.calls if availability in {'missing','exhausted'} else len(model.calls)==1


def test_older_context_cannot_overwrite_newer_running_analysis(tmp_path):
    instance, _, first = _seed(tmp_path)
    entered, release = Event(), Event()
    older = task(tmp_path, request_id='slow-old', operation='analyze')
    def hold(stage):
        if stage == 'personal_complete':
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(instance.run, older, hold)
        assert entered.wait(5)
        newest = instance.run(task(tmp_path, request_id='fresh', operation='analyze',
            businesses=[company(version=2, facts=[{'fact_id':'reserve','text':'Есть резервный маршрут','provenance':'договор:p2'}])]))
        release.set()
        with pytest.raises(WorkflowSnapshotConflict): pending.result(timeout=5)
    prior = instance.state.prior('tenant-a','транссевер',newest.events[0]['event_id'])
    assert prior['candidate']['versions']['context'] == 2
    with pytest.raises(WorkflowSnapshotConflict): instance.run(older)


def test_old_api_cache_is_rejected_after_new_context_was_observed(tmp_path):
    instance, _ = workflow(tmp_path)
    client = TestClient(create_app(store=instance.store, pipeline=AnalysisPipeline(instance.store),
        data_workflow=instance, artifact_store=ArtifactStore(tmp_path/'api.db'),
        service_token='integration-test',tenant_ids={'tenant-a'}))
    headers={'Authorization':'Bearer integration-test'}
    old = task(tmp_path,request_id='cached-old').model_dump(mode='json')
    assert client.post('/internal/v1/analyze',json=old,headers=headers).status_code==200
    new = task(tmp_path,request_id='new-context',operation='onboard', businesses=[company(version=2)])
    assert client.post('/internal/v1/analyze',json=new.model_dump(mode='json'),headers=headers).status_code==200
    response=client.post('/internal/v1/analyze',json=old,headers=headers)
    assert response.status_code==409
    assert response.json()['detail']=='stale_or_conflicting_company_context'


def test_period_returns_personal_candidates_without_overwriting_monitoring_cursor(tmp_path):
    instance, _, initial = _seed(tmp_path)
    event_id = initial.events[0]['event_id']
    before=instance.state.prior('tenant-a','транссевер',event_id)
    result=instance.run(task(tmp_path,request_id='period-personal',operation='period',period=PeriodSpec(
        start=NOW-timedelta(days=1),end=NOW,mode='as_known')))
    assert result.period_report['publication_timeline']
    assert result.candidates
    assert result.candidates[0].versions.as_of==NOW
    assert instance.state.prior('tenant-a','транссевер',event_id)==before
    outside=instance.run(task(tmp_path,request_id='outside-period',operation='period',period=PeriodSpec(
        start=NOW-timedelta(days=4),end=NOW-timedelta(days=2),mode='as_known')))
    assert outside.candidates==[]


def test_request_preferences_do_not_change_persisted_company_facts(tmp_path):
    instance, _, initial = _seed(tmp_path)
    business=company()
    business['preferences']={'role':'финансовый директор','answer_style':'concise','horizon':'short'}
    result=instance.run(task(tmp_path,request_id='preferences',operation='analyze',businesses=[business]))
    applied=next(row for row in result.trace if row['stage']=='preferences_applied')
    assert applied['preferences']==business['preferences']
    assert configuration_store(instance).get('tenant-a','транссевер')['config'].horizon==initial.configurations[0]['config']['horizon']


def test_operator_override_is_used_without_replacing_private_current(tmp_path):
    from intelligence.analysts import AnalystConfig
    instance,_,initial=_seed(tmp_path)
    override=AnalystConfig(config_id='review-method',retrieval_strategy='dependencies_first',horizon='short')
    instance.analyst_config=override
    result=instance.run(task(tmp_path,request_id='operator-selected',operation='analyze'))
    assert result.candidates[0].versions.analyst_config==override.ref
    assert result.configurations[0]['runtime_override_ref']==override.ref
    assert configuration_store(instance).get('tenant-a','транссевер')['config_ref']==initial.configurations[0]['config_ref']


def test_same_version_source_policy_is_immutable_but_personal_hide_is_not_analysis(tmp_path):
    instance,_,first=_seed(tmp_path)
    original=task(tmp_path,request_id='bad-policy',operation='analyze')
    changed=original.source_policy.model_copy(update={'excluded_source_ids':('news',)})
    with pytest.raises(WorkflowSnapshotConflict,match='stale_or_conflicting_source_policy'):
        instance.run(original.model_copy(update={'source_policy':changed}))
    hidden=original.source_policy.model_copy(update={'user_excluded_source_ids':('news',)})
    result=instance.run(original.model_copy(update={'request_id':'personal-hide','source_policy':hidden}))
    assert result.candidates
    assert result.revisions[0]['input_signature']==first.revisions[0]['input_signature']


def test_historical_ingest_does_not_mutate_current_event_versions(tmp_path):
    instance,_,first=_seed(tmp_path)
    instance.store.ingest({'tenant_id':'tenant-a','source_id':'news','source_role':'external',
        'canonical_uri':'https://news.example/story-1','title':'Уточнение: терминал вновь открыт',
        'content':'Северный порт открыт; поставки оборудования возобновлены.','content_type':'text/plain',
        'published_at':NOW.isoformat(),'fetched_at':(NOW+timedelta(hours=1)).isoformat(),'synthetic':True})
    latest=instance.run(task(tmp_path,request_id='latest-version',operation='analyze',as_of=NOW+timedelta(hours=2)))
    def event_rows():
        with instance.events._connect() as db:
            return ([tuple(row) for row in db.execute('SELECT event_id,revision,snapshot_hash FROM event_revisions ORDER BY event_id,revision')],
                    [tuple(row) for row in db.execute('SELECT publication_id,version,title,content_hash FROM publication_versions ORDER BY publication_id,version')])
    before=event_rows()
    historical=instance.run(task(tmp_path,request_id='old-history',operation='period',as_of=NOW+timedelta(hours=2),
        period=PeriodSpec(start=NOW-timedelta(days=1),end=NOW,mode='as_known')))
    assert historical.candidates
    assert all(c.versions.as_of==NOW for c in historical.candidates)
    assert historical.coverage['event_snapshot_ref'].startswith('historical:')
    assert historical.candidates[0].versions.event_document_revision_ref==first.candidates[0].versions.event_document_revision_ref
    assert event_rows()==before
    with pytest.raises(WorkflowSnapshotConflict,match='stale_analysis_as_of'):
        instance.run(task(tmp_path,request_id='obsolete-monitoring',operation='analyze',as_of=NOW))
    assert event_rows()==before


def test_period_known_cutoff_excludes_post_period_bank_evidence(tmp_path):
    instance,_,_= _seed(tmp_path)
    from intelligence.collection import SourceGrant
    bank=SourceGrant(source_id='bank',source_role='bank',uri='https://bank.example/rss',
        content_type='application/rss+xml',data_classification='synthetic')
    source_policy=policy(grant(),bank)
    instance.store.ingest({'tenant_id':'tenant-a','source_id':'bank','source_role':'bank',
        'canonical_uri':'https://bank.example/later','title':'Поздняя аналитика порта',
        'content':'Порт и поставки оборудования.','content_type':'text/plain',
        'published_at':(NOW+timedelta(hours=1)).isoformat(),'fetched_at':(NOW+timedelta(hours=1)).isoformat(),'synthetic':True})
    result=instance.run(task(tmp_path,request_id='period-bank',operation='period',source_policy=source_policy,
        as_of=NOW+timedelta(hours=2),period=PeriodSpec(start=NOW-timedelta(days=1),end=NOW,mode='as_known')))
    assert result.candidates
    assert all(e.source_role!='bank' for c in result.candidates for e in c.evidence)
    assert result.period_report['known_at'].startswith(NOW.isoformat())


def test_rollback_conflict_is_terminal_partial_without_mutation(tmp_path):
    instance,_,initial=_seed(tmp_path)
    result=instance.run(task(tmp_path,request_id='wrong-rollback',operation='rollback_config',
        expected_config_revisions={'транссевер':999}))
    assert result.status=='partial'
    assert result.configurations[0]['status']=='revision_conflict'
    assert configuration_store(instance).get('tenant-a','транссевер')['config_ref']==initial.configurations[0]['config_ref']


def test_as_known_requires_historical_profile_stamp_or_observed_records(tmp_path):
    instance,_,_=_seed(tmp_path)
    future_profile=company(facts=[{'fact_id':'secret-current','text':'UNSEENPROFILE2035','provenance':'current-only'}])
    research=task(tmp_path,request_id='undated-current-profile',operation='period',businesses=[future_profile],
        period=PeriodSpec(start=NOW-timedelta(days=1),end=NOW,mode='as_known'))
    result=instance.run(research)
    assert result.candidates
    assert 'UNSEENPROFILE2035' not in json.dumps([c.model_dump(mode='json') for c in result.candidates])
    assert result.coverage['profile_time_limitations']
    dated=company(facts=[{'fact_id':'dated','text':'Подтверждён запас оборудования','provenance':'receipt:p1',
                         'observed_at':(NOW-timedelta(days=2)).isoformat()}])
    verified=instance.run(task(tmp_path,request_id='dated-profile',operation='period',businesses=[dated],
        period=PeriodSpec(start=NOW-timedelta(days=1),end=NOW,mode='as_known')))
    assert verified.candidates[0].coverage.company_facts==1
