"""Check discovery inputs and grounding boundaries; model meaning is reviewed separately."""
from datetime import datetime,timezone
import pytest
from intelligence.company_context import project_company_context,context_research_leads
from intelligence.pipeline import AnalysisPipeline
from intelligence.research_demo import run_research_cases
from intelligence.schemas import AnalysisRequest


def test_source_cases_retrieve_intermediate_documents_without_direct_profile_match(tmp_path):
    report=run_research_cases(tmp_path)
    by_id={row['case_id']:row for row in report['cases']}
    for case_id in ('glass-route','payment-certificate','route-refuted'):
        row=by_id[case_id]
        assert row['result']['selections'][0]['score']==0
        candidate=row['result']['candidates'][0]
        assert len({item['document_revision_ref'] for item in candidate['evidence']})>=2
        assert candidate['status']=='partial'
        assert any('не означает отсутствия риска' in question for question in candidate['clarifications'])
    negative=by_id['route-refuted']['result']['candidates'][0]
    assert any('Старый бюллетень' in item['quote'] for item in negative['evidence'])
    assert any('железной дороге' in item['quote'] for item in negative['evidence'])
    # The baseline does not pretend to have performed the semantic refutation.
    assert all(candidate['execution_mode']=='baseline' for row in report['cases'] for candidate in row['result']['candidates'])


def test_unconfirmed_dependency_triggers_real_search_and_questions_but_cannot_ground_impact(tmp_path):
    report=run_research_cases(tmp_path)
    row=next(item for item in report['cases'] if item['case_id']=='assumed-service')
    result=row['result'];selection=result['selections'][0];candidate=result['candidates'][0]
    assert selection['score']>0.1 and selection['paths']==[]
    assert selection['coverage']['matched_research_lead_ids']==['maybe-platform']
    assert any('TransitLeaf' in query['query'] for query in row['actual_searches'] if query['role']=='external')
    assert candidate['coverage']['confirmed_dependencies']==0
    assert any('TransitLeaf' in question for question in candidate['clarifications'])
    assert candidate['usage']['search_coverage']['unconfirmed_leads_are_evidence'] is False
    raw=row['input'];event=raw['documents'][0]
    request=AnalysisRequest(request_id='invalid-promotion',tenant_id=result['tenant_id'],company_id=raw['company_id'],
        as_of=report['inputs']['as_of'],context_version=1,source_policy_version=1,source_ids=['research-source'],
        company=raw['company'],synthetic=True,event={'event_id':'test','title':event['title'],'text':event['text'],
            'source_id':'research-source','published_at':report['inputs']['as_of']})
    evidence=[{'evidence_id':'ev','source_role':'external','source_id':'research-source','quote':event['text']}]
    payload={'summary':'Не допускается подтверждение связи','claims':[{'claim_id':'impact','kind':'model_hypothesis',
        'text':'Неподтверждённая платформа влияет на компанию.','context_refs':['maybe-platform'],'evidence_refs':['ev'],
        'alternatives':['Используется другой сервис.'],'falsifiers':['Сервис не используется.']}],
        'influence_chain':[],'clarifications':[]}
    with pytest.raises(ValueError,match='context_not_confirmed_or_active'):
        AnalysisPipeline._check_model_payload(payload,request,evidence)


def test_only_current_noncontradictory_research_leads_are_exposed():
    instant=datetime(2026,10,25,tzinfo=timezone.utc)
    def item(key,**changes):return {'assertion_id':key,'kind':'dependency','label':'Возможное использование '+key,
        'status':'assumption','provenance':'user hint',**changes}
    company={'name':'Example','assertions':[item('current'),item('contradicted',status='contradiction'),
        item('future',observed_at='2026-10-26T00:00:00Z'),item('expired',valid_until='2026-10-24T00:00:00Z'),
        item('negated',label='Не использует этот маршрут')]}
    leads=context_research_leads(project_company_context(company,as_of=instant))
    assert [item['item_id'] for item in leads]==['current']


def test_undated_lead_is_explicit_and_does_not_duplicate_unbounded_lineage(tmp_path):
    lead={"assertion_id":"x"*8000,"kind":"dependency","label":"Возможный сервис Example",
        "status":"unknown","provenance":"p"*8000,"evidence_refs":["r"*8000]*20}
    projection=project_company_context({"name":"Example","assertions":[lead]},
        as_of=datetime(2026,10,25,tzinfo=timezone.utc))
    result=context_research_leads(projection)[0]
    assert result["freshness_unknown"] is True and result["observed_at"] is None
    assert "provenance" not in result and "evidence_refs" not in result
    assert len(result["item_id"])<=256
    with pytest.raises(ValueError,match="trusted_Core_job"):
        run_research_cases(tmp_path,input_path=tmp_path/"untrusted.json",role_services={"analyst":object()})
