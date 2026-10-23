"""End-to-end checks across corpus, pipeline and real accounting adapter without network."""
import json
from decimal import Decimal
from pathlib import Path

from intelligence.cli import APP_ROOT, main, make_pipeline, read_json
from intelligence.corpus import CorpusStore
from intelligence.evaluation import evaluate_dataset
from intelligence.llm.config import ProviderConfig
from intelligence.llm.pricing import Pricing
from intelligence.llm.service import LLMService
from intelligence.llm.types import Completion, Usage
from intelligence.pipeline import AnalysisPipeline
from intelligence.schemas import AnalysisRequest


class EvidenceTransport:
    def __init__(self): self.calls=0
    def complete(self,messages,**kwargs):
        self.calls+=1
        payload=json.loads(messages[1]['content'].split('\n')[-1])
        evidence=payload['RETRIEVED_EVIDENCE']
        ext=next(e for e in evidence if e['source_role']=='external')
        company=payload['REQUEST_CONTEXT']['company']
        dep=next(d for d in company['dependencies'] if d['status']=='confirmed')
        claim={'claim_id':'hypothesis-1','kind':'model_hypothesis','text':'Задержка поставки может повлиять на производство при недостатке запасов.',
               'evidence_refs':[ext['evidence_id']],'context_refs':[dep['dependency_id']],
               'assumptions':['Запасов не хватит на период задержки.'],'alternatives':['Действующий запас покрывает задержку.'],
               'falsifiers':['Запасов достаточно на три недели.']}
        response={'summary':'Требуется проверить запасы и сроки нового заказа.','claims':[
            {'claim_id':'fact-1','kind':'external_fact','text':ext['quote'],'evidence_refs':[ext['evidence_id']],'context_refs':[],
             'assumptions':[],'alternatives':[],'falsifiers':[]},claim],
                  'influence_chain':[], 'clarifications':['На сколько дней хватит текущих запасов?']}
        return Completion(content=json.dumps(response,ensure_ascii=False),model='google/gemma-4-26b-a4b-it',
                          usage=Usage(prompt_tokens=100,completion_tokens=150,reported_cost=Decimal('0'),cost_currency='USD'),
                          finish_reason='stop',response_id='test-response')


def test_model_pipeline_uses_evidence_and_accounts_for_projected_cost(tmp_path):
    config=ProviderConfig(profile='openrouter',base_url='https://openrouter.ai/api/v1',api_key_env='UNUSED_TEST_KEY',
        model='google/gemma-4-26b-a4b-it:free',budget_scope='test-e2e',currency='USD',daily_limit=Decimal('0'),
        input_per_million=Decimal('0'),output_per_million=Decimal('0'),max_output_tokens=2048,timeout_seconds=10)
    transport=EvidenceTransport()
    llm=LLMService(config,ledger_path=tmp_path/'ledger.db',transport=transport,
        projected_hackathon_pricing=Pricing(Decimal('10'),Decimal('20'),'RUB'),projected_hackathon_model=config.model.removesuffix(':free'))
    store=CorpusStore(tmp_path/'corpus.db')
    request=AnalysisRequest.model_validate(read_json(APP_ROOT/'examples/analysis-request.json'))
    result=AnalysisPipeline(store,llm).run(request)
    assert result.execution_mode=='model'
    assert result.status=='partial'  # no bank material; never invent bank opinion
    assert not result.usage['errors']
    assert transport.calls==1
    assert any(c.kind=='model_hypothesis' and c.context_refs==['dep-aluminium'] for c in result.claims)
    attempt=result.usage['attempts'][0]
    assert Decimal(str(attempt['cost']))==0
    assert Decimal(str(attempt['projected_hackathon_cost_rub']))==Decimal('0.004')
    assert llm.ledger.summary('test-e2e','USD')['settled_count']==1


def test_offline_evaluation_does_not_measure_model_quality():
    report=evaluate_dataset(read_json(APP_ROOT/'evaluation/cases.json'),pipeline_factory=lambda store:AnalysisPipeline(store))
    assert report['passed']
    assert report['synthetic'] is True
    assert report['case_count']==5
    assert all(c['execution_mode']=='baseline' and c['usage']['model_calls']==0 for c in report['cases'])


def test_cli_demo_and_evaluation_write_json_without_credentials(tmp_path,monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY',raising=False)
    demo=tmp_path/'demo.json'
    assert main(['demo','--output',str(demo)])==0
    assert json.loads(demo.read_text())['execution_mode']=='baseline'
    output=tmp_path/'evaluation.json'
    assert main(['evaluate','--state-dir',str(tmp_path/'state'),'--output',str(output)])==0
    assert json.loads(output.read_text())['passed']


def test_cli_ingests_local_content_and_searches_scoped(tmp_path,capsys):
    document=read_json(APP_ROOT/'examples/documents.json')[0]
    metadata=tmp_path/'doc.json'
    metadata.write_text(json.dumps(document))
    text=tmp_path/'source.txt'; text.write_text('Аллюминий: учебный текст для локального импорта.')
    state=tmp_path/'state'
    assert main(['ingest','--input',str(metadata),'--file',str(text),'--state-dir',str(state)])==0
    capsys.readouterr()
    assert main(['search','--tenant','demo','--sources','bank-demo','--as-of','2026-10-24T12:00:00Z',
                 '--query','Аллюминий','--state-dir',str(state)])==0
    assert json.loads(capsys.readouterr().out)[0]['quote'].startswith('Аллюминий')
