"""AI/Data execution artifacts. Core owns permissions, scheduling and publication."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from .schemas import StrictModel, CompanyContext, ProvenancedDependency, AnalysisBudget, AnalysisRequest, EventInput, CandidateAssessment, _aware
from .collection import NewsCollector, SourcePolicySnapshot
from .corpus import CorpusStore
from .events import EventStore, PublicationInput
from .research import DependencyEdge, PeriodObservation, ImpactSelection, select_affected_businesses, impact_candidates, analyze_period
from .analysts import PromptRegistry, CompanyAnalystStore, AnalystConfig
from .multi_agent import MultiAgentPipeline
from .watches import WatchCondition, WatchObservation, WatchEvaluation, evaluate_conditions
from .periods import PeriodPublication, analyze_period_research
from .artifacts import canonical_json, payload_hash

class WorkflowNotStarted(ValueError):
    """Capacity/lock refusal before analysis begins; safe to retry this owner."""
    def __init__(self, code):
        if code not in {"capacity_exhausted", "request_in_progress"}:
            raise ValueError("invalid_not_started_code")
        self.code = code
        super().__init__(code)


class BusinessSnapshot(StrictModel):
    company_id: str = Field(min_length=1,max_length=256)
    context_version: int = Field(ge=1)
    company: CompanyContext
    dependency_graph: list[DependencyEdge] = Field(default_factory=list,max_length=300)

class PublishedAssessment(StrictModel):
    """Core acknowledgement that a particular assessment signature was published."""
    company_id: str = Field(min_length=1,max_length=256)
    event_id: str = Field(min_length=1,max_length=256)
    input_signature: str = Field(min_length=1,max_length=128)
    assessment_revision: int = Field(ge=1)

class PeriodSpec(StrictModel):
    start: datetime
    end: datetime
    mode: Literal['retrospective','as_known']
    observations: list[PeriodObservation] = Field(default_factory=list,max_length=5000)
    @model_validator(mode='after')
    def dates(self):
        _aware(self.start); _aware(self.end)
        if self.end < self.start: raise ValueError('invalid_period')
        return self

class DataTask(StrictModel):
    schema_version: Literal['ai-data-task-v1'] = 'ai-data-task-v1'
    request_id: str = Field(min_length=1,max_length=256)
    tenant_id: str = Field(min_length=1,max_length=256)
    company_id: str = Field(min_length=1,max_length=256)
    context_version: int = Field(ge=1)
    source_policy_version: int = Field(ge=1)
    operation: Literal['collect','analyze','reassess','period','onboard'] = 'analyze'
    trigger: Literal['scheduled','manual','context_changed','policy_changed','evidence_changed','watch_due'] = 'manual'
    knowledge_mode: Literal['as_of','latest'] = 'as_of'
    as_of: datetime
    source_policy: SourcePolicySnapshot
    businesses: list[BusinessSnapshot] = Field(default_factory=list,max_length=20)
    event_ids: list[str] = Field(default_factory=list,max_length=30)
    period: PeriodSpec | None = None
    watches: list[WatchCondition] = Field(default_factory=list,max_length=300)
    watch_observations: list[WatchObservation] = Field(default_factory=list,max_length=5000)
    published_assessments: list[PublishedAssessment] = Field(default_factory=list,max_length=600)
    budget: AnalysisBudget = Field(default_factory=AnalysisBudget)
    data_classification: Literal['synthetic','public','confidential'] = 'confidential'
    synthetic: bool = False
    max_events: int = Field(default=10,ge=1,le=30)
    @model_validator(mode='after')
    def snapshot(self):
        _aware(self.as_of)
        if self.source_policy.tenant_id != self.tenant_id or self.source_policy.revision != self.source_policy_version: raise ValueError('policy_snapshot_mismatch')
        ids=[x.company_id for x in self.businesses]
        if len(ids)!=len(set(ids)): raise ValueError('duplicate_business')
        if self.businesses and (self.company_id not in ids or next(x.context_version for x in self.businesses if x.company_id==self.company_id)!=self.context_version): raise ValueError('primary_company_snapshot_mismatch')
        allowed_companies=set(ids)|{self.company_id}
        if any(item.company_id not in allowed_companies for item in [*self.watches,*self.watch_observations]): raise ValueError('watch_company_scope_mismatch')
        if self.operation=='period' and self.period is None: raise ValueError('period_required')
        if self.operation=='period' and self.knowledge_mode!='as_of': raise ValueError('period_requires_as_of_knowledge_mode')
        if self.synthetic and self.data_classification!='synthetic': raise ValueError('synthetic_classification_mismatch')
        return self

class DataResult(StrictModel):
    schema_version: Literal['ai-data-result-v1'] = 'ai-data-result-v1'
    request_id: str
    tenant_id: str
    company_id: str
    status: Literal['completed','partial','no_change','unknown']
    versions: dict
    collection: dict | None = None
    events: list[dict] = Field(default_factory=list)
    selections: list[ImpactSelection] = Field(default_factory=list)
    invalidations: list[dict] = Field(default_factory=list)
    candidates: list[CandidateAssessment] = Field(default_factory=list)
    revisions: list[dict] = Field(default_factory=list)
    configurations: list[dict] = Field(default_factory=list)
    period_report: dict | None = None
    watch_results: list[WatchEvaluation] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)
    trace: list[dict] = Field(default_factory=list)
    created_at: datetime

class WorkflowState:
    def __init__(self,path):
        self.path=str(path)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS workflow_stages(tenant TEXT,request TEXT,digest TEXT,stage TEXT,payload TEXT,PRIMARY KEY(tenant,request,stage));
            CREATE TABLE IF NOT EXISTS workflow_analyses(tenant TEXT,company TEXT,event TEXT,signature TEXT,payload TEXT,PRIMARY KEY(tenant,company,event));
            CREATE TABLE IF NOT EXISTS workflow_results(tenant TEXT,request TEXT,digest TEXT,payload TEXT,PRIMARY KEY(tenant,request));''')
    def db(self): return sqlite3.connect(self.path,timeout=15)
    def stage(self,task,name,payload):
        with self.db() as db: db.execute('INSERT OR REPLACE INTO workflow_stages VALUES(?,?,?,?,?)',(task.tenant_id,task.request_id,payload_hash(task.model_dump(mode='json')),name,canonical_json(payload)))
    def prior(self,tenant,company,event):
        with self.db() as db: row=db.execute('SELECT payload FROM workflow_analyses WHERE tenant=? AND company=? AND event=?',(tenant,company,event)).fetchone()
        return json.loads(row[0]) if row else None
    def save_analysis(self,tenant,company,event,signature,payload):
        with self.db() as db: db.execute('INSERT OR REPLACE INTO workflow_analyses VALUES(?,?,?,?,?)',(tenant,company,event,signature,canonical_json(payload)))
    def cached(self,task):
        with self.db() as db: row=db.execute('SELECT digest,payload FROM workflow_results WHERE tenant=? AND request=?',(task.tenant_id,task.request_id)).fetchone()
        if row and row[0]!=payload_hash(task.model_dump(mode='json')): raise ValueError('idempotency_conflict')
        return DataResult.model_validate_json(row[1]) if row else None
    def save_result(self,task,result):
        with self.db() as db: db.execute('INSERT INTO workflow_results VALUES(?,?,?,?)',(task.tenant_id,task.request_id,payload_hash(task.model_dump(mode='json')),result.model_dump_json()))

class _TaskRoleLLM:
    def __init__(self,session,service):
        self.session,self.service=session,service
        self.config=getattr(service,"config",None)
        self.ledger=getattr(service,"ledger",None)
    def complete(self,messages,**kwargs):
        return self.session.complete_with(self.service,messages,**kwargs)


class _TaskLLM:
    """One conservative call/token allowance shared by every route and business."""
    def __init__(self,service,budget,check,role_services=None):
        self.service=service
        self.budget,self.check=budget,check
        self.role_services=dict(role_services or {})
        self.config=getattr(service,"config",None)
        self.ledger=getattr(service,"ledger",None)
        self.calls,self.tokens=0,0
        self.attempts=[]
    def for_role(self,role):
        service=self.role_services.get(role,self.service)
        return _TaskRoleLLM(self,service) if service is not None else None
    def complete(self,messages,**kwargs):
        return self.complete_with(self.service,messages,**kwargs)
    def complete_with(self,service,messages,**kwargs):
        from .llm.service import estimate_text_tokens
        if service is None:raise ValueError("role_service_unavailable")
        self.check('before_model')
        estimate=estimate_text_tokens(messages)
        configured_output=getattr(getattr(service,"config",None),"max_output_tokens",self.budget.max_output_tokens)
        out=min(kwargs.get('max_output_tokens',self.budget.max_output_tokens),self.budget.max_output_tokens,configured_output)
        remaining=self.budget.max_total_tokens-self.tokens
        out=min(out,remaining-estimate)
        input_limit=min(kwargs.get('max_input_tokens',self.budget.max_input_tokens),self.budget.max_input_tokens)
        if self.calls>=self.budget.max_model_calls or out<1 or self.tokens+estimate+out>self.budget.max_total_tokens: raise ValueError('task_model_budget_exhausted')
        if estimate>input_limit: raise ValueError('task_input_limit')
        self.calls+=1; self.tokens+=estimate+out
        try:
            response=service.complete(messages,max_input_tokens=input_limit,max_output_tokens=out,json_mode=kwargs.get('json_mode',True))
        except Exception as exc:
            self.attempts.append({'model':getattr(getattr(service,'config',None),'model',None),'error':getattr(exc,'code',type(exc).__name__),'billing_uncertain':getattr(exc,'billing_uncertain',False)})
            raise
        self.attempts.append({'model':response.get('model'),'cost':str(response.get('cost')) if response.get('cost') is not None else None,
            'currency':response.get('cost_currency'),'cost_source':response.get('cost_source'),'usage':response.get('usage')})
        self.check('after_model')
        return response

class DataWorkflow:
    def __init__(self,state_dir,*,llm=None,role_services=None,analyst_registry=None,collector=None,max_concurrency=2):
        self.state_dir=Path(state_dir); self.state_dir.mkdir(parents=True,exist_ok=True)
        self.store=CorpusStore(self.state_dir/'corpus.sqlite3')
        self.events=EventStore(self.state_dir/'events.sqlite3')
        self.state=WorkflowState(self.state_dir/'workflow.sqlite3')
        self.llm=llm
        self.role_services=dict(role_services or {})
        # Operator-approved analyst templates are shared. CompanyAnalystStore below
        # still writes only tenant/company-scoped private configurations.
        self.runtime_fingerprint=payload_hash({
            "modules":{file.name:hashlib.sha256(file.read_bytes()).hexdigest() for file in Path(__file__).parent.glob("*.py")},
            "prompts":{file.name:hashlib.sha256(file.read_bytes()).hexdigest() for file in (Path(__file__).parent/'prompts').glob("*.txt")}})
        self.registry=analyst_registry or PromptRegistry(self.state_dir/'analysts.sqlite3')
        self.collector=collector or NewsCollector(self.store,state_path=self.state_dir/'collection.sqlite3',raw_dir=self.state_dir/'raw'/'collection')
        self.max_concurrency=max(1,min(4,max_concurrency))
        self.slots=threading.BoundedSemaphore(self.max_concurrency)
        self.execution_locks=self.state_dir/'execution-locks'
        self.execution_locks.mkdir(parents=True,exist_ok=True)
    def run(self,task,checkpoint=None):
        task=task if isinstance(task,DataTask) else DataTask.model_validate(task)
        check=checkpoint or (lambda stage:None); check('before_workflow')
        slot_lock=self._acquire_slot_lock()
        if slot_lock is None:raise WorkflowNotStarted('capacity_exhausted')
        request_key=hashlib.sha256(f'{task.tenant_id}\0{task.request_id}'.encode()).hexdigest()
        request_lock=None
        local_slot=False
        try:
            request_lock=self._lock_file(self.execution_locks/f'request-{request_key}.lock')
            if request_lock is None:raise WorkflowNotStarted('request_in_progress')
            local_slot=self.slots.acquire(blocking=False)
            if not local_slot:raise WorkflowNotStarted('capacity_exhausted')
            return self._run(task,check)
        finally:
            if local_slot:self.slots.release()
            if request_lock is not None:self._release_file_lock(request_lock)
            self._release_file_lock(slot_lock)

    def _acquire_slot_lock(self):
        for index in range(self.max_concurrency):
            handle=self._lock_file(self.execution_locks/f'slot-{index}.lock')
            if handle is not None:return handle
        return None

    @staticmethod
    def _lock_file(path):
        handle=open(path,'a+b')
        try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close();return None
        return handle

    @staticmethod
    def _release_file_lock(handle):
        try:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
        finally:handle.close()
    def _run(self,task,check):
        cached=self.state.cached(task)
        if cached is not None:check('cached_result');return cached
        policy=task.source_policy; allowed=policy.effective_source_ids(); trace=[]
        def stage(name,payload):
            check(name);self.state.stage(task,name,payload);trace.append({'stage':name,**payload})
        result=DataResult(request_id=task.request_id,tenant_id=task.tenant_id,company_id=task.company_id,status='completed',versions={'context':task.context_version,'source_policy':policy.revision},created_at=datetime.now(timezone.utc))
        # Confidential snapshots execute locally. No model payload is built or sent for them.
        model_allowed=task.data_classification in {'synthetic','public'}
        model=_TaskLLM(self.llm,task.budget,check,self.role_services) if model_allowed and (self.llm or self.role_services) else None
        if (self.llm or self.role_services) and not model_allowed:
            result.coverage['model_limitation']='confidential_external_model_disabled'
        if task.operation=='collect':
            stage('before_collection',{'source_count':len(allowed)})
            result.collection=self.collector.collect(policy,now=datetime.now(timezone.utc),checkpoint=check)
            stage('collection_complete',{'report_saved':True})
        analysis_as_of=(datetime.now(timezone.utc) if task.knowledge_mode=='latest' else task.as_of)
        if task.period and task.period.mode=='retrospective': analysis_as_of=datetime.now(timezone.utc)
        docs=self.store.list_documents(tenant_id=task.tenant_id,source_ids=allowed,as_of=analysis_as_of.isoformat(),policy=policy,as_known=bool(task.period and task.period.mode=='as_known'))
        docs=[d for d in docs if not d['canonical_uri'].startswith('urn:event:')]
        refs=[d['document_revision_ref'] for d in docs]
        if model and any(document.get('data_classification','confidential') not in {'public','synthetic'} for document in docs):
            model=None
            result.coverage['model_limitation']='persisted_source_classification_denied'
        result.watch_results=evaluate_conditions(task.watches,as_of=analysis_as_of,
            context_versions={item.company_id:item.context_version for item in task.businesses},
            document_versions={document['document_id']:document['document_revision_ref'] for document in docs},observations=task.watch_observations)

        stage('permitted_snapshot',{'documents':len(refs),'policy_version':policy.revision,
            'knowledge_mode':task.knowledge_mode,'analysis_as_of':analysis_as_of.isoformat()})
        events={};event_document_refs={}
        for doc in docs:
            if doc['source_role']!='external' or not doc['canonical_uri'].startswith(('https://','http://')):continue
            check('event_ingest')
            data={k:doc[k] for k in PublicationInput.model_fields if k in doc and doc[k] is not None}
            event=self.events.ingest(data);event_id=event['event_id'];events[event_id]=event
            if doc.get('document_revision_ref'):
                event_document_refs.setdefault(event_id,set()).add(doc['document_revision_ref'])
        if task.event_ids:events={k:v for k,v in events.items() if k in task.event_ids}
        if task.operation=='period':
            publications=[PeriodPublication(event_id=next((event_id for event_id,event_refs in event_document_refs.items() if document['document_revision_ref'] in event_refs),document['document_id']),
                revision=document['revision'],event_time=document.get('event_time'),published_at=document['published_at'],server_received_at=document.get('received_at'),
                source_id=document['source_id'],document_revision_ref=document['document_revision_ref'],title=document['title'],quote=document['content'][:1000]) for document in docs if document['content'] and document['source_role']=='external']
            report=analyze_period_research(task.period.observations,period_start=task.period.start,period_end=task.period.end,known_at=task.as_of,mode=task.period.mode,publications=publications)
            result.period_report={**report.metrics.model_dump(mode='json'),'publication_timeline':[item.model_dump(mode='json') for item in report.publication_timeline],
                'research_coverage':report.coverage,'limitations':report.limitations}
            result.coverage['document_revision_refs']=refs
            stage('period_complete',{'metrics':len(report.metrics.metrics),'publications':len(report.publication_timeline)})
        else:
            personal={}
            for business in task.businesses:
                folder=self.company_dir(task.tenant_id,business.company_id)
                registry=self.registry;configs=CompanyAnalystStore(folder/'company-analysts.sqlite3',registry)
                record=configs.onboard(task.tenant_id,business.company_id,business.context_version,business.company)
                personal[business.company_id]=(registry,record)
                result.configurations.append({**record,
                    'config':record['config'].model_dump(mode='json'),
                    'source_config_ref':record['history'][-1]['source_config_ref'] if record.get('history') else None})
            if task.operation!='onboard':
                for event_id in list(events)[:task.max_events]:
                    # Shared packages stay in local artifacts; only the existing pipeline's
                    # explicitly classification-gated summary may be sent to an allowed model.
                    event_refs=sorted(event_document_refs.get(event_id,set()))
                    if not event_refs:continue
                    package=self.events.build_impact_package(task.tenant_id,event_id,source_policy_id=policy.policy_id,policy_version=str(policy.revision),allowed_source_ids=allowed,allowed_document_revision_refs=event_refs,llm=model.for_role('extractor') if model else None)
                    result.events.append(package.model_dump(mode='json'))
                    source=next((d for d in docs if d['document_revision_ref'] in {s.document_revision_ref for s in package.sources}),None)
                    if source is None:continue
                    selections=select_affected_businesses(package,task.businesses,as_of=analysis_as_of)
                    result.selections.extend(selections)
                    by_id={business.company_id:business for business in task.businesses}
                    for selection in selections:
                        business=by_id[selection.company_id]
                        business_model=model
                        if task.trigger=='scheduled' and selection.score<0.10:
                            business_model=None
                            stage('business_model_not_selected',{'company_id':business.company_id,'event_id':event_id,'reason':'no_sufficient_signal_in_available_snapshot','fallback':'local_baseline_with_coverage'})
                        check('before_business');registry,record=personal[business.company_id]
                        config=record['config'] if isinstance(record['config'],AnalystConfig) else AnalystConfig.model_validate(record['config'])
                        paths=impact_candidates(package,business.company_id,business.dependency_graph,as_of=analysis_as_of.isoformat(),max_hops=config.max_depth)
                        bank_refs=self._relevant_bank_refs(task,business,allowed,refs,config,as_of=analysis_as_of)
                        event_semantics=sorted({(source.content_hash,source.origin_group_id,source.source_role,
                                                  source.title,source.publisher,source.author)
                                                 for source in package.sources})
                        policy_signature=policy.model_dump(mode='json');policy_signature.pop('issued_at',None)
                        analysis_company=self._analysis_company(business)
                        def model_identity(service):
                            configured=getattr(service,'config',None)
                            return {name:str(getattr(configured,name,None)) for name in ('profile','model','max_output_tokens','reasoning_effort','input_per_million','output_per_million')}
                        route_models={role:model_identity(service) for role,service in self.role_services.items()}
                        active_context_ids=sorted(item.dependency_id for item in analysis_company.dependencies
                            if self._context_active(item,analysis_as_of))
                        active_fact_ids=sorted(item.fact_id for item in analysis_company.facts
                            if self._context_active(item,analysis_as_of))
                        inputs={'event_id':event_id,'event_semantics':event_semantics,
                            'context_version':business.context_version,
                            'company_snapshot':business.company.model_dump(mode='json'),
                            'analysis_context':analysis_company.model_dump(mode='json'),
                            'dependency_graph':[edge.model_dump(mode='json') for edge in business.dependency_graph],
                            'active_context_ids':active_context_ids,'active_fact_ids':active_fact_ids,
                            'policy':policy_signature,'relevant_bank_refs':bank_refs,
                            'config_ref':record['config_ref'],'config':config.model_dump(mode='json'),
                            'model':{'default':model_identity(self.llm),**route_models},'runtime':self.runtime_fingerprint,
                            'data_classification':task.data_classification,'knowledge_mode':task.knowledge_mode,
                            'workflow':'ai-data-v1'}
                        signature=payload_hash(inputs);previous=self.state.prior(task.tenant_id,business.company_id,event_id)
                        analysis_request_id=hashlib.sha256(f'{task.request_id}:{business.company_id}:{event_id}'.encode()).hexdigest()
                        force_refresh=task.trigger in {'manual','watch_due'}
                        proof=next((item for item in task.published_assessments if item.company_id==business.company_id
                                    and item.event_id==event_id and item.input_signature==signature),None)
                        if proof and not force_refresh:
                            result.revisions.append({'company_id':business.company_id,'event_id':event_id,
                                'input_signature':signature,'assessment_revision':proof.assessment_revision,
                                'status':'no_change','publication_revision_required':False});continue
                        same_request_resume=bool(previous and previous.get('candidate',{}).get('request_id')==analysis_request_id)
                        if previous and previous['signature']==signature and (not force_refresh or same_request_resume) and self._prior_candidate_is_in_scope(
                                previous,refs,event_refs,allowed):
                            candidate=CandidateAssessment.model_validate(previous['candidate'])
                            prior_request_id=candidate.request_id
                            candidate.request_id=hashlib.sha256(f'{task.request_id}:{business.company_id}:{event_id}'.encode()).hexdigest()
                            candidate.created_at=datetime.now(timezone.utc)
                            candidate.versions.event_revision=package.revision
                            candidate.shared_package_ref=package.package_id
                            candidate.coverage.independent_origins=package.coverage.independent_origin_groups
                            candidate.coverage.origin_groups=package.coverage.origin_group_count
                            candidate.coverage.independence_status=package.coverage.independence_status
                            candidate.trace.append({'stage':'persisted_candidate_reused','reason':'publication_not_proven'})
                            changes={'status':'changed','publication_revision_required':True,
                                'reason':'publication_not_proven','changed_inputs':[],
                                'input_signature':signature,'previous_request_id':prior_request_id}
                            candidate.changes=changes
                            result.candidates.append(candidate);result.revisions.append({'company_id':business.company_id,
                                'event_id':event_id,**changes})
                            self.state.save_analysis(task.tenant_id,business.company_id,event_id,signature,
                                {'signature':signature,'inputs':inputs,'candidate':candidate.model_dump(mode='json')})
                            stage('personal_reused',{'company_id':business.company_id,'event_id':event_id,
                                'status':'candidate_reused_publication_unconfirmed'})
                            continue
                        request=AnalysisRequest(request_id=analysis_request_id,tenant_id=task.tenant_id,company_id=business.company_id,as_of=analysis_as_of,context_version=business.context_version,source_policy_version=policy.revision,source_ids=allowed,document_revision_refs=refs,company=analysis_company,event=EventInput(document_revision_ref=source['document_revision_ref'],event_id=event_id,revision=package.revision,title=source['title'],text=source['content'],source_id=source['source_id'],published_at=source['published_at'],synthetic=source['synthetic']),budget=task.budget,synthetic=task.synthetic)
                        if business_model:
                            request=request.model_copy(update={'budget':request.budget.model_copy(update={'max_model_calls':max(0,min(3,task.budget.max_model_calls-business_model.calls))})})
                        analyst_service=business_model.for_role('analyst') if business_model else None
                        role_adapters={role:business_model.for_role(role) for role in ('researcher','analyst','critic')} if business_model else {}
                        # The common package already supplies event terms. Use the bounded
                        # local research plan, preserving calls for analysis/repair/critique.
                        if business_model: role_adapters['researcher']=None
                        candidate=MultiAgentPipeline(self.store,analyst_service,registry=registry,
                            analyst_config=config,role_services=role_adapters).run(request,checkpoint=check,shared_event_package=package,data_classification=task.data_classification)
                        if selection.status=='no_signal_found':
                            # A valid quote/ID and a critic's agreement cannot establish
                            # a missing event-to-company relationship.
                            if candidate.status=='ready': candidate.status='partial'
                            candidate.coverage.limitations=list(dict.fromkeys(candidate.coverage.limitations+['business_relevance_not_established']))
                            candidate.clarifications=list(dict.fromkeys(candidate.clarifications+[
                                'Какая подтверждённая зависимость компании связана с этим событием? В переданном контексте такая связь не найдена.']))[:30]
                        candidate.shared_package_ref=package.package_id
                        candidate.coverage.independent_origins=package.coverage.independent_origin_groups
                        candidate.coverage.origin_groups=package.coverage.origin_group_count
                        candidate.coverage.independence_status=package.coverage.independence_status
                        candidate.coverage.checked_at=datetime.now(timezone.utc)
                        candidate.coverage.limitations=list(dict.fromkeys(candidate.coverage.limitations+package.coverage.limitations))
                        candidate.watch_conditions=list(dict.fromkeys([x for c in candidate.claims for x in c.falsifiers+c.confirmation_conditions]))[:50]
                        candidate.trace.append({'stage':'dependency_paths','paths':[p.model_dump(mode='json') for p in paths]})
                        changes=self.changes(previous,candidate,inputs);changes['input_signature']=signature;candidate.changes=changes
                        result.candidates.append(candidate);result.revisions.append({'company_id':business.company_id,'event_id':event_id,**changes})
                        self.state.save_analysis(task.tenant_id,business.company_id,event_id,signature,{'signature':signature,'inputs':inputs,'candidate':candidate.model_dump(mode='json')})
                        stage('personal_complete',{'company_id':business.company_id,'event_id':event_id,'status':candidate.status})
        for proof in task.published_assessments:
            # A scoped request only observes the requested events. Absence of any
            # other published event from this subset says nothing about its current
            # policy/time eligibility, so do not recommend invalidation for it.
            if proof.event_id not in events and (not task.event_ids or proof.event_id in task.event_ids):
                result.invalidations.append({'company_id':proof.company_id,'event_id':proof.event_id,
                    'assessment_revision':proof.assessment_revision,'reason':'event_has_no_evidence_in_current_policy_and_time_snapshot',
                    'source_policy_version':policy.revision,'action':'recheck_or_withdraw_current_basis','retain_history':True})
        result.coverage.update(runtime_fingerprint=self.runtime_fingerprint,source_ids=allowed,documents=len(docs),external_search='configured_sources_and_corpus_only',
            knowledge_mode=task.knowledge_mode,requested_as_of=task.as_of.isoformat(),analysis_as_of=analysis_as_of.isoformat(),
            model_calls=model.calls if model else 0,reserved_tokens=model.tokens if model else 0,model_attempts=model.attempts if model else [])
        if not result.candidates and result.revisions and all(r['status']=='no_change' for r in result.revisions):result.status='no_change'
        elif any(c.status!='ready' for c in result.candidates):result.status='partial'
        elif not result.candidates and task.operation not in {'onboard','period'}:result.status='unknown'
        result.trace=trace;check('before_result_cache');self.state.save_result(task,result)
        return result
    def company_dir(self,tenant,company):
        folder=self.state_dir/'companies'/hashlib.sha256(tenant.encode()).hexdigest()/hashlib.sha256(company.encode()).hexdigest()
        folder.mkdir(parents=True,exist_ok=True);return folder

    @staticmethod
    def _analysis_company(business):
        """Combine Core business context and its separately-provenanced graph for analysis."""
        company=business.company
        dependencies=list(company.dependencies)
        known={item.dependency_id for item in dependencies}|{fact.fact_id for fact in company.facts}
        for edge in business.dependency_graph:
            if edge.edge_id in known:
                raise ValueError('duplicate_business_context_id')
            known.add(edge.edge_id)
            dependencies.append(ProvenancedDependency(
                dependency_id=edge.edge_id,
                label=f'{edge.subject} → {edge.object}',
                kind=edge.predicate,
                status='confirmed' if edge.status=='confirmed' else 'hypothesis',
                provenance=f'{edge.evidence_basis}: {edge.evidence_ref}; source status: {edge.status}',
                source_node=edge.subject,target_node=edge.object,evidence_refs=[edge.evidence_ref],
                valid_from=datetime.fromisoformat(edge.valid_from) if edge.valid_from else None,
                valid_until=datetime.fromisoformat(edge.valid_to) if edge.valid_to else None))
        return company.model_copy(update={'dependencies':dependencies})

    @staticmethod
    def _context_active(item,as_of):
        valid_from=getattr(item,'valid_from',None)
        valid_until=getattr(item,'valid_until',None)
        return not ((valid_from is not None and valid_from>as_of) or
                    (valid_until is not None and valid_until<as_of))

    def _relevant_bank_refs(self,task,business,allowed,document_refs,config,*,as_of=None):
        as_of=as_of or task.as_of
        bank_refs=[doc['document_revision_ref'] for doc in self.store.list_documents(
            tenant_id=task.tenant_id,source_ids=allowed,as_of=as_of.isoformat(),
            policy=task.source_policy,as_known=bool(task.period and task.period.mode=='as_known'))
            if doc['source_role']=='bank' and doc['document_revision_ref'] in set(document_refs)]
        if not bank_refs:return []
        terms=[d.label for d in business.company.dependencies if d.status=='confirmed']
        terms.extend(fact.text for fact in business.company.facts)
        terms.extend([business.company.name,business.company.industry,business.company.role])
        query=' '.join(terms)[:320]
        if not query:return []
        evidence=self.store.search(tenant_id=task.tenant_id,source_ids=allowed,as_of=as_of.isoformat(),
            query=query,document_revision_refs=bank_refs,role='bank',
            limit=min(16,max(4,getattr(config,'evidence_limit',16))))
        return sorted({(item['evidence_id'],item['content_hash'],item['document_revision_ref'])
                       for item in evidence})

    @staticmethod
    def _prior_candidate_is_in_scope(previous,permitted_refs,event_refs,allowed_source_ids):
        try:
            candidate=CandidateAssessment.model_validate(previous['candidate'])
        except Exception:
            return False
        permitted=set(permitted_refs);event_permitted=set(event_refs)
        event_ref=candidate.versions.event_document_revision_ref
        if event_ref not in event_permitted:return False
        for evidence in candidate.evidence:
            if evidence.document_revision_ref not in permitted or evidence.source_id not in set(allowed_source_ids):
                return False
        return True

    @staticmethod
    def changes(previous,candidate,inputs):
        if not previous:return {'status':'changed','publication_revision_required':True,'reason':'first_assessment','changed_inputs':list(inputs)}
        changed=[k for k,v in inputs.items() if previous['inputs'].get(k)!=v]
        old=previous['candidate'];new=candidate.model_dump(mode='json')
        different=any(old.get(k)!=new.get(k) for k in ('summary','claims','influence_chain','clarifications','watch_conditions')) or any(k in changed for k in ('policy','event_revision','context_version','as_of_day','active_context_ids','active_fact_ids'))
        return {'status':'changed' if different else 'no_change','publication_revision_required':different,'changed_inputs':changed,'previous_request_id':old.get('request_id')}
