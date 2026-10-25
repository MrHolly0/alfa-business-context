"""Bounded, auditable prompt tournaments over frozen evaluation cases."""
from __future__ import annotations

import hashlib
import json
import secrets
import re
import unicodedata
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from .analysts import PromptRegistry
from .corpus import CorpusStore
from .schemas import AnalysisRequest, CandidateAssessment


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


EVALUATION_VERSION = "tournament-grounding-v1"


def _runtime_hashes():
    return {name:hashlib.sha256(Path(__file__).with_name(name+'.py').read_bytes()).hexdigest()
            for name in ('pipeline','tournament','experiments','schemas','analysts','corpus','parsers','evaluation')}



def _normalized(value):
    return " ".join(re.findall(r"\w+",unicodedata.normalize("NFKC",value).casefold()))


def _case_groups(case):
    request=AnalysisRequest.model_validate(case["request"])
    tenant=request.tenant_id
    groups={"event:"+_digest([tenant,_normalized(request.event.text)]),
            "company:"+_digest([tenant,_normalized(request.company.name)])}
    if case.get("group_id"):
        if not isinstance(case["group_id"],str) or len(case["group_id"])>256: raise ValueError("invalid_case_group")
        groups.add("declared:"+_digest([tenant,case["group_id"]]))
    for document in case.get("documents",[]):
        if document.get("source_role")=="external":
            if document.get("canonical_uri"): groups.add("uri:"+_digest([tenant,document["canonical_uri"]]))
            if document.get("content"): groups.add("document:"+_digest([tenant,_normalized(document["content"])]))
    return groups


