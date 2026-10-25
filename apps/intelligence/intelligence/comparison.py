"""Reproducible description-to-result comparison with one shared event corpus."""
from __future__ import annotations
from datetime import datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET
from email.utils import format_datetime

from .collection import FetchResult, NewsCollector, SourceGrant, SourcePolicySnapshot
from .description_context import DescriptionPrepareRequest, prepare_description
from .workflow import BusinessSnapshot, DataTask, DataWorkflow

DEFAULT_INPUT=Path(__file__).resolve().parents[1]/'examples/personalization-comparison.json'

def run_comparison(state_dir,*,input_path=DEFAULT_INPUT,llm=None,role_services=None):
    data=json.loads(Path(input_path).read_text())
    if data.get('schema_version')!='personalization-comparison-input-v1':raise ValueError('invalid_comparison_input')
    if len(data['companies'])>20 or len(data['events'])>20:raise ValueError('comparison_input_limit')
    instant=datetime.fromisoformat(data['as_of'].replace('Z','+00:00'))
    tenant=data['tenant_id']
    proposals=[];businesses=[]
    for item in data['companies']:
        request=DescriptionPrepareRequest(tenant_id=tenant,company_id=item['company_id'],name=item['name'],
            description=item['description'],provenance='synthetic user questionnaire',as_of=instant,
            data_classification='synthetic',statement_status='core_confirmed')
        proposal=prepare_description(request)
        # Explicit fixture simulation of Core accepting the questionnaire. The
        # optional industry hint is supplied by Core, not inferred as a fact.
        company=proposal.context.model_copy(update={'industry':item.get('industry_hint','')})
        businesses.append(BusinessSnapshot(company_id=item['company_id'],context_version=1,
            company=company,known_at=instant))
        proposals.append({'input':request.model_dump(mode='json'),'proposal':proposal.model_dump(mode='json'),
            'core_supplied_industry_hint':item.get('industry_hint','')})
    if not businesses:raise ValueError('comparison_requires_companies')
    uri='https://comparison.example/feed.xml'
    grant=SourceGrant(source_id='comparison-source',source_role='external',uri=uri,
        content_type='application/rss+xml',data_classification='synthetic',
        publisher='Синтетический вестник',article_hosts=('comparison.example',),fetch_articles=False)
    policy=SourcePolicySnapshot(policy_id='comparison-policy',revision=1,tenant_id=tenant,
        issued_at=instant,sources=(grant,))
    rss=ET.Element('rss',version='2.0');channel=ET.SubElement(rss,'channel')
    ET.SubElement(channel,'title').text='Синтетический вестник'
    for event in data['events']:
        node=ET.SubElement(channel,'item')
        for field,value in {'title':event['title'],'description':event['text'],
            'link':'https://comparison.example/'+event['key'],
            'pubDate':format_datetime(instant-timedelta(hours=1))}.items():ET.SubElement(node,field).text=value
    feed=ET.tostring(rss,encoding='utf-8',xml_declaration=True)
    state=Path(state_dir);workflow=DataWorkflow(state,clock=lambda:instant)
    def fetch(url,headers,**options):
        if url!=uri:raise ValueError('comparison_is_local_fixture_only')
        return FetchResult(status=200,headers={'content-type':'application/rss+xml'},body=feed)
    workflow.collector=NewsCollector(workflow.store,state_path=state/'collection.sqlite3',fetcher=fetch,
        raw_dir=state/'raw'/'collection')
    primary=businesses[0]
    base=DataTask(request_id='comparison-collect-'+uuid4().hex,tenant_id=tenant,
        company_id=primary.company_id,context_version=1,source_policy_version=1,operation='collect',
        trigger='manual',as_of=instant,source_policy=policy,businesses=businesses,
        budget={'max_model_calls':0},data_classification='synthetic',synthetic=True)
    collected=workflow.run(base.model_copy(update={'businesses':[]}))
    key_by_event={event['event_id']:next(source['canonical_uri'].rsplit('/',1)[-1]
        for source in event['sources']) for event in collected.events}
    active=DataWorkflow(state,llm=llm,role_services=role_services,clock=lambda:instant)
    results=[];searches_by_request={};configurations={};intents={}
    original_search=active.store.search
    current_searches=[]
    def observed_search(**kwargs):
        current_searches.append({key:kwargs.get(key) for key in ('query','role','source_ids','document_revision_refs')})
        return original_search(**kwargs)
    active.store.search=observed_search
    # Give every company/event the same upper bounds and record the search calls
    # actually executed against the real local corpus. The source transport alone
    # is a fixture; the search implementation is not replaced.
    for business in businesses:
        for event_id in key_by_event:
            current_searches=[]
            task=base.model_copy(update={'request_id':'comparison-case-'+uuid4().hex,
                'operation':'analyze','company_id':business.company_id,'businesses':[business],
                'event_ids':[event_id],'budget':base.budget.model_copy(update={
                    'max_model_calls':3 if llm is not None or role_services else 0})})
            result=active.run(task)
            results.append(result)
            searches_by_request[result.request_id]=current_searches
            for config in result.configurations:configurations[config['company_id']]=config
            for intent in result.follow_up_intents:intents[intent.idempotency_key]=intent.model_dump(mode='json')
    rows=[]
    for result in results:
        selections={(entry.company_id,entry.coverage['package_id']):entry for entry in result.selections}
        for candidate in result.candidates:
            selection=selections.get((candidate.company_id,candidate.shared_package_ref))
            rows.append({'company_id':candidate.company_id,'event_key':key_by_event[candidate.versions.event_id],
                'priority':selection.score if selection else None,
                'selection':selection.model_dump(mode='json') if selection else None,
                'actual_searches':searches_by_request[result.request_id],
                'candidate':candidate.model_dump(mode='json')})
    return {'schema_version':'personalization-comparison-result-v1','classification':'synthetic_fixture',
        'sources_network_access':False,'model_requested':bool(llm or role_services),
        'inputs':data,'profiles':proposals,
        'configurations':list(configurations.values()),
        'automatic_optimization_intents':list(intents.values()),
        'results':rows,
        'executions':[{'request_id':result.request_id,'company_id':result.company_id,
            'status':result.status,'coverage':result.coverage} for result in results],
        'model_calls':sum(result.coverage.get('model_calls',0) for result in results),
        'quality_improvement_proven':False}
