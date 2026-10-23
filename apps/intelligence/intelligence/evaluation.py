"""Repeatable groundedness checks, distinct from unmeasured model quality."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from .corpus import CorpusStore
from .schemas import AnalysisRequest


def evaluate_dataset(dataset, *, pipeline_factory):
    if dataset.get('schema_version') != 'evaluation-set-v1' or not isinstance(dataset.get('cases'),list):
        raise ValueError('invalid_evaluation_set')
    if not 1 <= len(dataset['cases']) <= 100: raise ValueError('case_count_limit')
    results=[]
    seen=set()
    for case in dataset['cases']:
        case_id=case['case_id']
        if not isinstance(case_id,str) or not case_id or case_id in seen: raise ValueError('duplicate_or_invalid_case_id')
        seen.add(case_id)
        with tempfile.TemporaryDirectory(prefix='business-context-eval-') as tmp:
            store=CorpusStore(Path(tmp)/'corpus.sqlite3')
            request=AnalysisRequest.model_validate(case['request'])
            for document in case.get('documents',[]):
                if document.get('tenant_id') != request.tenant_id: raise ValueError('evaluation_tenant_mismatch')
                store.ingest(document)
            candidate=pipeline_factory(store).run(request).model_dump(mode='json')
            evidence={e['evidence_id']:e for e in candidate['evidence']}
            claims=candidate['claims']
            expected=case.get('expected',{})
            checks={
                'evidence_ids_exist':all(ref in evidence for c in claims for ref in c['evidence_refs']),
                'quotes_match_hash':all(hashlib.sha256(e['quote'].encode()).hexdigest()==e['content_hash'] for e in evidence.values()),
                'source_scope':all(e['source_id'] in request.source_ids for e in evidence.values()),
                'fact_source_roles':all(all(evidence.get(ref,{}).get('source_role')==('bank' if c['kind']=='bank_view' else 'external') for ref in c['evidence_refs']) and bool(c['evidence_refs']) for c in claims if c['kind'] in {'external_fact','bank_view'}),
                'mode_explicit':candidate['execution_mode'] in {'baseline','model'},
                'budget':candidate['usage'].get('model_calls',0)<=request.budget.max_model_calls,
            }
            bank_unknown=any(c['kind']=='unknown' and c.get('origin')=='bank' for c in claims)
            if 'bank_unknown' in expected: checks['bank_unknown']=bank_unknown==expected['bank_unknown']
            hypotheses=[c for c in claims if c['kind']=='model_hypothesis']
            if candidate['execution_mode']=='model' and 'require_hypothesis' in expected:
                checks['personalization']=bool(hypotheses)==expected['require_hypothesis']
            if candidate['execution_mode']=='baseline': checks['baseline_abstains']=not hypotheses
            if candidate['execution_mode']=='model' and expected.get('require_context'): checks['company_grounds']=bool(hypotheses) and all(c['context_refs'] for c in hypotheses)
            checks['no_pipeline_errors']=not candidate['usage'].get('errors')
            results.append({'case_id':case_id,'passed':all(checks.values()),'checks':checks,'status':candidate['status'],
                            'execution_mode':candidate['execution_mode'],'usage':candidate['usage']})
    return {'schema_version':'evaluation-report-v1','synthetic':dataset.get('synthetic') is True,
            'purpose':'Проверка сценариев и ссылок. Не оценка качества на реальных данных.',
            'passed':all(r['passed'] for r in results),'case_count':len(results),
            'passed_cases':sum(r['passed'] for r in results),'cases':results}
