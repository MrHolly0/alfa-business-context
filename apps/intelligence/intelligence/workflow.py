"""AI/Data execution artifacts. Core owns permissions, scheduling and publication."""
from __future__ import annotations
import hashlib
import json
from .storage import Database
import threading
import time
from uuid import uuid4
import os
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
from .personalization import FollowUpIntent
from .description_context import DescriptionPrepareRequest, DescriptionProposal, prepare_description

class WorkflowNotStarted(ValueError):
    """Capacity/lock refusal before analysis begins; safe to retry this owner."""
    def __init__(self, code):
        if code not in {"capacity_exhausted", "request_in_progress", "execution_recovery_required"}:
            raise ValueError("invalid_not_started_code")
        self.code = code
        super().__init__(code)


class WorkflowSnapshotConflict(ValueError):
    """A newer Core snapshot was already observed; this result cannot be current."""


class BusinessPreferences(StrictModel):
    """Request preferences never replace facts or immutable evidence requirements."""
    role: str | None = Field(default=None, max_length=1000)
    answer_style: Literal['concise', 'balanced', 'detailed'] | None = None
    horizon: Literal['short', 'quarter', 'long'] | None = None


class BusinessSnapshot(StrictModel):
    company_id: str = Field(min_length=1,max_length=256)
    context_version: int = Field(ge=1)
    company: CompanyContext
    preferences: BusinessPreferences | None = None
    known_at: datetime | None = None
    dependency_graph: list[DependencyEdge] = Field(default_factory=list,max_length=300)
    @model_validator(mode='after')
    def knowledge_timestamp(self):
        if self.known_at is not None:_aware(self.known_at)
        return self

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
    operation: Literal['collect','analyze','reassess','period','onboard','optimize','rollback_config','prepare_description'] = 'analyze'
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
    max_exploratory_analyses: int = Field(default=1,ge=0,le=3)
    auto_optimize: bool = True
    description_input: DescriptionPrepareRequest | None = None
    expected_config_revisions: dict[str, int] = Field(default_factory=dict, max_length=20)
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
        if self.operation=='prepare_description':
            if self.description_input is None:raise ValueError('description_input_required')
            if (self.description_input.tenant_id!=self.tenant_id or self.description_input.company_id!=self.company_id
                or self.description_input.as_of!=self.as_of or self.description_input.data_classification!=self.data_classification):
                raise ValueError('description_scope_mismatch')
            if self.businesses:raise ValueError('description_proposal_does_not_replace_business_snapshot')
        elif self.description_input is not None:raise ValueError('description_input_only_for_prepare_description')
        if self.operation in {'optimize', 'rollback_config'}:

            if not ids or set(self.expected_config_revisions) != set(ids):
                raise ValueError('expected_config_revisions_required')
            if any(type(value) is not int or value < 1 for value in self.expected_config_revisions.values()):
                raise ValueError('invalid_expected_config_revision')
        elif self.expected_config_revisions:
            raise ValueError('config_revisions_only_for_configuration_operations')
        if any(proof.company_id not in allowed_companies for proof in self.published_assessments):
            raise ValueError('published_assessment_company_scope_mismatch')
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
    context_proposals: list[DescriptionProposal] = Field(default_factory=list, max_length=1)
    follow_up_intents: list[FollowUpIntent] = Field(default_factory=list, max_length=20)
    period_report: dict | None = None
    watch_results: list[WatchEvaluation] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)
    provider: dict | None = None
    trace: list[dict] = Field(default_factory=list)
    created_at: datetime

