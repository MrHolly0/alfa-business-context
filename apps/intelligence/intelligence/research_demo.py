"""Data-driven local source cases for indirect research; expectations live separately."""
from __future__ import annotations
from datetime import datetime,timedelta
from email.utils import format_datetime
import json
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET
from .collection import FetchResult,NewsCollector,SourceGrant,SourcePolicySnapshot
from .workflow import BusinessSnapshot,DataTask,DataWorkflow

DEFAULT_INPUT=Path(__file__).resolve().parents[1]/'examples/indirect-research.json'

def run_research_cases(state_dir,*,input_path=DEFAULT_INPUT,llm=None,role_services=None):
    if (llm is not None or role_services) and Path(input_path).resolve()!=DEFAULT_INPUT.resolve():
        raise ValueError('custom_demo_model_input_requires_trusted_Core_job')
    data=json.loads(Path(input_path).read_text())
    if data.get('schema_version')!='indirect-research-input-v1' or data.get('classification')!='synthetic':
        raise ValueError('synthetic_research_fixture_required')
    if not 1<=len(data['cases'])<=10:raise ValueError('research_case_limit')
    instant=datetime.fromisoformat(data['as_of'].replace('Z','+00:00'))
    rows=[]
    for item in data['cases']:
        if not 1<=len(item['documents'])<=10:raise ValueError('research_document_limit')
        tenant='research-example-'+item['case_id'];uri='https://research.example/feed.xml'
        grant=SourceGrant(source_id='research-source',source_role='external',uri=uri,
            content_type='application/rss+xml',data_classification='synthetic',
            publisher='Синтетический источник',article_hosts=('research.example',),fetch_articles=False)
        policy=SourcePolicySnapshot(policy_id='research-policy',revision=1,tenant_id=tenant,issued_at=instant,sources=(grant,))
        rss=ET.Element('rss',version='2.0');channel=ET.SubElement(rss,'channel')
        ET.SubElement(channel,'title').text='Синтетический источник'
        for doc in item['documents']:
            node=ET.SubElement(channel,'item')
            values={'title':doc['title'],'description':doc['text'],
                'link':'https://research.example/'+doc['key'],
                'pubDate':format_datetime(datetime.fromisoformat(doc['published_at'].replace('Z','+00:00')) if doc.get('published_at') else instant-timedelta(hours=1))}
            for field,value in values.items():ET.SubElement(node,field).text=value
        feed=ET.tostring(rss,encoding='utf-8',xml_declaration=True)
        state=Path(state_dir)/item['case_id']
        workflow=DataWorkflow(state,clock=lambda:instant)
        def fetch(url,headers,**options):
            if url!=uri:raise ValueError('fixture_network_disabled')
            return FetchResult(status=200,headers={'content-type':'application/rss+xml'},body=feed)
        workflow.collector=NewsCollector(workflow.store,state_path=state/'collection.sqlite3',fetcher=fetch,raw_dir=state/'raw'/'collection')
        business=BusinessSnapshot(company_id=item['company_id'],context_version=1,company=item['company'],known_at=instant)
        task=DataTask(request_id='research-collect-'+uuid4().hex,tenant_id=tenant,company_id=business.company_id,
            context_version=1,source_policy_version=1,operation='collect',as_of=instant,source_policy=policy,
            businesses=[],budget={'max_model_calls':0},synthetic=True,data_classification='synthetic')
        collected=workflow.run(task)
        trigger_key=item['trigger_document_key']
        event=next(event for event in collected.events if any(
            source['canonical_uri'].rsplit('/',1)[-1]==trigger_key for source in event['sources']))
        active=DataWorkflow(state,llm=llm,role_services=role_services,clock=lambda:instant)
        searches=[];original=active.store.search
        def observed_search(**kwargs):
            found=original(**kwargs)
            searches.append({'role':kwargs.get('role'),'query':kwargs.get('query'),
                'source_ids':kwargs.get('source_ids'),'document_revision_refs':kwargs.get('document_revision_refs'),
                'found_document_refs':list(dict.fromkeys(row['document_revision_ref'] for row in found))})
            return found
        active.store.search=observed_search
        request=task.model_copy(update={'request_id':'research-analyze-'+uuid4().hex,'operation':'analyze',
            'event_ids':[event['event_id']],'businesses':[business],'trigger':'scheduled',
            'budget':task.budget.model_copy(update={'max_model_calls':3 if llm or role_services else 0})})
        result=active.run(request)
        rows.append({'case_id':item['case_id'],'input':item,'collection':collected.collection,
            'collected_events':collected.events,'actual_searches':searches,'result':result.model_dump(mode='json')})
    return {'schema_version':'indirect-research-result-v1','classification':'synthetic',
        'network_sources':'local_RSS_transport_fixture','model_requested':bool(llm or role_services),
        'quality_improvement_proven':False,'inputs':data,'cases':rows}