class TournamentStore:
    """SQLite persistence; interrupted reservations stay unavailable."""
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, manifest TEXT NOT NULL, report TEXT);
                CREATE TABLE IF NOT EXISTS holdout_exposures(group_hash TEXT NOT NULL,run_id TEXT NOT NULL,
                    PRIMARY KEY(group_hash,run_id));
                CREATE TABLE IF NOT EXISTS shadow_exposures(group_hash TEXT NOT NULL,run_id TEXT NOT NULL,
                    PRIMARY KEY(group_hash,run_id));
                CREATE TABLE IF NOT EXISTS cells(cell_key TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    state TEXT NOT NULL, payload TEXT, reserved_at REAL NOT NULL);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, run_id: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT report FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row or row["report"] is None:
            raise ValueError("tournament_report_not_found")
        return json.loads(row["report"])

    def expose_holdout(self, run_id, cases):
        # Track content/origin groups, not mutable case IDs. Company overlap alone
        # is expected between independent future holdouts, so track event groups.
        groups={group for case in cases for group in _case_groups(case) if not group.startswith("company:")}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior=set()
            for group in groups:
                prior.update(row[0] for row in db.execute("SELECT run_id FROM holdout_exposures WHERE group_hash=? AND run_id!=?",(group,run_id)))
                db.execute("INSERT OR IGNORE INTO holdout_exposures VALUES(?,?)",(group,run_id))
        return {"fresh_for_selection":not prior,"prior_run_count":len(prior)}

    def expose_shadow(self, run_id, cases):
        groups={group for case in cases for group in _case_groups(case) if not group.startswith("company:")}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior=set()
            for group in groups:
                prior.update(row[0] for row in db.execute("SELECT run_id FROM shadow_exposures WHERE group_hash=? AND run_id!=?",(group,run_id)))
                db.execute("INSERT OR IGNORE INTO shadow_exposures VALUES(?,?)",(group,run_id))
        return {"fresh":not prior,"prior_run_count":len(prior)}

    def review_pack(self, run_id: str) -> list[dict]:
        """Return blind answers with their case context, never config identities."""
        return self.get(run_id)["human_review"]["pack"]

    def _reserve(self, run_id: str, key: str):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,payload FROM cells WHERE cell_key=?", (key,)).fetchone()
            if row:
                if row["state"] == "complete": return json.loads(row["payload"])
                raise ValueError("cell_interrupted_or_in_progress; explicit operator recovery required")
            db.execute("INSERT INTO cells VALUES(?,?, 'reserved',NULL,?)", (key, run_id, time.time()))
        return None

    def _complete(self, key: str, value: dict):
        with self._db() as db:
            if db.execute("UPDATE cells SET state='complete',payload=? WHERE cell_key=? AND state='reserved'", (_json(value), key)).rowcount != 1:
                raise ValueError("cell_reservation_lost")

    def review(self, run_id: str, *, reviewer: str, ratings: dict) -> dict:
        with self._db() as db:
            row = db.execute("SELECT manifest,report FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or not row["report"]: raise ValueError("tournament_report_not_found")
            manifest, report = json.loads(row["manifest"]), json.loads(row["report"])
            if _digest({k:v for k,v in report.items() if k not in {"human_review","report_hash","promotion_eligible"}}) != manifest.get("frozen_report_hash"):
                raise ValueError("frozen_report_hash_mismatch")
            pack = report["human_review"]["pack"]
            ids = {item["blind_id"] for item in pack}
            if not reviewer.strip() or set(ratings) != ids or any(v not in {"A", "B", "tie"} for v in ratings.values()):
                raise ValueError("invalid_blind_review_ratings")
            label_map = manifest.get("review_label_map", {})
            votes = {ref: 0 for labels in label_map.values() for ref in labels.values()}
            for item in pack:
                choice = ratings[item["blind_id"]]
                if choice != "tie": votes[label_map[item["blind_id"]][choice]] += 1
            ordered = sorted(votes.values(), reverse=True)
            human = max(votes, key=votes.get) if ordered and ordered[0] > 0 and (len(ordered) == 1 or ordered[0] > ordered[1]) else None
            approved = human == report["winner_ref"]
            report["human_review"].update({"reviewer": reviewer, "ratings": ratings, "human_winner_ref": human, "approved": approved})
            ref = report["winner_ref"]
            baseline_ref=report.get("baseline_ref","baseline@1")
            report["promotion_eligible"] = bool(approved and report["synthetic"] is False and ref != baseline_ref and report.get("baseline_snapshot") is not None
                and report["holdout"].get("fresh_for_selection") is True
                and report.get("shadow_validation",{}).get("validated") is True
                and report.get("shadow_validation",{}).get("passed") is True
                and report.get("shadow_validation",{}).get("fresh") is True
                and report.get("mode") == "model" and report.get("shadow") is False
                                and report["holdout"]["passed"] and not report["hard_failures"]
                and report["training"][ref]["wins"] > report["training"][ref]["losses"])
            report["report_hash"] = _digest({k:v for k,v in report.items() if k != "report_hash"})
            db.execute("UPDATE runs SET report=? WHERE run_id=?", (_json(report), run_id))
        return report


class TournamentRunner:
    def __init__(self, store: TournamentStore, registry: PromptRegistry, *, pipeline_factory: Callable,
                 judge=None, critic=None):
        self.store, self.registry, self.pipeline_factory = store, registry, pipeline_factory
        self.judge, self.critic = judge, critic

    @staticmethod
    def _validate_dataset(dataset: dict):
        if dataset.get("schema_version") != "tournament-dataset-v1" or not isinstance(dataset.get("synthetic"), bool): raise ValueError("invalid_tournament_dataset")
        train, holdout = dataset.get("train"), dataset.get("holdout")
        if not isinstance(train, list) or not isinstance(holdout, list) or not train or not holdout or len(train)+len(holdout)>50: raise ValueError("dataset_case_count_limit")
        ids=set(); events={"train":set(),"holdout":set()}; companies={"train":set(),"holdout":set()}
        for split, cases in (("train",train),("holdout",holdout)):
            for case in cases:
                cid=case.get("case_id")
                if not isinstance(cid,str) or not cid or cid in ids: raise ValueError("duplicate_or_invalid_case_id")
                ids.add(cid); req=AnalysisRequest.model_validate(case["request"])
                events[split].add(req.event.event_id); companies[split].add(req.company_id)
                docs=case.get("documents",[])
                if not isinstance(docs,list) or any(not isinstance(d,dict) or d.get("tenant_id") != req.tenant_id for d in docs): raise ValueError("evaluation_tenant_mismatch")
                if dataset["synthetic"] is False and (req.synthetic or req.event.synthetic or any(d.get("synthetic") is True for d in docs)):
                    raise ValueError("synthetic_case_in_real_dataset")
        if events["train"] & events["holdout"] or companies["train"] & companies["holdout"]: raise ValueError("train_holdout_identity_leak")
        train_groups={group for case in train for group in _case_groups(case)}
        holdout_groups={group for case in holdout for group in _case_groups(case)}
        if train_groups & holdout_groups: raise ValueError("train_holdout_content_or_origin_leak")
        return train, holdout

    @staticmethod
    def _blind(candidate):
        from .experiments import blind_answer
        return blind_answer(candidate)

    @staticmethod
    def _blind_evidence(evidence):
        from .experiments import blind_evidence
        return blind_evidence(evidence)

    def _run_case(self, run_id, case, config, split, execution_identity, mode, shadow, candidate_override=None):
        req=AnalysisRequest.model_validate(case["request"])
        pipeline_version="bounded-pipeline-v1"
        prompt_hash=hashlib.sha256(files("intelligence.prompts").joinpath(f"{config.template_version}.system.ru.txt").read_bytes()+files("intelligence.prompts").joinpath(f"{config.template_version}.user.ru.txt").read_bytes()).hexdigest()
        runtime_hashes=_runtime_hashes()
        key=_digest({"config":config.model_dump(mode="json"),"request":req.model_dump(mode="json"),"documents":case.get("documents",[]),"expected":case.get("expected",{}),"tenant":req.tenant_id,"pipeline":pipeline_version,"evaluation":EVALUATION_VERSION,"prompt_hash":prompt_hash,"runtime_hashes":runtime_hashes,"execution_identity":execution_identity,"mode":mode,"shadow":shadow,"candidate_override":candidate_override})
        cached=self.store._reserve(run_id,key)
        if cached is not None: return cached
        with tempfile.TemporaryDirectory(prefix="tournament-") as directory:
            corpus=CorpusStore(Path(directory)/"corpus.sqlite3")
            for doc in case.get("documents",[]): corpus.ingest(doc)
            started=time.perf_counter()
            if candidate_override is None:
                pipeline=self.pipeline_factory(corpus,config)
                candidate=pipeline.run(req).model_dump(mode="json")
            else:
                corpus.ingest({"tenant_id":req.tenant_id,"source_id":req.event.source_id,"source_role":"external",
                    "canonical_uri":f"urn:event:{req.event.event_id}","title":req.event.title,"content":req.event.text,
                    "content_type":"text/plain","published_at":req.event.published_at.isoformat(),
                    "fetched_at":req.as_of.isoformat(),"synthetic":bool(req.synthetic or req.event.synthetic),
                    "publisher":"","language":"ru"})
                candidate=candidate_override
            latency_ms=round((time.perf_counter()-started)*1000, 3)
            # Apply the shipped deterministic evaluation checks to this exact output;
            # never run the pipeline a second time just to score it.
            parsed=CandidateAssessment.model_validate(candidate)
            candidate=parsed.model_dump(mode="json")
            evidence={e["evidence_id"]:e for e in candidate["evidence"]}
            stored_evidence={eid:corpus.get_evidence(tenant_id=req.tenant_id,source_ids=req.source_ids,
                as_of=req.as_of.isoformat(),evidence_id=eid) for eid in evidence}
            claims=candidate["claims"]; expected=case.get("expected",{})
            checks={
                "evidence_ids_exist":all(ref in evidence for c in claims for ref in c["evidence_refs"]),
                "quotes_match_hash":all(hashlib.sha256(e["quote"].encode()).hexdigest()==e["content_hash"] for e in evidence.values()),
                "source_scope":all(e["source_id"] in req.source_ids for e in evidence.values()),
                "fact_source_roles":all(all(evidence.get(ref,{}).get("source_role")==("bank" if c["kind"]=="bank_view" else "external") for ref in c["evidence_refs"]) and bool(c["evidence_refs"]) for c in claims if c["kind"] in {"external_fact","bank_view"}),
                "mode_explicit":candidate["execution_mode"] in {"baseline","model"},
                "requested_execution_mode":candidate["execution_mode"]==mode,
                "request_identity":candidate["request_id"]==req.request_id and candidate["tenant_id"]==req.tenant_id and candidate["company_id"]==req.company_id,
                "budget":candidate["usage"].get("model_calls",0)<=req.budget.max_model_calls,
                "no_pipeline_errors":not candidate["usage"].get("errors"),
                "evidence_timebound":all(e["source_id"] in req.source_ids and datetime.fromisoformat(e["published_at"].replace("Z","+00:00"))<=req.as_of and datetime.fromisoformat(e["fetched_at"].replace("Z","+00:00"))<=req.as_of for e in evidence.values()),
                "evidence_acl":all(stored_evidence[eid] is not None and all(evidence[eid].get(k)==stored_evidence[eid].get(k)
                    for k in ("quote","content_hash","source_id","source_role","document_id","document_revision_ref")) for eid in evidence),
            }
            bank_unknown=any(c["kind"]=="unknown" and c.get("origin")=="bank" for c in claims)
            if "bank_unknown" in expected: checks["bank_unknown"]=bank_unknown==expected["bank_unknown"]
            hypotheses=[c for c in claims if c["kind"]=="model_hypothesis"]
            if candidate["execution_mode"]=="model" and "require_hypothesis" in expected: checks["personalization"]=bool(hypotheses)==expected["require_hypothesis"]
            if candidate["execution_mode"]=="baseline": checks["baseline_abstains"]=not hypotheses
            if candidate["execution_mode"]=="model" and expected.get("require_context"): checks["company_grounds"]=bool(hypotheses) and all(c["context_refs"] for c in hypotheses)
            attempts=candidate.get("usage",{}).get("attempts",[])
            cost_values=[]
            for attempt in attempts:
                try:
                    value=Decimal(str(attempt["cost"]))
                    if value.is_finite() and value>=0: cost_values.append(value)
                except (KeyError,InvalidOperation,TypeError,ValueError): pass
            complete_cost=bool(attempts) and len(cost_values)==len(attempts)
            result={"case_id":case["case_id"],"split":split,"candidate":candidate,"checks":checks,"passed":all(checks.values()),"latency_ms":latency_ms,
                    "measured_cost":str(sum(cost_values,Decimal(0))) if complete_cost else None,
                    "known_partial_cost":str(sum(cost_values,Decimal(0))) if cost_values and not complete_cost else None,
                    "cost_currency":next((a.get("cost_currency") for a in attempts if a.get("cost_currency")),None),"cost_complete":complete_cost}
        self.store._complete(key,result)
        return result

    def _pair(self, case, challenger, baseline, execution_identity=None):
        if not challenger["passed"]: return {"winner":"baseline","reason":"challenger_hard_checks_failed","advisory":True}
        if self.judge is None: return {"winner":"tie","reason":"no_advisory_judge","advisory":True}
        a,b=self._blind(challenger["candidate"]),self._blind(baseline["candidate"])
        request=AnalysisRequest.model_validate(case["request"])
        source_evidence={e["evidence_id"]:e for output in (challenger,baseline) for e in output["candidate"].get("evidence",[])}
        judge_case={"company":request.company.model_dump(mode="json"),"question":request.event.model_dump(mode="json"),"evidence":list(source_evidence.values())}
        # Reserve each potentially billable judge call before it leaves this process.
        key_a=_digest({"kind":"judge","version":"pair-judge-v1","runtime":_runtime_hashes(),"case":judge_case,"a":a,"b":b,"orientation":"candidate_first","execution_identity":execution_identity or {}})
        key_b=_digest({"kind":"judge","version":"pair-judge-v1","runtime":_runtime_hashes(),"case":judge_case,"a":b,"b":a,"orientation":"candidate_second","execution_identity":execution_identity or {}})
        orientation="candidate_first"
        try:
            first=self.store._reserve("judge",key_a)
            if first is None:
                first=self.judge.judge(case=judge_case,a=a,b=b,orientation="candidate_first")
                if first.get("advisory") is not True: raise ValueError("judge_not_advisory")
                self.store._complete(key_a,first)
            orientation="candidate_second"
            second=self.store._reserve("judge",key_b)
            if second is None:
                second=self.judge.judge(case=judge_case,a=b,b=a,orientation="candidate_second")
                if second.get("advisory") is not True: raise ValueError("judge_not_advisory")
                self.store._complete(key_b,second)
        except Exception as exc:
            return {"winner":"tie","reason":"judge_failed_or_interrupted","advisory":True,
                    "judge_error_type":type(exc).__name__[:64],
                    "judge_error_reason":self._judge_error_reason(exc),
                    "failed_orientation":orientation}
        # First call: challenger is A. Second call: challenger is B. Same identity
        # across positions is agreement; same letter means position bias.
        first_identity={"A":"challenger","B":"baseline","tie":"tie"}.get(first.get("winner"),"tie")
        second_identity={"B":"challenger","A":"baseline","tie":"tie"}.get(second.get("winner"),"tie")
        winner=first_identity if first_identity==second_identity else "tie"
        return {"winner":winner,"reason":"paired_advisory_judgment","advisory":True,"scores":[first.get("scores"),second.get("scores")],"flip_disagreement":first_identity!=second_identity}

    @staticmethod
    def _judge_error_reason(exc):
        message=str(exc)
        known={
            "input exceeds the configured text-token estimate limit":"judge_input_cap_exceeded",
            "experiment role returned invalid JSON":"judge_invalid_json",
            "judge response has invalid structure":"judge_invalid_structure",
            "judge_not_advisory":"judge_not_advisory",
            "experiment maximum call count reached for this budget period":"experiment_call_limit_reached",
        }
        if message in known:return known[message]
        code=getattr(exc,"code",None)
        if isinstance(code,str) and re.fullmatch(r"[a-z][a-z0-9_]{0,80}",code):return code
        if isinstance(exc,TimeoutError):return "judge_timeout"
        return "judge_call_failed"

    def run(self, *, run_id: str, dataset: dict, candidate_refs: list[str], mode: str="baseline", shadow: bool=False, execution_identity: dict|None=None, target_scope: str|None=None, baseline_ref: str|None=None, shadow_dataset: dict|None=None, refinement: bool=True) -> dict:
        train,holdout=self._validate_dataset(dataset)
        shadow_cases=[]
        if shadow_dataset is not None:
            shadow_train,shadow_holdout=self._validate_dataset(shadow_dataset)
            shadow_cases=shadow_train+shadow_holdout
            main_groups={group for case in train+holdout for group in _case_groups(case)}
            if main_groups & {group for case in shadow_cases for group in _case_groups(case)}:
                raise ValueError("shadow_dataset_overlaps_selection_dataset")
        if not isinstance(refinement,bool): raise ValueError("refinement must be boolean")
        if mode not in {"baseline","model"}: raise ValueError("invalid_tournament_mode")
        if mode=="model" and not execution_identity: raise ValueError("model_mode_requires_sanitized_execution_identity")
        snapshot=self.registry.snapshot(target_scope) if target_scope is not None else None
        selected_ref=snapshot["ref"] if snapshot else "baseline@1"
        if baseline_ref is not None and baseline_ref!=selected_ref: raise ValueError("baseline_must_match_active_scope")
        baseline_ref=selected_ref
        if len(candidate_refs)<2 or len(candidate_refs)>4 or baseline_ref not in candidate_refs or len(set(candidate_refs))!=len(candidate_refs): raise ValueError("candidate_set_requires_active_baseline_and_2_to_4_configs")
        configs={ref:self.registry.get(ref) for ref in candidate_refs}
        config_hashes={ref:_digest(config.model_dump(mode="json")) for ref,config in configs.items()}
        prompt_hashes={ref:hashlib.sha256(files("intelligence.prompts").joinpath(f"{config.template_version}.system.ru.txt").read_bytes()+files("intelligence.prompts").joinpath(f"{config.template_version}.user.ru.txt").read_bytes()).hexdigest() for ref,config in configs.items()}
        runtime_hashes=_runtime_hashes()
        manifest={"dataset_hash":_digest(dataset),"shadow_dataset_hash":_digest(shadow_dataset) if shadow_dataset is not None else None,"baseline_ref":baseline_ref,"baseline_snapshot":snapshot,"refs":candidate_refs,"config_hashes":config_hashes,"prompt_hashes":prompt_hashes,"runtime_hashes":runtime_hashes,"evaluation_version":EVALUATION_VERSION,"mode":mode,"shadow":bool(shadow),"critic_enabled":self.critic is not None,"refinement_enabled":refinement,"synthetic":dataset["synthetic"],"execution_identity":execution_identity or {}}
        # Provider identities may contain Decimal tariffs; compare their persisted JSON form.
        manifest=json.loads(_json(manifest))
        with self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior=db.execute("SELECT manifest,report FROM runs WHERE run_id=?",(run_id,)).fetchone()
            if prior:
                saved=json.loads(prior["manifest"])
                if saved.get("input_manifest")!=manifest: raise ValueError("run_id_manifest_conflict")
                if prior["report"]: return json.loads(prior["report"])
            else: db.execute("INSERT INTO runs VALUES(?,?,NULL)",(run_id,_json({"input_manifest":manifest})))
        outputs={ref:{"train":[self._run_case(run_id,case,config,"train",execution_identity or {},mode,shadow) for case in train]} for ref,config in configs.items()}
        scores={ref:{"wins":0,"losses":0,"ties":0} for ref in candidate_refs}; pairings={}
        base=outputs[baseline_ref]
        for ref in candidate_refs:
            if ref==baseline_ref: continue
            judgments=[]
            for i,case in enumerate(train):
                j=self._pair(case,outputs[ref]["train"][i],base["train"][i],execution_identity); judgments.append(j)
                field="wins" if j["winner"]=="challenger" else "losses" if j["winner"]=="baseline" else "ties"
                scores[ref][field]+=1
            pairings[ref]=judgments
        leaders=[r for r in candidate_refs if r!=baseline_ref and scores[r]["wins"]>scores[r]["losses"]]
        margins={r:scores[r]["wins"]-scores[r]["losses"] for r in leaders}
        best_margin=max(margins.values(),default=0)
        top=[r for r,value in margins.items() if value==best_margin]
        provisional=top[0] if len(top)==1 else baseline_ref
        critique_result={"used":False,"refinement":{"attempted":False,"applied":False,"reason":"critic_unavailable"}}
        if self.critic is not None and train:
            from .experiments import critic_round, refine_candidate_answer
            critique_ref=provisional
            if critique_ref==baseline_ref:
                critique_ref=max((r for r in candidate_refs if r!=baseline_ref),key=lambda x:scores[x]["wins"]-scores[x]["losses"],default=baseline_ref)
            case=train[0]
            candidate_output=outputs[critique_ref]["train"][0]["candidate"]
            opponent_output=base["train"][0]["candidate"] if critique_ref!=baseline_ref else outputs[baseline_ref]["train"][0]["candidate"]
            critique_case={"request":case["request"],"context":case["request"].get("company"),"question":case["request"].get("event"),
                           "evidence":[{"id":e["evidence_id"],**e} for e in candidate_output.get("evidence",[])]}
            critic_key=_digest({"kind":"critic","version":"critic-refinement-v1","runtime":_runtime_hashes(),"case":critique_case,"answer":self._blind(candidate_output),"opponent":self._blind(opponent_output),"execution_identity":execution_identity or {}})
            try:
                critiques=self.store._reserve(run_id,critic_key)
                if critiques is None:
                    critiques=critic_round(self.critic,case=critique_case,answer=candidate_output,opponent=opponent_output)
                    self.store._complete(critic_key,critiques)
                critique_result={"used":True,"refinement":{"attempted":False,"applied":False,
                    "reason":"disabled" if not refinement else "no_valid_critiques"},"critiques":critiques}
                if refinement and critiques.get("critiques") and critique_ref!=baseline_ref:
                    refinement_key=_digest({"kind":"answer-refinement","version":"answer-refinement-v1","runtime":_runtime_hashes(),
                        "case":critique_case,"answer":self._blind(candidate_output),"critiques":critiques,"execution_identity":execution_identity or {}})
                    refined=self.store._reserve(run_id,refinement_key)
                    if refined is None:
                        refined=refine_candidate_answer(self.critic,case=case,answer=candidate_output,critiques=critiques["critiques"])
                        self.store._complete(refinement_key,refined)
                    if refined.get("refined") is True:
                        prior_candidate=outputs[critique_ref]["train"][0]
                        refined_case=self._run_case(run_id,case,configs[critique_ref],"train_refined",execution_identity or {},mode,shadow,
                                                    candidate_override=refined["candidate"])
                        old=pairings.get(critique_ref,[None]*len(train))[0]
                        if old:
                            field_old="wins" if old["winner"]=="challenger" else "losses" if old["winner"]=="baseline" else "ties"
                            scores[critique_ref][field_old]-=1
                        new_judgement=self._pair(case,refined_case,base["train"][0],execution_identity)
                        if critique_ref in pairings: pairings[critique_ref][0]=new_judgement
                        field_new="wins" if new_judgement["winner"]=="challenger" else "losses" if new_judgement["winner"]=="baseline" else "ties"
                        scores[critique_ref][field_new]+=1
                        outputs[critique_ref]["train"][0]=refined_case
                        critique_result={"used":True,"critiques":critiques,"refinement":{
                            "attempted":True,"applied":True,"case_id":case["case_id"],"candidate_ref":critique_ref,
                            "hard_checks_passed":refined_case["passed"],"checks":refined_case["checks"],
                            "cost":refined.get("cost"),"cost_currency":refined.get("cost_currency"),
                            "cost_source":refined.get("cost_source"),"usage":refined.get("usage"),
                            "scoring_judgment":new_judgement}}
                    else:
                        critique_result["refinement"]={"attempted":True,"applied":False,"reason":refined.get("reason","refinement_rejected")}
                elif refinement and critique_ref==baseline_ref:
                    critique_result["refinement"]={"attempted":False,"applied":False,"reason":"no_challenger_to_refine"}
            except Exception as exc:
                critique_result={"used":True,"refinement":{"attempted":True,"applied":False,
                    "reason":"critic_or_refinement_failed_closed","error_type":type(exc).__name__}}
        # Refinement can change only one training judgement. Re-select before the fresh holdout;
        # shadow observations are deliberately excluded from all winner selection.
        leaders=[r for r in candidate_refs if r!=baseline_ref and scores[r]["wins"]>scores[r]["losses"]]
        margins={r:scores[r]["wins"]-scores[r]["losses"] for r in leaders}
        best_margin=max(margins.values(),default=0)
        top=[r for r,value in margins.items() if value==best_margin]
        provisional=top[0] if len(top)==1 else baseline_ref
        exposure=self.store.expose_holdout(run_id,holdout) if mode=="model" else {"fresh_for_selection":False,"prior_run_count":0}
        outputs[baseline_ref]["holdout"]=[self._run_case(run_id,case,configs[baseline_ref],"holdout",execution_identity or {},mode,shadow) for case in holdout]
        if provisional != baseline_ref: outputs[provisional]["holdout"]=[self._run_case(run_id,case,configs[provisional],"holdout",execution_identity or {},mode,shadow) for case in holdout]
        winner_cases=outputs[provisional]["holdout"] if provisional != baseline_ref else outputs[baseline_ref]["holdout"]
        base_cases=outputs[baseline_ref]["holdout"]
        holdout_judgments=[]
        if provisional!=baseline_ref:
            for i,case in enumerate(holdout):
                holdout_judgments.append(self._pair(case,winner_cases[i],base_cases[i],execution_identity))
        hold_no_regression=all(j["winner"]!="baseline" and j.get("reason")!="judge_failed_or_interrupted" for j in holdout_judgments)
        hold_pass=all(x["passed"] for x in winner_cases) and (provisional==baseline_ref or all(x["passed"] for x in base_cases)) and hold_no_regression
        failures=[f"{r}:{x['case_id']}" for r in dict.fromkeys([provisional,baseline_ref])
                  for x in outputs[r].get("train",[])+outputs[r].get("holdout",[]) if not x["passed"]]
        winner=baseline_ref if provisional!=baseline_ref and (not hold_pass or failures) else provisional
        shadow_validation={"validated":False,"passed":False,"fresh":False,"case_count":0,
                           "reason":"separate_shadow_dataset_not_supplied","comparisons":[]}
        if shadow_dataset is not None:
            exposure_shadow=self.store.expose_shadow(run_id,shadow_cases)
            shadow_results=[]; comparisons=[]
            if winner!=baseline_ref:
                for case in shadow_cases:
                    baseline_result=self._run_case(run_id,case,configs[baseline_ref],"shadow",execution_identity or {},mode,True)
                    candidate_result=self._run_case(run_id,case,configs[winner],"shadow",execution_identity or {},mode,True)
                    comparison=self._pair(case,candidate_result,baseline_result,execution_identity)
                    grounded_kinds={"external_fact","bank_view"}
                    def grounded_refs(output):
                        return {ref for claim in output["candidate"].get("claims",[])
                                if claim.get("kind") in grounded_kinds for ref in claim.get("evidence_refs",[])}
                    baseline_grounded=grounded_refs(baseline_result)
                    candidate_grounded=grounded_refs(candidate_result)
                    facts_preserved=baseline_grounded.issubset(candidate_grounded)
                    shadow_results.append({"case_id":case["case_id"],"baseline_passed":baseline_result["passed"],
                        "candidate_passed":candidate_result["passed"],"baseline_cost":baseline_result["measured_cost"],
                        "candidate_cost":candidate_result["measured_cost"],"baseline_latency_ms":baseline_result["latency_ms"],
                        "candidate_latency_ms":candidate_result["latency_ms"],
                        "baseline_grounded_evidence_refs":sorted(baseline_grounded),
                        "candidate_grounded_evidence_refs":sorted(candidate_grounded),
                        "grounded_evidence_preserved":facts_preserved})
                    comparisons.append({"case_id":case["case_id"],**comparison})
            comparison_complete=bool(comparisons) and all(c.get("reason")!="judge_failed_or_interrupted" for c in comparisons)
            checks_pass=bool(shadow_results) and all(r["baseline_passed"] and r["candidate_passed"] for r in shadow_results)
            candidate_no_regression=bool(comparisons) and all(c.get("winner")!="baseline" for c in comparisons)
            grounded_evidence_preserved=bool(shadow_results) and all(r.get("grounded_evidence_preserved") is True for r in shadow_results)
            real_fresh=exposure_shadow["fresh"] and shadow_dataset["synthetic"] is False and mode=="model"
            shadow_passed=bool(real_fresh and checks_pass and comparison_complete and candidate_no_regression and grounded_evidence_preserved)
            shadow_validation={"validated":shadow_passed,"passed":shadow_passed,"fresh":exposure_shadow["fresh"],
                "prior_run_count":exposure_shadow["prior_run_count"],"case_count":len(shadow_cases),
                "candidate_ref":winner,"baseline_ref":baseline_ref,"synthetic":shadow_dataset["synthetic"],
                "execution_mode":mode,"hard_checks_passed":checks_pass,"comparison_complete":comparison_complete,
                "candidate_no_regression":candidate_no_regression,"grounded_evidence_preserved":grounded_evidence_preserved,
                "results":shadow_results,"comparisons":comparisons,
                "reason":"fresh_model_shadow_compared_without_selection" if shadow_passed else "shadow_validation_gates_not_satisfied"}
        if winner==baseline_ref:
            challenger=max((r for r in candidate_refs if r!=baseline_ref),key=lambda x:scores[x]["wins"]-scores[x]["losses"],default=baseline_ref)
            review_split="train"
            other=challenger
            review_cases=train
        else:
            review_split="holdout"; other=baseline_ref; review_cases=holdout
        label_map={}
        pack=[]
        for i,case in enumerate(review_cases):
            blind_id=_digest([run_id,case["case_id"]])[:16]
            labels={"A":other,"B":winner} if secrets.randbelow(2) else {"A":winner,"B":other}
            label_map[blind_id]=labels
            a=outputs[labels["A"]][review_split][i]["candidate"]
            b=outputs[labels["B"]][review_split][i]["candidate"]
            evidence={e["evidence_id"]:e for out in (a,b) for e in out.get("evidence",[])}
            pack.append({"blind_id":blind_id,"case_id":case["case_id"],"company":case["request"].get("company"),"question":case["request"].get("event"),"evidence":self._blind_evidence(list(evidence.values())),"A":self._blind(a),"B":self._blind(b)})
        report={"schema_version":"tournament-report-v1","run_id":run_id,"baseline_ref":baseline_ref,"baseline_snapshot":snapshot,"winner_ref":winner,"synthetic":dataset["synthetic"],"mode":mode,"shadow":bool(shadow),"shadow_validation":shadow_validation,"measured_quality":False,"training":scores,"pairings":pairings,"refinement":critique_result,
                "candidate_metrics":{r:[{"case_id":x["case_id"],"cost":x["measured_cost"],"known_partial_cost":x["known_partial_cost"],"cost_currency":x["cost_currency"],"cost_complete":x["cost_complete"],"latency_ms":x["latency_ms"]} for x in outputs[r]["train"]] for r in candidate_refs},
                "holdout":{**exposure,"passed":bool(hold_pass),"case_count":len(holdout),"judgments":holdout_judgments,"winner_cases":winner_cases,"baseline_cases":base_cases},"hard_failures":failures,
                "critic":critique_result,
                "human_review":{"approved":False,"reviewer":None,"ratings":None,"pack":pack},"promotion_eligible":False}
        report["report_hash"]=_digest(report)
        with self.store._db() as db:
            saved=json.loads(db.execute("SELECT manifest FROM runs WHERE run_id=?",(run_id,)).fetchone()[0]); saved.update({"input_manifest":manifest,"frozen_report_hash":_digest({k:v for k,v in report.items() if k not in {"human_review","report_hash","promotion_eligible"}}),"review_label_map":label_map})
            db.execute("UPDATE runs SET manifest=?,report=? WHERE run_id=?",(_json(saved),_json(report),run_id))
        return report
