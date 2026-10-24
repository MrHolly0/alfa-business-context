"""HTTP and Core-port checks with real collection, corpus and data workflow."""
import hashlib
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from intelligence.api import create_app
from intelligence.artifacts import ArtifactStore
from intelligence.core_client import CoreClient
from intelligence.core_stand import create_app as stand_app
from intelligence.pipeline import AnalysisPipeline
from intelligence.worker import Worker
from test_workflow import workflow, task, policy, grant, FEED

AUTH={'Authorization':'Bearer service-test'}

def api(tmp_path):
    data,fetcher=workflow(tmp_path)
    artifacts=ArtifactStore(tmp_path/'artifacts.db')
    return data,fetcher,TestClient(create_app(store=data.store,pipeline=AnalysisPipeline(data.store),data_workflow=data,
        artifact_store=artifacts,service_token='service-test',tenant_ids={'tenant-a'}))

def test_existing_analyze_route_runs_data_task_and_returns_replay(tmp_path):
    data,fetcher,client=api(tmp_path)
    body=task(tmp_path).model_dump(mode='json')
    response=client.post('/internal/v1/analyze',headers=AUTH,json=body)
    assert response.status_code==200,response.text
    result=response.json()
    assert result['schema_version']=='ai-data-result-v1' and result['events'] and result['candidates']
    assert client.post('/internal/v1/analyze',headers=AUTH,json=body).json()==result
    assert len(fetcher.calls)==1
    progress=client.get('/internal/v1/executions/request-1',params={'tenant_id':'tenant-a'},headers=AUTH)
    assert progress.status_code==200 and progress.json()['stages']
    denied=client.get('/internal/v1/executions/request-1',params={'tenant_id':'tenant-b'},headers=AUTH)
    assert denied.status_code==403

def test_original_is_exact_upstream_and_policy_exclusion_blocks_old_raw(tmp_path):
    data,_,client=api(tmp_path)
    body=task(tmp_path).model_dump(mode='json')
    result=client.post('/internal/v1/analyze',headers=AUTH,json=body).json()
    original={'tenant_id':'tenant-a','source_policy':body['source_policy'],'document_revision_ref':result['events'][0]['sources'][0]['document_revision_ref'],'as_of':body['as_of']}
    raw=client.post('/internal/v1/originals',headers=AUTH,json=original)
    assert raw.status_code==200 and raw.content==FEED
    assert raw.headers['x-content-sha256']==hashlib.sha256(FEED).hexdigest()
    original['source_policy']['excluded_document_uris']=['https://news.example/story-1']
    assert client.post('/internal/v1/originals',headers=AUTH,json=original).status_code==404
    search={'tenant_id':'tenant-a','source_ids':['news'],'source_policy':original['source_policy'],'as_of':body['as_of'],'query':'Северный'}
    assert client.post('/internal/v1/search',headers=AUTH,json=search).json()==[]

def test_real_worker_runs_data_task_through_existing_core_http_contract(tmp_path):
    data,_=workflow(tmp_path)
    prepared=task(tmp_path,budget={'max_model_calls':0}).model_dump(mode='json')
    now=datetime.now(timezone.utc)
    job={'schema_version':'ai-job-v1','message_id':'seed','job_id':'full-path','tenant_scope':'tenant-a',
        'company_id':prepared['company_id'],'trace_id':'trace','lease_generation':1,
        'input_versions':{'context_version':1,'source_policy_version':7,'event_versions':[]},
        'deadline':(now+timedelta(minutes=5)).isoformat(),'lease_until':(now+timedelta(seconds=60)).isoformat(),
        'scope':{'source_ids':['news'],'document_revision_refs':[]},
        'budget':{'max_model_calls':0,'max_tokens':60000,'cost_reservation_rub':0},'task':prepared}
    app=stand_app(tmp_path/'stand.db',fixture_mode=True,operator_token='contract-test')
    with TestClient(app,client=('127.0.0.1',1234)) as http:
        assert http.post('/fixture/seed',headers={'X-Operator-Token':'contract-test'},json={'job':job}).status_code==200
        core=CoreClient('http://testserver','contract-test',client=http)
        worker=Worker(core,AnalysisPipeline(data.store),ArtifactStore(tmp_path/'artifacts.db'),data_workflow=data,tenant_ids={'tenant-a'},heartbeat_interval=.05)
        result=worker.run_once()
        assert result['status']=='submitted',result
        state=http.get('/fixture/state',headers={'X-Operator-Token':'contract-test'}).json()
        assert state['published'] and state['published'][0]['revision']==1
        assert worker.run_once() is None


def test_schedule_rejects_model_cap_then_executes_local_work_through_worker(tmp_path):
    data,_=workflow(tmp_path)
    request=task(tmp_path,request_id='scheduled-default').model_dump(mode='json')
    app=stand_app(tmp_path/'schedule-stand.db',fixture_mode=True,operator_token='schedule-test')
    stand=app.state.core_stand
    import pytest
    with pytest.raises(ValueError,match='max_model_calls=0'): stand.add_schedule('poll-news',request,60)
    request['budget']['max_model_calls']=0
    stand.add_schedule('poll-news',request,60)
    assert len(stand.enqueue_due_schedules())==1
    with TestClient(app,client=('127.0.0.1',1234)) as http:
        core=CoreClient('http://testserver','schedule-test',client=http)
        worker=Worker(core,AnalysisPipeline(data.store),ArtifactStore(tmp_path/'schedule-artifacts.db'),data_workflow=data,tenant_ids={'tenant-a'})
        result=worker.run_once()
        assert result['status']=='submitted',result


def test_period_retrospective_includes_late_received_external_not_bank(tmp_path):
    from intelligence.workflow import PeriodSpec
    data,_=workflow(tmp_path)
    now=datetime.now(timezone.utc)
    published=(now-timedelta(days=2)).isoformat()
    cutoff=now-timedelta(days=1)
    for role,source in [('external','news'),('bank','bank-note')]:
        data.store.ingest({'tenant_id':'tenant-a','source_id':source,'source_role':role,'canonical_uri':'https://news.example/'+role,
            'title':'Поздно полученный материал','content':'Порт остановлен.','content_type':'text/plain','published_at':published,'fetched_at':now.isoformat(),'synthetic':True})
    from intelligence.collection import SourceGrant
    source_policy=policy(grant(),SourceGrant(source_id='bank-note',source_role='bank',uri='https://bank.example/rss',content_type='application/rss+xml',data_classification='synthetic'))
    for mode in ['retrospective','as_known']:
        request=task(tmp_path,request_id='period-'+mode,operation='period',as_of=cutoff,source_policy=source_policy,
            period=PeriodSpec(start=now-timedelta(days=3),end=now,mode=mode))
        result=data.run(request)
        timeline=result.period_report['publication_timeline']
        assert len(timeline)==(1 if mode=='retrospective' else 0)
        if timeline: assert timeline[0]['source_id']=='news'