class WorkflowState:
    def __init__(self,path):
        self.path=str(path)
        self.database=Database('workflow',path)
    def db(self): return self.database.connection()

    def observe_snapshot(self, task, *, advance=False, db=None):
        # These are artifact fences, not an authoritative copy of Core profiles.
        # Historical research reads a supplied snapshot and never moves current heads.
        if task.operation == 'period':
            return
        if db is None:
            with self.db() as connection:
                connection.lock()
                return self.observe_snapshot(task, advance=advance, db=connection)
        for business in task.businesses:
            company = business.company.model_dump(mode='json')
            company.pop('role', None)
            digest = payload_hash({'company': company,
                'dependency_graph': [edge.model_dump(mode='json') for edge in business.dependency_graph]})
            row = db.execute('SELECT version,digest FROM workflow_context_heads WHERE tenant=%s AND company=%s',
                (task.tenant_id, business.company_id)).fetchone()
            if row and (row[0] > business.context_version or
                        (row[0] == business.context_version and row[1] != digest)):
                raise WorkflowSnapshotConflict('stale_or_conflicting_company_context')
            if advance and (row is None or row[0] < business.context_version):
                db.execute('INSERT INTO workflow_context_heads VALUES(%s,%s,%s,%s) ON CONFLICT(store_key,tenant,company) DO UPDATE SET version=excluded.version,digest=excluded.digest',
                    (task.tenant_id, business.company_id, business.context_version, digest))
        policy=task.source_policy.model_dump(mode='json')
        for key in ('issued_at','user_excluded_source_ids','user_excluded_publishers','user_excluded_document_uris'):policy.pop(key,None)
        policy_digest=payload_hash(policy)
        for company_id in {task.company_id, *(item.company_id for item in task.businesses)}:
            row = db.execute('SELECT version,digest FROM workflow_policy_heads WHERE tenant=%s AND company=%s AND policy=%s',
                (task.tenant_id, company_id, task.source_policy.policy_id)).fetchone()
            if row and (row[0] > task.source_policy_version or
                        (row[0]==task.source_policy_version and row[1] is not None and row[1]!=policy_digest)):
                raise WorkflowSnapshotConflict('stale_or_conflicting_source_policy')
            if advance and (row is None or row[0] < task.source_policy_version or row[1] is None):
                db.execute('INSERT INTO workflow_policy_heads VALUES(%s,%s,%s,%s,%s) ON CONFLICT(store_key,tenant,company,policy) DO UPDATE SET version=excluded.version,digest=excluded.digest',
                    (task.tenant_id, company_id, task.source_policy.policy_id, task.source_policy_version,policy_digest))
    def stage(self,task,name,payload):
        with self.db() as db: db.execute('INSERT INTO workflow_stages VALUES(%s,%s,%s,%s,%s) ON CONFLICT(store_key,tenant,request,stage) DO UPDATE SET digest=excluded.digest,payload=excluded.payload,stage_order=DEFAULT',(task.tenant_id,task.request_id,payload_hash(task.model_dump(mode='json')),name,canonical_json(payload)))
    def prior(self,tenant,company,event):
        with self.db() as db: row=db.execute('SELECT payload FROM workflow_analyses WHERE tenant=%s AND company=%s AND event=%s',(tenant,company,event)).fetchone()
        return json.loads(row[0]) if row else None
    def save_analysis(self,tenant,company,event,signature,payload,*,task=None):
        # Period candidates are research artifacts, not the current monitoring cursor.
        if task is not None and task.operation == 'period': return
        with self.db() as db:
            db.lock()
            if task is not None: self.observe_snapshot(task, db=db)
            prior=db.execute('SELECT payload FROM workflow_analyses WHERE tenant=%s AND company=%s AND event=%s',
                (tenant,company,event)).fetchone()
            if prior:
                old=json.loads(prior[0]).get('candidate',{}).get('versions',{})
                new=payload.get('candidate',{}).get('versions',{})
                if (old.get('as_of') and new.get('as_of') and
                    datetime.fromisoformat(old['as_of'])>datetime.fromisoformat(new['as_of'])):
                    raise WorkflowSnapshotConflict('stale_analysis_as_of')
            db.execute('INSERT INTO workflow_analyses VALUES(%s,%s,%s,%s,%s) ON CONFLICT(store_key,tenant,company,event) DO UPDATE SET signature=excluded.signature,payload=excluded.payload',(tenant,company,event,signature,canonical_json(payload)))
    def cached(self,task):
        with self.db() as db: row=db.execute('SELECT digest,payload FROM workflow_results WHERE tenant=%s AND request=%s',(task.tenant_id,task.request_id)).fetchone()
        if row and row[0]!=payload_hash(task.model_dump(mode='json')): raise ValueError('idempotency_conflict')
        return DataResult.model_validate_json(row[1]) if row else None
    def save_result(self,task,result):
        with self.db() as db:
            db.lock()
            self.observe_snapshot(task, db=db)
            digest=payload_hash(task.model_dump(mode='json'))
            prior=db.execute('SELECT digest FROM workflow_results WHERE tenant=%s AND request=%s',
                (task.tenant_id,task.request_id)).fetchone()
            if prior and prior[0]!=digest:raise ValueError('idempotency_conflict')
            db.execute('INSERT INTO workflow_results VALUES(%s,%s,%s,%s) ON CONFLICT(store_key,tenant,request) DO NOTHING',
                (task.tenant_id,task.request_id,digest,result.model_dump_json()))

    def claim_execution(self,task,owner,capacity,ttl=120):
        digest=payload_hash(task.model_dump(mode='json'))
        with self.db() as db:
            db.lock()
            old=db.execute('SELECT digest,state,lease_until>clock_timestamp() AS active FROM workflow_executions WHERE tenant=%s AND request=%s',(task.tenant_id,task.request_id)).fetchone()
            if old and old['digest']!=digest:raise ValueError('idempotency_conflict')
            if old and old['state']=='running' and old['active']:raise WorkflowNotStarted('request_in_progress')
            if old and old['state'] in {'running','uncertain'}:raise WorkflowNotStarted('execution_recovery_required')
            db.execute('DELETE FROM workflow_slots WHERE lease_until<=clock_timestamp()')
            occupied={row[0] for row in db.execute('SELECT slot FROM workflow_slots')}
            slot=next((value for value in range(capacity) if value not in occupied),None)
            if slot is None:raise WorkflowNotStarted('capacity_exhausted')
            db.execute('INSERT INTO workflow_slots VALUES(%s,%s,clock_timestamp()+make_interval(secs=>%s))',(slot,owner,ttl))
            db.execute("INSERT INTO workflow_executions VALUES(%s,%s,%s,%s,'running',clock_timestamp()+make_interval(secs=>%s)) ON CONFLICT(store_key,tenant,request) DO UPDATE SET owner=excluded.owner,state='running',lease_until=excluded.lease_until",(task.tenant_id,task.request_id,digest,owner,ttl))
    def renew_execution(self,task,owner,ttl=120):
        with self.db() as db:
            db.lock()
            updated=db.execute("UPDATE workflow_executions SET lease_until=clock_timestamp()+make_interval(secs=>%s) WHERE tenant=%s AND request=%s AND owner=%s AND state='running' AND lease_until>clock_timestamp()",(ttl,task.tenant_id,task.request_id,owner)).rowcount
            db.execute('UPDATE workflow_slots SET lease_until=clock_timestamp()+make_interval(secs=>%s) WHERE owner=%s AND lease_until>clock_timestamp()',(ttl,owner))
            return bool(updated)
    def release_execution(self,task,owner,success):
        with self.db() as db:
            db.lock()
            db.execute("UPDATE workflow_executions SET state=%s WHERE tenant=%s AND request=%s AND owner=%s",('complete' if success else 'uncertain',task.tenant_id,task.request_id,owner))
            db.execute('DELETE FROM workflow_slots WHERE owner=%s',(owner,))
    def recover_execution(self,tenant,request):
        with self.db() as db:
            db.lock()
            row=db.execute('SELECT owner,lease_until>clock_timestamp() AS active FROM workflow_executions WHERE tenant=%s AND request=%s',(tenant,request)).fetchone()
            if row and row['active']:raise ValueError('execution_lease_still_active')
            if row:
                db.execute('DELETE FROM workflow_slots WHERE owner=%s',(row['owner'],))
                db.execute('DELETE FROM workflow_executions WHERE tenant=%s AND request=%s',(tenant,request))

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
        self.attempts.append({'profile':response.get('profile'),'model':response.get('model'),'cost':str(response.get('cost')) if response.get('cost') is not None else None,
            'currency':response.get('cost_currency'),'cost_source':response.get('cost_source'),'usage':response.get('usage')})
        self.check('after_model')
        return response

