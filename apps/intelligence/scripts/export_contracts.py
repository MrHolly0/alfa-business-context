"""Export runtime-owned contracts. --check fails when committed schemas are stale."""
import argparse
import copy
import json
import tempfile
from pathlib import Path

from intelligence.api import create_app,DocumentInput,SearchInput
from intelligence.artifacts import ArtifactStore
from intelligence.corpus import CorpusStore
from intelligence.pipeline import AnalysisPipeline
from intelligence.relevance import RankRequest,RankResult
from intelligence.schemas import AnalysisRequest,CandidateAssessment,ModelAnswer

REPO=Path(__file__).resolve().parents[3]
DEST=REPO/'contracts/intelligence'


def contracts():
    output={name: model.model_json_schema() for name,model in [
        ('analysis-request.schema.json',AnalysisRequest),('candidate-assessment.schema.json',CandidateAssessment),
        ('model-answer.schema.json',ModelAnswer),('document.schema.json',DocumentInput),
        ('search.schema.json',SearchInput),('rank-request.schema.json',RankRequest),('rank-result.schema.json',RankResult)]}
    identifier={'type':'string','minLength':1,'maxLength':256}
    versions={'type':'object','additionalProperties':False,'required':['context_version','source_policy_version','event_versions'],
        'properties':{'context_version':{'type':'integer','minimum':1},'source_policy_version':{'type':'integer','minimum':1},
                      'event_versions':{'type':'array','minItems':1,'maxItems':1,'items':{'type':'object','additionalProperties':False,
                                       'required':['event_id','revision'],'properties':{'event_id':identifier,'revision':{'type':'integer','minimum':1}}}}}}
    def envelope(version):
        return {'type':'object','additionalProperties':False,'required':['schema_version','message_id','job_id','tenant_scope',
                    'company_id','trace_id','lease_generation','input_versions'],
                'properties':{'schema_version':{'const':version},'message_id':identifier,'job_id':identifier,'tenant_scope':identifier,
                              'company_id':identifier,'trace_id':identifier,'lease_generation':{'type':'integer','minimum':1},'input_versions':versions}}
    request=copy.deepcopy(output['analysis-request.schema.json'])
    job=envelope('analysis-job-v1')
    job['$defs']=request.pop('$defs',{})
    job['$defs']['AnalysisRequest']=request
    job['required']+=['deadline','lease_until','budget','scope','analysis']
    job['properties'].update({'deadline':{'type':'string','format':'date-time'},'lease_until':{'type':'string','format':'date-time'},
        'budget':{'type':'object','additionalProperties':False,'required':['max_model_calls','max_tokens','cost_reservation_rub'],
                  'properties':{'max_model_calls':{'type':'integer','minimum':0,'maximum':3},
                                'max_tokens':{'type':'integer','minimum':1,'maximum':60000},
                                'cost_reservation_rub':{'type':'number','minimum':0}}},
        'scope':{'type':'object','additionalProperties':False,'required':['source_ids','document_revision_refs'],
                 'properties':{'source_ids':{'type':'array','items':identifier,'minItems':1,'maxItems':100},
                               'document_revision_refs':{'type':'array','items':identifier,'maxItems':500}}},
        'analysis':{'$ref':'#/$defs/AnalysisRequest'}})
    candidate=copy.deepcopy(output['candidate-assessment.schema.json'])
    result=envelope('analysis-result-v1')
    result['$defs']=candidate.pop('$defs',{})
    result['$defs']['CandidateAssessment']=candidate
    result['required']+=['candidate','result_ref']
    result['properties'].update(candidate={'$ref':'#/$defs/CandidateAssessment'},result_ref={'type':['string','null']})
    output['analysis-job.schema.json']=job
    output['analysis-result.schema.json']=result
    with tempfile.TemporaryDirectory() as tmp:
        store=CorpusStore(Path(tmp)/'corpus.db')
        app=create_app(store=store,pipeline=AnalysisPipeline(store),artifact_store=ArtifactStore(Path(tmp)/'artifacts.db'),service_token='schema-only',tenant_ids={'demo'})
        spec=app.openapi()
        spec.setdefault('components',{}).setdefault('securitySchemes',{})['ServiceToken']={'type':'http','scheme':'bearer'}
        for path,operations in spec['paths'].items():
            if path.startswith('/internal/'):
                for operation in operations.values(): operation['security']=[{'ServiceToken':[]}]
        output['openapi.json']=spec
    for name,schema in output.items():
        if name != 'openapi.json': schema['$schema']='https://json-schema.org/draft/2020-12/schema'
    return output


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    stale=[]
    for name,content in contracts().items():
        text=json.dumps(content,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
        target=DEST/name
        if args.check:
            if not target.exists() or target.read_text()!=text: stale.append(name)
        else:
            target.parent.mkdir(parents=True,exist_ok=True); target.write_text(text)
    if stale: raise SystemExit('Stale contracts: '+', '.join(stale))


if __name__=='__main__': main()
