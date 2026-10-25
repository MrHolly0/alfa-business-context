"""Regression checks for policy, capacity, leases and shared event processing."""
import json,copy,sqlite3,threading
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from intelligence.api import create_app
from intelligence.artifacts import ArtifactStore
from intelligence.pipeline import AnalysisPipeline
from intelligence.collection import NewsCollector,FetchResult
from intelligence.corpus import CorpusStore
from intelligence.events import EventStore
from test_workflow import workflow,task,policy,grant,company,NOW,FEED

AUTH={'Authorization':'Bearer audit-local'}

def test_personal_source_hide_does_not_change_company_analysis(tmp_path):
    data,_=workflow(tmp_path)
    hidden=policy(grant(),user_excluded_source_ids=('news',))
    result=data.run(task(tmp_path,source_policy=hidden))
    assert len(result.candidates)==1, ('personal hide erased analytics',result.status,result.collection)


def test_capacity_rejection_can_retry_same_request_after_capacity_is_free(tmp_path):
    data,_=workflow(tmp_path)
    artifacts=ArtifactStore(tmp_path/'api-artifacts.sqlite3')
    client=TestClient(create_app(store=data.store,pipeline=AnalysisPipeline(data.store),data_workflow=data,
       artifact_store=artifacts,service_token='audit-local',tenant_ids={'tenant-a'}))
    locks=['capacity-check-'+str(i) for i in range(data.max_concurrency)]
    with data.state.db() as db:
        db.lock()
        for i,owner in enumerate(locks):
            db.execute("INSERT INTO workflow_slots VALUES(%s,%s,clock_timestamp()+INTERVAL '60 seconds')",(i,owner))
    assert all(locks)
    body=task(tmp_path,request_id='capacity-retry').model_dump(mode='json')
    try:
        busy=client.post('/internal/v1/analyze',headers=AUTH,json=body)
        assert busy.status_code==409 and busy.json()['detail']=='capacity_exhausted'
    finally:
        with data.state.db() as db:
            for lock in locks:db.execute('DELETE FROM workflow_slots WHERE owner=%s',(lock,))
    retry=client.post('/internal/v1/analyze',headers=AUTH,json=body)
    assert retry.status_code==200, retry.text


def test_large_permitted_document_produces_bounded_partial_not_task_crash(tmp_path):
    data,_=workflow(tmp_path)
    data.store.ingest({'tenant_id':'tenant-a','source_id':'news','source_role':'external',
      'canonical_uri':'https://news.example/long','title':'Длинная учебная статья',
      'content':'Поставки через порт задерживаются. '*4000,'content_type':'text/plain',
      'published_at':(NOW-timedelta(days=1)).isoformat(),'fetched_at':(NOW-timedelta(hours=1)).isoformat(),'synthetic':True})
    result=data.run(task(tmp_path,operation='analyze'))
    assert result.status in {'partial','unknown','completed'}


def test_stale_poll_cannot_release_newer_poll_lease(tmp_path):
    store=CorpusStore(tmp_path/'corpus.sqlite3')
    collector=NewsCollector(store,state_path=tmp_path/'poll.sqlite3',fetcher=lambda *a,**k:FetchResult(status=304,headers={}))
    source=grant()
    old,reason=collector._claim_poll('tenant-a',source,NOW,force=True)
    assert reason is None
    later=NOW+timedelta(seconds=31)
    new,reason=collector._claim_poll('tenant-a',source,later,force=True)
    assert reason is None and old['lease_until']!=new['lease_until']
    collector._save_state('tenant-a',source,later+timedelta(hours=1),old,FetchResult(status=304,headers={}),None,success=True)
    _,reason=collector._claim_poll('tenant-a',source,later+timedelta(seconds=1),force=True)
    assert reason=='already_running', ('old poll overwrote active lease',reason)


def test_exact_reprints_do_not_create_independent_confirmation_count(tmp_path):
    data,_=workflow(tmp_path,FetchResult(status=200,headers={'content-type':'application/rss+xml'},body=FEED),
       FetchResult(status=200,headers={'content-type':'application/rss+xml'},body=FEED.replace(b'news.example/story-1',b'news-b.example/story-2')))
    result=data.run(task(tmp_path,source_policy=policy(grant('wire-a'),grant('wire-b',article_hosts=('news-b.example',)))))
    assert len(result.events)==1
    count=result.events[0]['coverage']['independent_origin_groups']
    assert count<=1, ('identical reprints counted as independent',count)


def test_numeric_retry_after_is_respected(tmp_path):
    from unittest.mock import patch
    with patch('intelligence.collection.random.uniform',return_value=60):
        retry=NewsCollector._backoff(grant(),None,NOW,'3600')
    assert retry>=NOW+timedelta(seconds=3600),retry


def test_cancellation_between_sources_prevents_next_fetch(tmp_path):
    data,_=workflow(tmp_path)
    cancelled=False;calls=[]
    class Cancelled(RuntimeError):pass
    def fetcher(url,headers,**kwargs):
        nonlocal cancelled
        calls.append(url);cancelled=True
        return FetchResult(status=200,headers={'content-type':'application/rss+xml'},body=FEED)
    def checkpoint(stage):
        if cancelled:raise Cancelled('synthetic lease loss')
    data.collector=NewsCollector(data.store,state_path=tmp_path/'cancel-poll.sqlite3',fetcher=fetcher)
    with pytest.raises(Cancelled):
        data.run(task(tmp_path,source_policy=policy(grant('a'),grant('b'))),checkpoint=checkpoint)
    assert len(calls)==1,('second source fetched after lease loss',len(calls))


def test_shared_package_concurrent_build_calls_model_once(tmp_path):
    events=EventStore(tmp_path/'events.sqlite3')
    event=events.ingest({'tenant_id':'tenant-a','source_id':'news','source_role':'external','canonical_uri':'https://news.example/article',
       'title':'Порт закрыт','content':'Порт закрыт. Поставки задержаны.','published_at':NOW.isoformat(),'fetched_at':NOW.isoformat(),'synthetic':True,'document_revision_ref':'doc:1'})
    barrier=threading.Barrier(2)
    class Extractor:
        config=SimpleNamespace(model='synthetic-audit',max_output_tokens=2048)
        def __init__(self):self.calls=0;self.lock=threading.Lock()
        def complete(self,*args,**kwargs):
            with self.lock:self.calls+=1
            try:barrier.wait(timeout=1)
            except threading.BrokenBarrierError:pass
            return {'content':json.dumps({'claims':[],'entities':[],'territories':[],'resources':[],'mechanisms':[]})}
    model=Extractor()
    def build():
        return events.build_impact_package('tenant-a',event['event_id'],source_policy_id='p',policy_version='1',allowed_source_ids=['news'],allowed_document_revision_refs=['doc:1'],llm=model)
    with ThreadPoolExecutor(max_workers=2) as pool:
        output=list(pool.map(lambda _:build(),range(2)))
    assert output[0].package_id==output[1].package_id
    assert model.calls==1,('shared cache performed duplicate model calls',model.calls)