class DataWorkflow:
    def __init__(self,state_dir,*,llm=None,role_services=None,analyst_registry=None,collector=None,max_concurrency=2,clock=None,analyst_config=None):
        self.state_dir=Path(state_dir); self.state_dir.mkdir(parents=True,exist_ok=True)
        self.clock=clock or (lambda: datetime.now(timezone.utc))
        self.store=CorpusStore(self.state_dir/'corpus.sqlite3',clock=self.clock)
        self.events=EventStore(self.state_dir/'events.sqlite3')
        self.state=WorkflowState(self.state_dir/'workflow.sqlite3')
        self.llm=llm
        self.analyst_config=(analyst_config if isinstance(analyst_config,AnalystConfig) else AnalystConfig.model_validate(analyst_config) if analyst_config is not None else None)
        self.role_services=dict(role_services or {})
        # Operator-approved analyst templates are shared. CompanyAnalystStore below
        # still writes only tenant/company-scoped private configurations.
        self.runtime_fingerprint=payload_hash({
            "modules":{file.name:hashlib.sha256(file.read_bytes()).hexdigest() for file in Path(__file__).parent.glob("*.py")},
            "prompts":{file.name:hashlib.sha256(file.read_bytes()).hexdigest() for file in (Path(__file__).parent/'prompts').glob("*.txt")}})
        self.registry=analyst_registry or PromptRegistry(self.state_dir/'analysts.sqlite3')
        self.collector=collector or NewsCollector(self.store,state_path=self.state_dir/'collection.sqlite3',raw_dir=self.state_dir/'raw'/'collection')
        self.max_concurrency=max(1,min(4,max_concurrency))
    def _case_approval(self, task, documents=()):
        """Operator approval pins company snapshots and exact organizer document revisions."""
        from types import SimpleNamespace
        from .case_permission import approved_case_data
        if os.getenv('INTELLIGENCE_ALLOW_HACKATHON_CASE_DATA') != '1' or task.tenant_id != 'core-local':
            return None
        services=[item for item in [self.llm,*self.role_services.values()] if item is not None]
        if not services or not task.businesses:
            return None
        approval=None
        for business in task.businesses:
            request=SimpleNamespace(tenant_id=task.tenant_id,company_id=business.company_id,
                company=business.company,source_ids=task.source_policy.effective_source_ids(),
                document_revision_refs=[document['document_revision_ref'] for document in documents])
            for service in services:
                approval=approved_case_data(request,documents,service)
                if approval is None:return None
        return approval

    def _case_gateway_allowed(self, task):
        return self._case_approval(task) is not None

    def run(self,task,checkpoint=None):
        task=task if isinstance(task,DataTask) else DataTask.model_validate(task)
        original_check=checkpoint or (lambda stage:None);original_check('before_workflow')
        self.assert_current(task)
        cached=self.state.cached(task)
        if cached is not None:
            self.assert_current(task);return cached
        owner=uuid4().hex;self.state.claim_execution(task,owner,self.max_concurrency)
        stopping=threading.Event();lost=threading.Event();success=False;safe_boundary=False
        def heartbeat():
            while not stopping.wait(20):
                try:
                    if not self.state.renew_execution(task,owner):lost.set();return
                except Exception:lost.set();return
        thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
        def check(stage):
            if lost.is_set():raise WorkflowSnapshotConflict('execution_lease_lost')
            nonlocal safe_boundary
            try:original_check(stage)
            except BaseException:
                safe_boundary=stage in {'personal_complete','before_result_cache','collection_complete'}
                raise
        try:
            result=self._run(task,check);success=True;return result
        finally:
            stopping.set();thread.join(timeout=6)
            self.state.release_execution(task,owner,success or safe_boundary)

    def assert_current(self, task):
        self.state.observe_snapshot(task)

    def _run(self,task,check):
        self.state.observe_snapshot(task, advance=True)
        original_check = check
        config_fences = {}
        def check(stage):
            original_check(stage)
            self.state.observe_snapshot(task)
            for company_id, (config_store, expected_ref) in config_fences.items():
                if config_store.get(task.tenant_id,company_id)['config_ref'] != expected_ref:
                    raise WorkflowSnapshotConflict('stale_analyst_configuration')
        cached=self.state.cached(task)
        if cached is not None:check('cached_result');return cached
        policy=task.source_policy; allowed=policy.effective_source_ids(); trace=[]
        def stage(name,payload):
            check(name);self.state.stage(task,name,payload);trace.append({'stage':name,**payload})
        result=DataResult(request_id=task.request_id,tenant_id=task.tenant_id,company_id=task.company_id,status='completed',versions={'context':task.context_version,'source_policy':policy.revision,'company_contexts':{business.company_id:business.context_version for business in task.businesses},'policy_id':policy.policy_id},created_at=self.clock())
        case_approval=self._case_approval(task)
        case_gateway_allowed=case_approval is not None
        model_allowed=task.data_classification in {'synthetic','public'} or case_gateway_allowed
        model=_TaskLLM(self.llm,task.budget,check,self.role_services) if model_allowed and (self.llm or self.role_services) else None
        if model:
            config=getattr(self.llm or next(iter(self.role_services.values())), 'config', None)
            result.provider={'profile':getattr(config,'profile',None),'base_url':getattr(config,'base_url',None),
                'model':getattr(config,'model',None),'pricing_mode':getattr(config,'pricing_mode',None)}
        if (self.llm or self.role_services) and not model_allowed:
            result.coverage['model_limitation']='confidential_external_model_disabled'
        if task.operation=='prepare_description':
            proposal=prepare_description(task.description_input,llm=model.for_role('generator') if model else None)
            result.context_proposals.append(proposal)
            if proposal.extraction_mode=='deterministic_fallback' or proposal.limitations:result.status='partial'
            result.coverage.update(model_calls=model.calls if model else 0,model_attempts=model.attempts if model else [],
                context_saved=False,requires_core_profile_acceptance=True)
            stage('description_prepared',{'extraction_mode':proposal.extraction_mode,'claims_proposed':len(proposal.context.facts)+len(proposal.context.dependencies)+len(proposal.context.activities)+len(proposal.context.assertions)})
            check('before_result_cache');self.state.save_result(task,result)
            return result
        if task.operation=='collect':
            stage('before_collection',{'source_count':len(allowed)})
            result.collection=self.collector.collect(policy,now=self.clock(),checkpoint=check,
                source_ids=[source.source_id for source in policy.effective_sources() if source.source_role=='external'] if case_gateway_allowed else None)
            stage('collection_complete',{'report_saved':True})
        analysis_as_of=(self.clock() if task.knowledge_mode=='latest' else task.as_of)
        if task.period and task.period.mode=='retrospective': analysis_as_of=self.clock()
        elif task.period and task.period.mode=='as_known': analysis_as_of=min(task.as_of,task.period.end)
        analysis_businesses=task.businesses
        if task.period and task.period.mode=='as_known':
            analysis_businesses=[]
            profile_limits={}
            for business in task.businesses:
                if business.known_at is not None and business.known_at<=analysis_as_of:
                    analysis_businesses.append(business)
                    continue
                # A current profile with unknown observation dates is not a
                # historical snapshot. Retain only individually dated records.
                company=business.company
                updates={group:[item for item in getattr(company,group)
                    if getattr(item,'observed_at',None) is not None]
                    for group in ('activities','assertions','dependencies','facts')}
                updates.update(industry='',description='')
                analysis_businesses.append(business.model_copy(update={
                    'company':company.model_copy(update=updates),'dependency_graph':[]}))
                profile_limits[business.company_id]='historical_profile_knowledge_time_not_confirmed'
            if profile_limits:result.coverage['profile_time_limitations']=profile_limits
        docs=self.store.list_documents(tenant_id=task.tenant_id,source_ids=allowed,as_of=analysis_as_of.isoformat(),policy=policy,as_known=bool(task.period and task.period.mode=='as_known'))
        docs=[d for d in docs if not d['canonical_uri'].startswith('urn:event:')]
        refs=[d['document_revision_ref'] for d in docs]
        if task.operation=='collect' and case_gateway_allowed:
            present={document['document_revision_ref'] for document in result.collection['documents']}
            result.collection['documents'].extend(document for document in docs if document['source_role']=='bank' and document['document_revision_ref'] not in present)
            result.coverage['bank_documents_preloaded']=sum(document['source_role']=='bank' for document in docs)
        if case_gateway_allowed:
            case_approval=self._case_approval(task,docs)
            case_gateway_allowed=case_approval is not None
            if not case_gateway_allowed:
                model=None
                result.coverage['model_limitation']='organizer_approval_snapshot_mismatch'
        unavailable_bank=[source.source_id for source in policy.effective_sources() if source.source_role=='bank' and source.source_id not in {document['source_id'] for document in docs}]
        if unavailable_bank:
            result.coverage['bank_sources_unavailable']=unavailable_bank
            if result.collection is not None:
                result.collection['pending_metadata'].extend({'source_id':source_id,'reason':'publication_date_or_import_not_available'} for source_id in unavailable_bank)
        if model and not case_gateway_allowed and any(document.get('data_classification','confidential') not in {'public','synthetic'} for document in docs):
            model=None
            result.coverage['model_limitation']='persisted_source_classification_denied'
        result.watch_results=evaluate_conditions(task.watches,as_of=analysis_as_of,
            context_versions={item.company_id:item.context_version for item in task.businesses},
            document_versions={document['document_id']:document['document_revision_ref'] for document in docs},observations=task.watch_observations)

        stage('permitted_snapshot',{'documents':len(refs),'policy_version':policy.revision,
            'knowledge_mode':task.knowledge_mode,'analysis_as_of':analysis_as_of.isoformat()})
        event_store=self.events
        if task.operation=='period':
            event_store,snapshot_ref=self._historical_events(task,refs,analysis_as_of,check)
            result.coverage['event_snapshot_ref']=snapshot_ref
            result.versions['event_snapshot']=snapshot_ref
        events={};event_document_refs={}
        for doc in docs:
            if doc['source_role']!='external' or not doc['canonical_uri'].startswith(('https://','http://')):continue
            check('event_ingest')
            data={k:doc[k] for k in PublicationInput.model_fields if k in doc and doc[k] is not None}
            event=event_store.ingest(data);event_id=event['event_id'];events[event_id]=max([event,events.get(event_id,event)],key=lambda item:item['revision'])
            if doc.get('document_revision_ref'):
                event_document_refs.setdefault(event_id,set()).add(doc['document_revision_ref'])
        if task.event_ids:events={k:v for k,v in events.items() if k in task.event_ids}
        if task.operation=='period':
            publications=[PeriodPublication(event_id=next((event_id for event_id,event_refs in event_document_refs.items() if document['document_revision_ref'] in event_refs),document['document_id']),
                revision=document['revision'],event_time=document.get('event_time'),published_at=document['published_at'],server_received_at=document.get('received_at'),
                source_id=document['source_id'],document_revision_ref=document['document_revision_ref'],title=document['title'],quote=document['content'][:1000]) for document in docs if document['content'] and document['source_role']=='external']
            report=analyze_period_research(task.period.observations,period_start=task.period.start,period_end=task.period.end,known_at=analysis_as_of if task.period.mode=='as_known' else task.as_of,mode=task.period.mode,publications=publications,evaluated_at=self.clock())
            result.period_report={**report.metrics.model_dump(mode='json'),'publication_timeline':[item.model_dump(mode='json') for item in report.publication_timeline],
                'research_coverage':report.coverage,'limitations':report.limitations}
            result.coverage['document_revision_refs']=refs
            stage('period_complete',{'metrics':len(report.metrics.metrics),'publications':len(report.publication_timeline)})
            eligible_refs={entry.document_revision_ref for entry in report.publication_timeline}
            events={key:event for key,event in events.items() if event_document_refs.get(key,set()) & eligible_refs}
            event_document_refs={key:values & eligible_refs for key,values in event_document_refs.items() if key in events}
            # Out-of-period external evidence must not leak through retrieval.
            docs=[doc for doc in docs if doc['source_role']=='bank' or doc['document_revision_ref'] in eligible_refs]
            refs=[doc['document_revision_ref'] for doc in docs]
        if task.operation != 'period' or task.businesses:
            personal={}
            for business in analysis_businesses:
                folder=self.company_dir(task.tenant_id,business.company_id)
                registry=self.registry;configs=CompanyAnalystStore(folder/'company-analysts.sqlite3',registry)
                if task.operation == 'period':
                    # Avoid publishing historical preferences as the current company method.
                    config=registry.select(role=business.company.role, industry=business.company.industry)
                    record={'tenant_id':task.tenant_id,'company_id':business.company_id,
                        'context_version':business.context_version,'config_ref':config.ref,'config':config,
                        'generation_status':'historical_registry_snapshot','history':[]}
                else:
                    record=configs.onboard(task.tenant_id,business.company_id,business.context_version,business.company)
                if task.operation == 'optimize':
                    check('before_config_optimization')
                    if model is None:
                        record={**record,'generation_status':'optimization_unavailable',
                            'limitation':result.coverage.get('model_limitation','model_not_configured')}
                    else:
                        record=configs.optimize(task.tenant_id,business.company_id,business.context_version,
                            business.company,model.for_role('generator'),
                            expected_revision=task.expected_config_revisions[business.company_id])
                    check('after_config_optimization')
                elif task.operation == 'rollback_config':
                    check('before_config_rollback')
                    record=configs.rollback(task.tenant_id,business.company_id,
                        expected_revision=task.expected_config_revisions[business.company_id])
                    check('after_config_rollback')
                if task.operation != 'period':
                    config_fences[business.company_id]=(configs,record['config_ref'])
                if (task.auto_optimize and task.operation in {'onboard','analyze','collect','reassess'}
                    and record.get('generation_status') in {'registry_fallback','registry_fallback_after_raw_context_check','context_rebased','registry_context_rebased'}):
                    intent=FollowUpIntent.from_configuration(parent_request_id=task.request_id,
                        tenant_id=task.tenant_id,company_id=business.company_id,context_version=business.context_version,
                        company=business.company,configuration=record,
                        reason='onboard' if task.operation=='onboard' else 'context_changed')
                    if intent is not None:result.follow_up_intents.append(intent)
                personal[business.company_id]=(registry,record)
                result.configurations.append({**record,
                    'runtime_override_ref':self.analyst_config.ref if self.analyst_config else None,
                    'config':record['config'].model_dump(mode='json'),
                    'source_config_ref':record['history'][-1]['source_config_ref'] if record.get('history') else None})
            if task.operation not in {'onboard','optimize','rollback_config'}:
                ordered_events=self._ordered_events(task,events,event_document_refs,docs,analysis_as_of,businesses=analysis_businesses)
                exploratory_remaining=task.max_exploratory_analyses
                result.coverage.update(events_available=len(events),events_selected=min(len(ordered_events),task.max_events),
                    event_selection='lexical_priority_with_bounded_exploration',
                    selection_recall_not_established=True)
                for event_id in ordered_events[:task.max_events]:
                    # Shared packages stay in local artifacts; only the existing pipeline's
                    # explicitly classification-gated summary may be sent to an allowed model.
                    event_refs=sorted(event_document_refs.get(event_id,set()))
                    if not event_refs:continue
                    package=event_store.build_impact_package(task.tenant_id,event_id,revision=events[event_id]['revision'],source_policy_id=policy.policy_id,policy_version=str(policy.revision),allowed_source_ids=allowed,allowed_document_revision_refs=event_refs,llm=model.for_role('extractor') if model else None)
                    result.events.append(package.model_dump(mode='json'))
                    source=next((d for d in docs if d['document_revision_ref'] in {s.document_revision_ref for s in package.sources}),None)
                    if source is None:continue
                    selections=select_affected_businesses(package,analysis_businesses,as_of=analysis_as_of)
                    result.selections.extend(selections)
                    by_id={business.company_id:business for business in analysis_businesses}
                    for selection in selections:
                        business=by_id[selection.company_id]
                        business_model=model
                        if task.trigger=='scheduled' and selection.score<0.10:
                            if business_model and exploratory_remaining>0:
                                exploratory_remaining-=1
                                stage('business_model_exploration',{'company_id':business.company_id,
                                    'event_id':event_id,'reason':'audit_low_lexical_signal_within_same_task_budget'})
                            else:
                                business_model=None
                                stage('business_model_not_selected',{'company_id':business.company_id,'event_id':event_id,'reason':'no_sufficient_signal_in_available_snapshot','fallback':'local_baseline_with_coverage'})
                        check('before_business');registry,record=personal[business.company_id]
                        config=record['config'] if isinstance(record['config'],AnalystConfig) else AnalystConfig.model_validate(record['config'])
                        if self.analyst_config is not None:
                            config=self.analyst_config
                        if business.preferences is not None:
                            overrides=business.preferences.model_dump(exclude_none=True, exclude={'role'})
                            config=AnalystConfig.model_validate({**config.model_dump(mode='json'),**overrides})
                            stage('preferences_applied',{'company_id':business.company_id,
                                'preferences':business.preferences.model_dump(exclude_none=True)})
                        paths=impact_candidates(package,business.company_id,business.dependency_graph,as_of=analysis_as_of.isoformat(),max_hops=config.max_depth)
                        bank_refs=self._relevant_bank_refs(task,business,allowed,refs,config,as_of=analysis_as_of)
                        event_semantics=sorted({(source.content_hash,source.origin_group_id,source.source_role,
                                                  source.title,source.publisher,source.author)
                                                 for source in package.sources})
                        policy_signature=policy.model_dump(mode='json')
                        for key in ('issued_at','user_excluded_source_ids','user_excluded_publishers','user_excluded_document_uris'):
                            policy_signature.pop(key,None)
                        analysis_company=self._analysis_company(business)
                        def model_identity(service):
                            configured=getattr(service,'config',None)
                            return {name:str(getattr(configured,name,None)) for name in ('profile','model','max_output_tokens','reasoning_effort','input_per_million','output_per_million')}
                        route_models={role:model_identity(service) for role,service in self.role_services.items()}
                        active_context_ids=sorted(item.dependency_id for item in analysis_company.dependencies
                            if self._context_active(item,analysis_as_of))
                        active_fact_ids=sorted(item.fact_id for item in analysis_company.facts
                            if self._context_active(item,analysis_as_of))
                        from .company_context import project_company_context
                        context_projection=project_company_context(analysis_company,as_of=analysis_as_of)
                        inputs={'event_id':event_id,'event_semantics':event_semantics,
                            'context_projection':context_projection.fingerprint,
                            'context_version':business.context_version,
                            'company_snapshot':business.company.model_dump(mode='json'),
                            'analysis_context':analysis_company.model_dump(mode='json'),
                            'dependency_graph':[edge.model_dump(mode='json') for edge in business.dependency_graph],
                            'active_context_ids':active_context_ids,'active_fact_ids':active_fact_ids,
                            'policy':policy_signature,'relevant_bank_refs':bank_refs,
                            'config_ref':record['config_ref'],'config':config.model_dump(mode='json'),
                            'execution_budget':task.budget.model_dump(mode='json'),
                            'model_selected':business_model is not None,
                            'model':{'default':model_identity(self.llm),**route_models},'runtime':self.runtime_fingerprint,
                            'data_classification':task.data_classification,'knowledge_mode':task.knowledge_mode,
                            'workflow':'ai-data-v1'}
                        signature=payload_hash(inputs);previous=(None if task.operation=='period' else self.state.prior(task.tenant_id,business.company_id,event_id))
                        analysis_request_id=hashlib.sha256(f'{task.request_id}:{business.company_id}:{event_id}'.encode()).hexdigest()
                        force_refresh=task.trigger in {'manual','watch_due'} or task.operation=='period'
                        proof=next((item for item in ([] if task.operation=='period' else task.published_assessments) if item.company_id==business.company_id
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
                                {'signature':signature,'inputs':inputs,'candidate':candidate.model_dump(mode='json')},task=task)
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
                            analyst_config=config,role_services=role_adapters).run(request,checkpoint=check,shared_event_package=package,data_classification=task.data_classification,case_approval=case_approval)
                        if selection.status=='no_signal_found':
                            # A valid quote/ID and a critic's agreement cannot establish
                            # a missing event-to-company relationship.
                            if candidate.status=='ready': candidate.status='partial'
                            candidate.coverage.limitations=list(dict.fromkeys(candidate.coverage.limitations+['business_relevance_not_established']))
                            candidate.clarifications=list(dict.fromkeys(candidate.clarifications+[
                                'Какая подтверждённая зависимость связывает компанию с найденными промежуточными звеньями? Предварительный отбор прямой связи не установил; проверьте применимость по документам.']))[:30]
                        if business.company_id in result.coverage.get('profile_time_limitations',{}):
                            candidate.status='partial'
                            candidate.coverage.limitations=list(dict.fromkeys(candidate.coverage.limitations+[
                                'historical_profile_knowledge_time_not_confirmed']))
                            candidate.clarifications=list(dict.fromkeys(candidate.clarifications+[
                                'Какие сведения профиля были подтверждены на дату исследования? Нужен исторический снимок Core или даты observed_at.']))[:30]
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
                        self.state.save_analysis(task.tenant_id,business.company_id,event_id,signature,{'signature':signature,'inputs':inputs,'candidate':candidate.model_dump(mode='json')},task=task)
                        stage('personal_complete',{'company_id':business.company_id,'event_id':event_id,'status':candidate.status})
        for proof in ([] if task.operation=='period' else task.published_assessments):
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
        elif task.operation in {'optimize','rollback_config'} and any(row.get('status') in {'revision_conflict','stale_context','rollback_unavailable'} for row in result.configurations):result.status='partial'
        elif task.operation=='optimize' and any(row.get('generation_status') not in {
                'model_generated_bounded_preferences','stored','optimized','unchanged','already_current'} for row in result.configurations):result.status='partial'
        elif not result.candidates and task.operation not in {'onboard','period','optimize','rollback_config'}:result.status='unknown'
        result.trace=trace;check('before_result_cache');self.state.save_result(task,result)
        return result
    def _historical_events(self,task,refs,as_of,check):
        """Materialize research in an isolated artifact snapshot, never current events."""
        signature=payload_hash({'tenant':task.tenant_id,'policy':task.source_policy.model_dump(mode='json'),
            'refs':sorted(refs),'as_of':as_of.isoformat(),'runtime':self.runtime_fingerprint})
        check('historical_snapshot_copy')
        snapshot=self.events.clone_snapshot(self.events.database.namespace+':history:'+signature,task.tenant_id)
        return snapshot,'historical:'+signature

    def _ordered_events(self,task,events,event_refs,docs,as_of,*,businesses=None):
        from .relevance import rank_events
        by_ref={doc['document_revision_ref']:doc for doc in docs}
        inputs=[]
        for event_id,event in events.items():
            sources=[by_ref[ref] for ref in event_refs.get(event_id,()) if ref in by_ref]
            if not sources:continue
            doc=max(sources,key=lambda item:item['published_at'])
            inputs.append(EventInput(event_id=event_id,revision=event['revision'],title=doc['title'],
                text=doc['content'],source_id=doc['source_id'],published_at=doc['published_at'],synthetic=doc['synthetic']))
        scores={event.event_id:0 for event in inputs}
        for business in (task.businesses if businesses is None else businesses):
            for offset in range(0,len(inputs),500):
                ranked=rank_events(tenant_id=task.tenant_id[:128],company=self._analysis_company(business),
                    events=inputs[offset:offset+500],source_ids=task.source_policy.effective_source_ids(),as_of=as_of.isoformat())
                for row in ranked['ranked']:scores[row['event_id']]=max(scores[row['event_id']],row['score'])
        by_id={event.event_id:event for event in inputs}
        ordered=sorted(scores,key=lambda key:(-scores[key],-by_id[key].published_at.timestamp(),key))
        # Inspect a rotating low-score item when a cap would otherwise hide it.
        if len(ordered)>task.max_events and task.max_exploratory_analyses and task.max_events>1:
            low=[key for key in ordered[task.max_events-1:] if scores[key]==0]
            if low:
                chosen=min(low,key=lambda key:payload_hash({'event':key,'date':as_of.date().isoformat()}))
                ordered.remove(chosen);ordered.insert(task.max_events-1,chosen)
        return ordered

    def company_dir(self,tenant,company):
        folder=self.state_dir/'companies'/hashlib.sha256(tenant.encode()).hexdigest()/hashlib.sha256(company.encode()).hexdigest()
        folder.mkdir(parents=True,exist_ok=True);return folder

    @staticmethod
    def _analysis_company(business):
        """Combine Core business context and its separately-provenanced graph for analysis."""
        company=business.company
        if business.preferences and business.preferences.role is not None:
            company=company.model_copy(update={'role':business.preferences.role})
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
        observed_at=getattr(item,'observed_at',None)
        return not ((observed_at is not None and observed_at>as_of) or (valid_from is not None and valid_from>as_of) or
                    (valid_until is not None and valid_until<as_of))

    def _relevant_bank_refs(self,task,business,allowed,document_refs,config,*,as_of=None):
        as_of=as_of or task.as_of
        bank_refs=[doc['document_revision_ref'] for doc in self.store.list_documents(
            tenant_id=task.tenant_id,source_ids=allowed,as_of=as_of.isoformat(),
            policy=task.source_policy,as_known=bool(task.period and task.period.mode=='as_known'))
            if doc['source_role']=='bank' and doc['document_revision_ref'] in set(document_refs)]
        if not bank_refs:return []
        from .company_context import project_company_context
        projected=project_company_context(business.company,as_of=as_of)
        terms=[d.label for d in projected.dependencies]
        terms.extend(fact.text for fact in projected.facts)
        terms.extend(item.label for item in [*projected.activities,*projected.assertions])
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
