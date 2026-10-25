"""Evidence-bounded business impact paths and observation-only period analysis."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator
from .company_context import project_company_context
from .schemas import CompanyContext


def _time(value: str|datetime, name: str) -> datetime:
    if isinstance(value,datetime):result=value
    elif isinstance(value,str):
        try: result=datetime.fromisoformat(value.replace("Z","+00:00"))
        except ValueError: raise ValueError(f"{name} must be an aware ISO datetime") from None
    else: raise ValueError(f"{name} must be an aware ISO datetime")
    if result.tzinfo is None or result.utcoffset() is None: raise ValueError(f"{name} must include timezone")
    return result.astimezone(timezone.utc)


def _norm(value: str) -> str:
    return " ".join(re.findall(r"[\w]+",value.casefold(),flags=re.UNICODE))


class DependencyEdge(BaseModel):
    """One asserted business dependency; edges require traceable evidence and time bounds."""
    model_config=ConfigDict(extra="forbid")
    edge_id: str=Field(min_length=1,max_length=200)
    subject: str=Field(min_length=1,max_length=500)
    predicate: str=Field(min_length=1,max_length=200)
    object: str=Field(min_length=1,max_length=500)
    status: Literal["confirmed","user_reported","assumed"]
    evidence_ref: str=Field(min_length=1,max_length=1000)
    evidence_basis: Literal["contract","official_record","operational_record","user_statement","payment_observation","other"]
    valid_from: str|None=None
    valid_to: str|None=None

    @field_validator("valid_from","valid_to")
    @classmethod
    def valid_time(cls,value): return None if value is None else _time(value,"edge validity") .isoformat()


class ImpactPath(BaseModel):
    model_config=ConfigDict(extra="forbid")
    company_id: str
    event_id: str
    event_revision: int
    target: str
    path: list[str]
    edge_ids: list[str]
    evidence_refs: list[str]
    statuses: list[str]
    hop_count: int
    interpretation: str
    causal_claim: Literal[False]=False


class ImpactSelection(BaseModel):
    """Heuristic queue-prioritization record, not a semantic impact conclusion."""
    model_config=ConfigDict(extra="forbid")
    company_id: str=Field(min_length=1,max_length=256)
    status: Literal["signal_found","no_signal_found"]
    score_type: Literal["heuristic_priority"]="heuristic_priority"
    score: float=Field(ge=0,le=1)
    reasons: list[str]=Field(default_factory=list,max_length=30)
    roots: list[str]=Field(default_factory=list,max_length=10)
    matched_nodes: list[str]=Field(default_factory=list,max_length=50)
    paths: list[ImpactPath]=Field(default_factory=list,max_length=100)
    coverage: dict[str,Any]=Field(default_factory=dict)
    uncertainty: list[str]=Field(default_factory=list,max_length=30)


class PeriodObservation(BaseModel):
    """A provided numeric observation or revision. No value is inferred from event text."""
    model_config=ConfigDict(extra="forbid")
    observation_id: str=Field(min_length=1,max_length=200)
    metric: str=Field(min_length=1,max_length=300)
    observed_at: str
    available_at: str
    value: Decimal
    unit: str=Field(min_length=1,max_length=100)
    source_ref: str=Field(min_length=1,max_length=1000)
    revision: int=Field(default=1,ge=1)

    @field_validator("observed_at","available_at")
    @classmethod
    def observed_time(cls,value): return _time(value,"observation date").isoformat()

    @field_validator("value",mode="before")
    @classmethod
    def finite_decimal(cls,value):
        try: result=Decimal(str(value))
        except (InvalidOperation,ValueError,TypeError): raise ValueError("observation value must be a finite decimal") from None
        if not result.is_finite(): raise ValueError("observation value must be a finite decimal")
        return result


class PeriodMetric(BaseModel):
    model_config=ConfigDict(extra="forbid")
    metric: str
    unit: str
    first_observed_at: str
    first_value: Decimal
    last_observed_at: str
    last_value: Decimal
    change: Decimal
    observations_used: int
    source_refs: list[str]


class PeriodReport(BaseModel):
    model_config=ConfigDict(extra="forbid")
    period_start: str
    period_end: str
    knowledge_mode: Literal["retrospective","as_known"]
    known_at: str
    metrics: list[PeriodMetric]
    coverage: dict[str,Any]
    statement: str="Descriptive changes in supplied observations; no causal attribution is made."


class ReassessmentReport(BaseModel):
    model_config=ConfigDict(extra="forbid")
    status: Literal["changed","no_change"]
    previous_signature: str|None
    current_signature: str
    changes: list[dict[str,Any]]
    publication_revision_required: bool


def impact_candidates(package, company_id: str, edges: Iterable[DependencyEdge|Mapping[str,Any]], *,
                      as_of: str, max_hops: int=3) -> list[ImpactPath]:
    """Return evidence-backed paths of >=2 edges ending at an explicit package entity.

    `payment_observation` alone is deliberately excluded as a basis for a buyer or supplier
    relationship. Caller-provided edge labels are never expanded into new relations.
    """
    instant=_time(as_of,"as_of")
    if isinstance(max_hops,bool) or not isinstance(max_hops,int) or not 2<=max_hops<=3:
        raise ValueError("max_hops must be 2 or 3")
    parsed=[e if isinstance(e,DependencyEdge) else DependencyEdge.model_validate(e) for e in edges]
    entities={_norm(e) for e in getattr(package,"entities",[]) if isinstance(e,str) and e.strip()}
    for claim in getattr(package,"claims",[]):
        statement=getattr(claim,"statement",None) or (claim.get("statement") if isinstance(claim,dict) else "")
        if statement:
            entities.update(_norm(x) for x in re.findall(r"(?<!\w)(?:[А-ЯЁA-Z][\w-]*(?:\s+[А-ЯЁA-Z][\w-]*){0,2})",statement))
    active=[]
    for edge in parsed:
        if edge.evidence_basis=="payment_observation": continue
        if edge.valid_from and instant<_time(edge.valid_from,"valid_from"): continue
        if edge.valid_to and instant>_time(edge.valid_to,"valid_to"): continue
        active.append(edge)
    results=[]
    def walk(node,path_nodes,path_edges,depth):
        if depth>=2 and _norm(node) in entities:
            results.append(ImpactPath(company_id=company_id,event_id=package.event_id,
                event_revision=package.revision,target=node,path=path_nodes.copy(),edge_ids=[e.edge_id for e in path_edges],
                evidence_refs=[e.evidence_ref for e in path_edges],statuses=[e.status for e in path_edges],
                hop_count=depth,interpretation="Possible dependency path; applicability and effect require review."))
            return
        if depth>=max_hops:return
        for edge in active:
            if _norm(edge.subject)==_norm(node) and not any(x.edge_id==edge.edge_id for x in path_edges):
                if _norm(edge.object) in {_norm(x) for x in path_nodes}: continue
                walk(edge.object,path_nodes+[edge.object],path_edges+[edge],depth+1)
    walk(company_id,[company_id],[],0)
    unique={tuple(path.edge_ids):path for path in results}
    return list(unique.values())


def select_affected_businesses(package, businesses: Sequence[Any], *,
                               as_of: str|datetime, max_hops: int=3) -> list[ImpactSelection]:
    """Rank authorized business snapshots using literal event terms and supplied context.

    Scores are deterministic retrieval-priority hints. They are not model confidence,
    truth probabilities, or proof that an event will affect a company. Every supplied
    company receives a result, including zero-score rows, so callers can inspect
    coverage rather than treating absence of a match as proof of no impact.
    """
    instant=_time(as_of.isoformat() if isinstance(as_of,datetime) else as_of,"as_of")
    if isinstance(max_hops,bool) or not isinstance(max_hops,int) or not 1<=max_hops<=3:
        raise ValueError("max_hops must be between 1 and 3")
    event_terms=_event_terms(package)
    selections=[]
    seen_companies=set()
    for business in businesses:
        company_id=str(_get(business,"company_id") or "")
        if not company_id: raise ValueError("business_snapshot_requires_company_id")
        if company_id in seen_companies: raise ValueError("duplicate_business_snapshot")
        seen_companies.add(company_id)
        company=_get(business,"company")
        company_name=str(_get(company,"name") or "")
        roots={_norm(company_id),_norm(company_name)}-{""}
        graph_raw=list(_get(business,"dependency_graph",[]) or [])
        company_context = company if isinstance(company, CompanyContext) else CompanyContext.model_validate(company)
        projection = project_company_context(company_context, as_of=instant)
        company_deps=list(projection.dependencies)
        facts=list(projection.facts)
        edges=[]; invalid_graph_edges=0; payment_edges=0; inactive_edges=0
        active_declared_graph_edges=0
        for raw in graph_raw:
            try:
                edge=raw if isinstance(raw,DependencyEdge) else DependencyEdge.model_validate(raw)
            except Exception:
                invalid_graph_edges+=1;continue
            if edge.evidence_basis=="payment_observation":
                payment_edges+=1;continue
            if not _active_edge(edge,instant):
                inactive_edges+=1;continue
            active_declared_graph_edges+=1
            edges.append({"id":edge.edge_id,"subject":edge.subject,"object":edge.object,
                "predicate":edge.predicate,"status":edge.status,"evidence_refs":[edge.evidence_ref],
                "basis":edge.evidence_basis,"valid_from":edge.valid_from,"valid_until":edge.valid_to})
        matched_nodes=[]; paths=[]; reasons=[]; uncertainty=[]; signal_scores=[]
        active_context_deps=0; inactive_context_deps=0;payment_context_deps=0
        for raw in company_deps:
            dep=_as_mapping(raw)
            start=str(dep.get("source_node") or "company")
            if _norm(start)=="company":start=company_id
            target=dep.get("target_node")
            label=str(dep.get("label") or "")
            kind=str(dep.get("kind") or "dependency")
            provenance=str(dep.get("provenance") or "")
            status="confirmed" if dep.get("status")=="confirmed" else "assumed"
            if "payment_observation" in (kind+" "+provenance).casefold():
                payment_context_deps+=1;continue
            if not _active_validity(dep.get("valid_from"),dep.get("valid_until"),instant):
                inactive_context_deps+=1;continue
            active_context_deps+=1
            refs=[str(x) for x in (dep.get("evidence_refs") or []) if isinstance(x,str) and x]
            if target:
                edges.append({"id":str(dep.get("dependency_id") or ""),"subject":start,
                    "object":str(target),"predicate":kind,"status":status,"evidence_refs":refs,
                    "basis":"company_context","valid_from":dep.get("valid_from"),"valid_until":dep.get("valid_until")})
            else:
                matches=_matching_terms(label,event_terms)
                if matches:
                    target=matches[0]
                    path=ImpactPath(company_id=company_id,event_id=str(_get(package,"event_id") or "unknown"),
                        event_revision=int(_get(package,"revision") or 1),target=target,
                        path=[company_id,target],edge_ids=[str(dep.get("dependency_id") or "context-dependency")],
                        evidence_refs=refs,statuses=[status],hop_count=1,
                        interpretation="Textual company dependency matches an explicit event term; applicability needs review.")
                    paths.append(path);matched_nodes.extend(matches)
                    signal_scores.append(0.80 if status=="confirmed" else 0.45)
                    reasons.append("company_dependency_text_matches_event_term")
        paths_from_graph=_selection_paths(package,company_id,roots,edges,event_terms,instant,max_hops)
        paths.extend(paths_from_graph)
        for path in paths_from_graph:
            matched_nodes.append(path.target)
            confirmed=all(status=="confirmed" for status in path.statuses)
            signal_scores.append((1.0 if path.hop_count==1 else 0.90-0.05*(path.hop_count-2))
                                 if confirmed else (0.50-0.04*(path.hop_count-1)))
            reasons.append("active_confirmed_dependency_path" if confirmed else "active_unconfirmed_dependency_path")

        fact_matches=[]
        for raw in facts:
            fact=_as_mapping(raw)
            fact_text=_affirmative_context_text(str(fact.get("text") or ""))
            matches=_matching_terms(fact_text,event_terms)
            if matches:
                fact_matches.extend(matches);matched_nodes.extend(matches)
        if fact_matches:
            reasons.append("company_fact_text_matches_event_term")
            signal_scores.append(0.34)

        for activity in projection.activities:
            matches=_matching_terms(activity.label,event_terms)
            if matches:
                matched_nodes.extend(matches);signal_scores.append(0.55)
                reasons.append("confirmed_activity_text_matches_event_term")
        for assertion in projection.assertions:
            matches=_matching_terms(_affirmative_context_text(assertion.label),event_terms)
            if matches:
                matched_nodes.extend(matches);signal_scores.append(0.50)
                reasons.append("confirmed_assertion_text_matches_event_term")

        # General descriptive/industry overlap is deliberately weak and never a path.
        industry=str(_get(company,"industry") or "")
        description=str(_get(company,"description") or "")
        industry_matches=_matching_terms(industry,event_terms)
        description_matches=_matching_terms(description,event_terms)
        if industry_matches:
            matched_nodes.extend(industry_matches)
            reasons.append("industry_term_only_weak_signal")
            signal_scores.append(0.08)
        elif description_matches and not (paths or fact_matches):
            matched_nodes.extend(description_matches)
            reasons.append("description_term_only_weak_signal")
            signal_scores.append(0.06)

        unique_paths={tuple(path.edge_ids):path for path in paths}
        paths=list(unique_paths.values())
        matched_nodes=list(dict.fromkeys(matched_nodes))
        if not signal_scores:
            reasons.append("no_explicit_context_match_found")
            uncertainty.append("No literal match was found in the supplied snapshot; this does not establish absence of impact.")
        if invalid_graph_edges: uncertainty.append("Some supplied graph edges were malformed and were not used.")
        if payment_edges or payment_context_deps:
            uncertainty.append("Payment observations were not treated as evidence of a dependency relationship.")
        if inactive_edges or inactive_context_deps:
            uncertainty.append("Dependencies outside their validity interval were excluded at as_of.")
        if any(any(status!="confirmed" for status in path.statuses) for path in paths):
            uncertainty.append("Some matching dependency paths include assumed or user-reported edges.")
        if industry_matches and not (paths or fact_matches):
            uncertainty.append("Industry overlap is weak prioritization context and does not establish a business dependency.")
        uncertainty.extend(projection.limitations)
        uncertainty.extend("Follow-up: " + question.question for question in projection.questions)
        if not event_terms:
            uncertainty.append("The impact package supplied no explicit entities, resources, or territories to match.")
        confirmed_context=sum(1 for dep in company_deps if _as_mapping(dep).get("status")=="confirmed")
        assumed_context=len(company_deps)-confirmed_context
        package_coverage=_as_mapping(_get(package,"coverage",{}))
        coverage={"package_id":_get(package,"package_id"),
            "package_source_count":len(_get(package,"sources",[]) or []),
            "package_independent_origins":package_coverage.get("independent_origin_groups"),
            "package_limitations":list(package_coverage.get("limitations",[]) or []),
            "event_term_count":len(event_terms),"dependency_graph_edges":len(graph_raw),
            "active_declared_graph_edges":active_declared_graph_edges,
            "active_relationship_edges":len(edges),"invalid_graph_edges":invalid_graph_edges,
            "inactive_graph_edges":inactive_edges,"payment_graph_edges_excluded":payment_edges,
            "company_dependencies":len(company_deps),"active_company_dependencies":active_context_deps,
            "confirmed_company_dependencies":confirmed_context,"assumed_company_dependencies":assumed_context,
            "inactive_company_dependencies":inactive_context_deps,
            "payment_company_dependencies_excluded":payment_context_deps,"company_facts":len(facts),
            "company_activities":len(projection.activities),"company_assertions":len(projection.assertions),
            "company_context_fingerprint":projection.fingerprint,
            "matched_event_terms":len(matched_nodes),"grounded_paths":len(paths),
            "signal_basis":list(dict.fromkeys(reasons)),
            "as_of":instant.isoformat()}
        score=max(signal_scores,default=0.0)
        selections.append(ImpactSelection(company_id=company_id,status="signal_found" if score>0 else "no_signal_found",
            score=score,
            reasons=list(dict.fromkeys(reasons)),roots=sorted(roots),matched_nodes=matched_nodes,paths=paths,
            coverage=coverage,uncertainty=uncertainty))
    selections.sort(key=lambda item:(-item.score,item.company_id))
    return selections


def _get(value,key,default=None):
    return value.get(key,default) if isinstance(value,Mapping) else getattr(value,key,default)


def _as_mapping(value):
    if isinstance(value,Mapping):return value
    dump=getattr(value,"model_dump",None)
    return dump(mode="python") if callable(dump) else {}


def _event_terms(package):
    terms=[]
    for name in ("entities","territories","resources"):
        values=_get(package,name,[]) or []
        terms.extend(str(x).strip() for x in values if isinstance(x,str) and x.strip())
    # Deterministic extractors may leave resources empty while preserving the
    # source wording in attributed claims. These phrases are retrieval leads,
    # not new evidence or extracted entities.
    phrase_stop = _STOP_TOKENS | {"что", "как", "источник", "издание", "сообщает", "сообщение",
        "сообщили", "оператор", "компания", "компании", "отдельном", "случае", "неизвестно",
        "неизвестна", "установлены", "подтверждает", "временно", "штатно", "работает", "открыт",
        "открыта", "после", "перед", "который", "которая", "которые", "были", "будет",
        "дата", "срок", "сроки", "может", "могут", "другом", "другая", "другой"}
    for claim in (_get(package,"claims",[]) or [])[:32]:
        quote=str(_get(claim,"quote","") or "")[:1200]
        # Keep literal named service/company identifiers even when the cheap
        # extractor wraps them in a descriptor (e.g. "Supplier SomeCloud").
        # These remain search leads, never an inferred business relationship.
        terms.extend(token for token in re.findall(r"\b[A-Z][A-Za-z0-9-]{3,}\b",quote)
            if token.casefold() not in {"source","supplier","company","operator","according","report"})
        # Store original tokens. Feeding already-stemmed tokens back through
        # _match_tokens during matching can strip a second suffix (станции →
        # станци → станц), losing legitimate matches for unseen descriptions.
        words=[word for word in re.findall(r"[\w]+",quote,flags=re.UNICODE)
               if _match_tokens(word)]
        for size in (2,3):
            for start in range(max(0,len(words)-size+1)):
                phrase=words[start:start+size]
                if len(phrase)==size and not any(word.casefold() in phrase_stop for word in phrase):
                    terms.append(" ".join(phrase))
    # Deduplicate by normalized phrase while retaining the literal source spelling.
    unique={}
    for term in terms:
        key=tuple(_match_tokens(term))
        if key:unique.setdefault(key,term)
    return list(unique.values())[:128]


_RU_ENDINGS=("иями","ями","ами","иях","ях","ах","ого","ему","ому","ыми","ими","ых","их",
    "ее","ие","ые","ая","яя","ый","ий","ого","ой","ей","ое","ее","ую","юю",
    "ов","ев","ей","ам","ям","ом","ем","ы","и","а","я","у","ю","е","о")
_STOP_TOKENS={"и","в","во","на","по","за","из","к","ко","с","со","для","при","через",
    "the","and","of","in","on","to","for","with","from","a","an"}


def _match_tokens(value):
    tokens=[]
    for token in re.findall(r"[\w]+",str(value).casefold().replace("ё","е"),flags=re.UNICODE):
        if token in _STOP_TOKENS:continue
        if len(token)>=5:
            for suffix in _RU_ENDINGS:
                if token.endswith(suffix) and len(token)-len(suffix)>=4:
                    token=token[:-len(suffix)];break
        elif len(token)>4 and token.endswith("s"):
            token=token[:-1]
        if len(token)>=3:tokens.append(token)
    return tokens


def _phrase_in_text(phrase,text):
    wanted=_match_tokens(phrase);haystack=_match_tokens(text)
    if not wanted or not haystack or len(wanted)>len(haystack):return False
    return any(haystack[index:index+len(wanted)]==wanted for index in range(len(haystack)-len(wanted)+1))


def _matching_terms(text,terms):
    return [term for term in terms if _phrase_in_text(term,text)]


_UNKNOWN_CLAUSE = re.compile(r"\b(?:нет|не|ни|неизвест\w*|отсутств\w*|неподтвержд\w*|не подтвержд\w*|не установ\w*|unknown|no confirmed|not known|not established)\b", re.IGNORECASE)


def _affirmative_context_text(text):
    """Exclude clauses stating a data gap, retaining affirmative parts of mixed assertions."""
    clauses=re.split(r"[;.!?\n]+",text)
    return " ".join(clause for clause in clauses if clause.strip() and not _UNKNOWN_CLAUSE.search(clause))


def _active_validity(valid_from,valid_until,instant):
    start=_time(valid_from,"valid_from") if valid_from else None
    end=_time(valid_until,"valid_until") if valid_until else None
    return not ((start is not None and instant<start) or (end is not None and instant>end))


def _active_edge(edge,instant):
    return _active_validity(edge.valid_from,edge.valid_to,instant)


def _selection_paths(package,company_id,roots,edges,event_terms,instant,max_hops):
    event_id=str(_get(package,"event_id") or "unknown")
    revision=int(_get(package,"revision") or 1)
    found=[]
    def walk(node,path_nodes,path_edges):
        depth=len(path_edges)
        if depth>=max_hops:return
        for edge in edges:
            subject=_norm(edge["subject"])
            starts_here=(subject in roots if depth==0 else subject==_norm(node))
            if not starts_here or any(old["id"]==edge["id"] for old in path_edges):continue
            if not _active_validity(edge["valid_from"],edge["valid_until"],instant):continue
            target=edge["object"]
            if _norm(target) in {_norm(x) for x in path_nodes}:continue
            next_nodes=path_nodes+[target]
            next_edges=path_edges+[edge]
            matched=_matching_terms(target,event_terms)
            if matched:
                found.append(ImpactPath(company_id=company_id,event_id=event_id,event_revision=revision,
                    target=matched[0],path=next_nodes,edge_ids=[e["id"] for e in next_edges],
                    evidence_refs=list(dict.fromkeys(ref for e in next_edges for ref in e["evidence_refs"])),
                    statuses=[e["status"] for e in next_edges],hop_count=len(next_edges),
                    interpretation="Active supplied dependency path reaches an explicit package term; impact is not established."))
            else:
                walk(target,next_nodes,next_edges)
    walk(company_id,[company_id],[])
    unique={tuple(path.edge_ids):path for path in found}
    return list(unique.values())


def analyze_period(observations: Iterable[PeriodObservation|Mapping[str,Any]], *,
                   period_start: str, period_end: str, known_at: str,
                   mode: Literal["retrospective","as_known"]="retrospective") -> PeriodReport:
    start,end,knowledge=_time(period_start,"period_start"),_time(period_end,"period_end"),_time(known_at,"known_at")
    if end<start: raise ValueError("period_end must not precede period_start")
    if mode not in {"retrospective","as_known"}: raise ValueError("mode must be retrospective or as_known")
    values=[o if isinstance(o,PeriodObservation) else PeriodObservation.model_validate(o) for o in observations]
    in_window=[o for o in values if start<=_time(o.observed_at,"observed_at")<=end]
    cutoff=knowledge if mode=="as_known" else datetime.now(timezone.utc)
    selectable=[o for o in in_window if _time(o.available_at,"available_at")<=cutoff
                and _time(o.observed_at,"observed_at")<=cutoff]
    latest={}
    for obs in selectable:
        current=latest.get(obs.observation_id)
        if current is None or (obs.revision,_time(obs.available_at,"available_at"))>(current.revision,_time(current.available_at,"available_at")):
            latest[obs.observation_id]=obs
    grouped={}
    for obs in latest.values(): grouped.setdefault((obs.metric,obs.unit),[]).append(obs)
    metrics=[]
    for (metric,unit),items in sorted(grouped.items()):
        items.sort(key=lambda o:(_time(o.observed_at,"observed_at"),o.observation_id))
        first,last=items[0],items[-1]
        metrics.append(PeriodMetric(metric=metric,unit=unit,first_observed_at=first.observed_at,
            first_value=first.value,last_observed_at=last.observed_at,last_value=last.value,
            change=last.value-first.value,observations_used=len(items),source_refs=list(dict.fromkeys(o.source_ref for o in items))))
    return PeriodReport(period_start=start.isoformat(),period_end=end.isoformat(),knowledge_mode=mode,
        known_at=cutoff.isoformat(),metrics=metrics,coverage={
            "provided_observations":len(values),"in_window_revisions":len(in_window),
            "selected_observation_revisions":len(latest),"excluded_after_known_at":sum(_time(o.available_at,"available_at")>cutoff for o in in_window),
            "metrics_with_multiple_points":sum(m.observations_used>1 for m in metrics),
            "source_refs":list(dict.fromkeys(o.source_ref for o in latest.values())),
            "mode_definition":"retrospective uses latest supplied revisions available now; as_known uses only revisions available by known_at"})


def reassess(previous: Mapping[str,Any]|None, current: Mapping[str,Any]) -> ReassessmentReport:
    """Compare only decision-relevant references and versions; repeated checks stay no_change."""
    keys=("event_revision_refs","evidence_refs","company_context_version","source_policy_id",
          "policy_version","analyst_config_version","model_version","prompt_version")
    def normalized(value): return {key:value.get(key) for key in keys if key in value}
    current_inputs=normalized(current)
    current_sig=hashlib.sha256(json.dumps(current_inputs,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
    if previous is None:
        return ReassessmentReport(status="changed",previous_signature=None,current_signature=current_sig,
            changes=[{"field":key,"before":None,"after":value} for key,value in current_inputs.items()],publication_revision_required=True)
    old_inputs=normalized(previous)
    old_sig=hashlib.sha256(json.dumps(old_inputs,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
    if old_sig==current_sig:
        return ReassessmentReport(status="no_change",previous_signature=old_sig,current_signature=current_sig,
                                  changes=[],publication_revision_required=False)
    changes=[{"field":key,"before":old_inputs.get(key),"after":current_inputs.get(key)}
             for key in sorted(set(old_inputs)|set(current_inputs)) if old_inputs.get(key)!=current_inputs.get(key)]
    return ReassessmentReport(status="changed",previous_signature=old_sig,current_signature=current_sig,
                              changes=changes,publication_revision_required=True)
