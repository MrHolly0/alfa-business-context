from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from intelligence.comparison import run_comparison
from intelligence.workflow import DataTask, DataWorkflow
from intelligence.description_context import DescriptionPrepareRequest
from intelligence.api import create_app
from intelligence.artifacts import ArtifactStore
from intelligence.pipeline import AnalysisPipeline
from intelligence.collection import SourcePolicySnapshot


def test_same_events_change_priority_and_output_by_dependencies_not_sector(tmp_path):
    report=run_comparison(tmp_path/'state')
    rows={(row['company_id'],row['event_key']):row for row in report['results']}
    assert len(rows)==9 and report['model_calls']==0
    assert report['inputs']['companies'][0]['industry_hint']==report['inputs']['companies'][1]['industry_hint']
    expected=report['inputs']['expected']['dependency_signals']
    for event_key,companies in expected.items():
        for company in (item['company_id'] for item in report['inputs']['companies']):
            row=rows[company,event_key]
            if company in companies:
                assert row['priority']>=0.8
                assert any(claim['claim_id']=='company-applicability' for claim in row['candidate']['claims'])
            else:
                assert row['priority']<0.1
                assert not row['selection']['paths']
    assert '4 дня' in rows['instrument-port','port']['candidate']['summary']
    assert '7 дней' in rows['mixed-business','port']['candidate']['summary']
    mixed=report['profiles'][2]['proposal']
    assert len(mixed['context']['activities'])==3 and len(mixed['context']['dependencies'])==2
    assert any('Sirius' in item['text'] for item in mixed['hypotheses'])
    assert all('Sirius' not in dep['label'] for dep in mixed['context']['dependencies'])
    bank_queries={company:next(query['query'] for query in rows[company,'port']['actual_searches'] if query['role']=='bank')
        for company in ('instrument-port','instrument-grid','mixed-business')}
    assert len(set(bank_queries.values()))==3
    assert 'Северный порт' in bank_queries['instrument-port']
    assert 'Восточной электроподстанции' in bank_queries['instrument-grid']
    assert all(row['candidate']['status']=='partial' for row in rows.values())
    assert all(not any(claim['kind']=='bank_view' for claim in row['candidate']['claims']) for row in rows.values())


def test_new_description_data_runs_without_a_named_company_branch(tmp_path):
    source=Path(__file__).parents[1]/'examples/personalization-comparison.json'
    data=json.loads(source.read_text())
    data['tenant_id']='unseen-example'
    data['companies']=[{'company_id':'new-factory','name':'Новый цех','industry_hint':'новое направление',
        'description':'Компания выращивает водоросли.\nЗависит от: работы насосной станции Н-17.\nФакты: запас воды на 2 дня.'}]
    data['events']=[{'key':'pumping','title':'Насосная станция Н-17: план ремонта',
        'text':'Оператор сообщил, что насосная станция Н-17 остановлена на ремонт.'}]
    input_path=tmp_path/'input.json';input_path.write_text(json.dumps(data,ensure_ascii=False))
    report=run_comparison(tmp_path/'state',input_path=input_path)
    assert report['results'][0]['priority']>=0.8
    assert 'насосной станции' in report['results'][0]['candidate']['summary']


def test_description_job_and_http_prepare_are_proposals_not_saved_profiles(tmp_path):
    instant=datetime(2026,10,25,10,tzinfo=timezone.utc)
    request=DescriptionPrepareRequest(tenant_id='t',company_id='c',name='Компания',
        description='Компания производит датчики. Зависит от Северного порта.',provenance='questionnaire',
        as_of=instant,data_classification='synthetic',statement_status='core_confirmed')
    workflow=DataWorkflow(tmp_path/'state',clock=lambda:instant)
    task=DataTask(request_id='description-job',tenant_id='t',company_id='c',context_version=1,
        source_policy_version=1,operation='prepare_description',as_of=instant,
        source_policy=SourcePolicySnapshot(policy_id='none',revision=1,tenant_id='t',issued_at=instant),
        description_input=request,data_classification='synthetic',synthetic=True,budget={'max_model_calls':0})
    result=workflow.run(task)
    assert result.context_proposals and not result.configurations and not result.follow_up_intents
    assert result.coverage['context_saved'] is False and result.coverage['model_calls']==0
    client=TestClient(create_app(store=workflow.store,pipeline=AnalysisPipeline(workflow.store),
        data_workflow=workflow,artifact_store=ArtifactStore(tmp_path/'api.db'),service_token='test',tenant_ids={'t'}))
    response=client.post('/internal/v1/company-context/from-description',headers={'Authorization':'Bearer test'},
        json=request.model_dump(mode='json'))
    assert response.status_code==200
    assert response.json()['context']['dependencies'][0]['status']=='confirmed'
    denied=request.model_copy(update={'tenant_id':'other'})
    assert client.post('/internal/v1/company-context/from-description',headers={'Authorization':'Bearer test'},
        json=denied.model_dump(mode='json')).status_code==403
    with pytest.raises(ValueError,match='description_scope_mismatch'):
        DataTask.model_validate({**task.model_dump(),'description_input':denied})
