"""Bounded research → analysis → critique orchestration.

All roles use the existing LLM service and the normal evidence/grounding checks.
The researcher can only propose short search terms; it never receives tools.
"""
from __future__ import annotations

import json
from importlib.resources import files
from typing import Any, Callable
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .analysts import AnalystConfig
from .llm.service import estimate_text_tokens
from .pipeline import AnalysisPipeline, MAX_QUERY_CHARS
from .company_context import project_company_context, context_research_leads
from .schemas import AnalysisRequest, CandidateAssessment


class ResearchSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern="^(external|bank)$")
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    searches: list[ResearchSearch] = Field(max_length=4)
    missing_context: list[str] = Field(default_factory=list, max_length=8)


class Critique(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(accept|review)$")
    issues: list[dict] = Field(default_factory=list, max_length=20)
    clarifications: list[str] = Field(default_factory=list, max_length=10)
    searches: list[ResearchSearch] = Field(default_factory=list, max_length=2)


class _SharedSession:
    """One hard request-level reservation across all role services."""
    def __init__(self, request: AnalysisRequest):
        self.request = request
        self.calls = 0
        self.reserved_tokens = 0
        self.measured_input = 0
        self.measured_output = 0
        self.accounted_input = 0
        self.accounted_output = 0
        self.budget_overrun = False
        self.attempts: list[dict] = []
        self.errors: list[str] = []
        self.steps = 0
        self.checkpoint_exception: BaseException | None = None

    def complete(self, service, messages, *, role, **kwargs):
        if service is None:
            raise RuntimeError("role_service_unavailable")
        budget = self.request.budget
        estimate = estimate_text_tokens(messages)
        remaining = budget.max_total_tokens - self.reserved_tokens
        config = getattr(service, "config", None)
        service_output = getattr(config, "max_output_tokens", budget.max_output_tokens)
        max_input = min(kwargs.get("max_input_tokens", budget.max_input_tokens), budget.max_input_tokens, remaining - 1)
        max_output = min(kwargs.get("max_output_tokens", budget.max_output_tokens), budget.max_output_tokens,
                         service_output, remaining - estimate)
        if self.calls >= budget.max_model_calls or estimate > max_input or max_input < 1 or max_output < 1:
            raise RuntimeError("request_model_budget_exhausted")
        # Reserve conservatively before entering a service; its own ledger remains authoritative for spend.
        self.calls += 1
        self.reserved_tokens += estimate + max_output
        try:
            result = service.complete(messages, max_input_tokens=max_input,
                                      max_output_tokens=max_output, json_mode=kwargs.get("json_mode", True))
        except Exception as exc:
            meta = getattr(exc, "usage", None)
            pin = getattr(meta, "prompt_tokens", None) if meta is not None and not isinstance(meta, dict) else (meta or {}).get("prompt_tokens")
            pout = getattr(meta, "completion_tokens", None) if meta is not None and not isinstance(meta, dict) else (meta or {}).get("completion_tokens")
            pin = pin if isinstance(pin, int) and pin >= 0 else estimate
            pout = pout if isinstance(pout, int) and pout >= 0 else max_output
            self.accounted_input += pin
            self.accounted_output += pout
            if pin + pout > estimate + max_output:
                self.budget_overrun = True
            self.attempts.append({"role": role, "provider_error": getattr(exc, "code", type(exc).__name__),
                                  "billing_uncertain": getattr(exc, "billing_uncertain", True),
                                  "reserved_input_tokens": estimate, "reserved_output_tokens": max_output,
                                  "input_tokens": pin if meta is not None else None,
                                  "output_tokens": pout if meta is not None else None})
            self.errors.append(getattr(exc, "code", type(exc).__name__))
            raise
        usage = result.get("usage")
        pin = getattr(usage, "prompt_tokens", None) if not isinstance(usage, dict) else usage.get("prompt_tokens")
        pout = getattr(usage, "completion_tokens", None) if not isinstance(usage, dict) else usage.get("completion_tokens")
        if isinstance(pin, int) and isinstance(pout, int):
            self.measured_input += pin
            self.measured_output += pout
        accounted_in = pin if isinstance(pin, int) and pin >= 0 else estimate
        accounted_out = pout if isinstance(pout, int) and pout >= 0 else max_output
        self.accounted_input += accounted_in
        self.accounted_output += accounted_out
        if accounted_in + accounted_out > estimate + max_output:
            self.budget_overrun = True
        self.attempts.append({"role": role, "model": result.get("model"),
                              "response_id": result.get("response_id"), "reserved_input_tokens": estimate,
                              "reserved_output_tokens": max_output,
                              "input_tokens": pin if isinstance(pin, int) else None,
                              "output_tokens": pout if isinstance(pout, int) else None,
                              "accounting_basis": "provider_reported" if isinstance(pin, int) and isinstance(pout, int) else "reserved_estimate",
                              "cost": str(result.get("cost")) if result.get("cost") is not None else None,
                              "cost_currency": result.get("cost_currency")})
        return result


class _RoleProxy:
    def __init__(self, service, session, role):
        self.service, self.session, self.role_name = service, session, role
        self.config = getattr(service, "config", None)

    def complete(self, messages, **kwargs):
        try:
            return self.session.complete(self.service, messages, role=self.role_name, **kwargs)
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            if not self.session.errors or self.session.errors[-1] != code:
                self.session.errors.append(code)
            raise


class MultiAgentPipeline:
    """Run bounded researcher, existing analyst pipeline, then advisory critic."""
    def __init__(self, store, llm=None, *, analyst_config=None, registry=None, role_services=None):
        self.store, self.llm = store, llm
        self.analyst_config = (analyst_config if isinstance(analyst_config, AnalystConfig)
                               else AnalystConfig.model_validate(analyst_config) if analyst_config is not None else None)
        self.registry = registry
        self.role_services = dict(role_services or {})
        configured = {(getattr(getattr(service, "config", None), "budget_scope", None),
                       getattr(getattr(service, "config", None), "currency", None))
                      for service in [llm, *self.role_services.values()] if service is not None and
                      getattr(getattr(service, "config", None), "budget_scope", None) is not None}
        if len(configured) > 1:
            raise ValueError("multi-agent roles must share account budget scope and currency")
        ledgers = {str(Path(service.ledger.path).resolve()) for service in [llm, *self.role_services.values()]
                   if service is not None and getattr(service, "ledger", None) is not None and
                   getattr(service.ledger, "path", None) not in (None, ":memory:")}
        if len(ledgers) > 1:
            raise ValueError("multi-agent roles must share one account ledger")

    def run(self, request: AnalysisRequest, checkpoint: Callable[[str], None] | None = None, *,
            shared_event_package: Mapping[str, Any] | Any | None = None, data_classification: str | None = None,
            case_approval=None) -> CandidateAssessment:
        if not isinstance(request, AnalysisRequest):
            request = AnalysisRequest.model_validate(request)
        config = self.analyst_config or (self.registry.select(role=request.company.role, industry=request.company.industry)
                                         if self.registry else AnalystConfig(config_id="baseline"))
        services = {"researcher": self.role_services.get("researcher", self.llm),
                    "analyst": self.role_services.get("analyst", self.llm),
                    "critic": self.role_services.get("critic", self.llm)}
        from .case_permission import request_case_documents, role_may_receive_case_data
        case_documents = request_case_documents(self.store, request)

        def role_allowed(role, *, evidence=(), package_for_role=None):
            if package_for_role is None:
                package_for_role = package
            return role_may_receive_case_data(request, case_documents, services[role],
                case_approval=case_approval, evidence=evidence, package=package_for_role,
                data_classification=data_classification)

        session = _SharedSession(request)
        trace: list[dict] = [{"stage": "workflow_started", "workflow": "research-analysis-critic-v1"}]
        package = None
        package_error = None
        try:
            package = AnalysisPipeline._normalize_shared_package(shared_event_package, request)
        except (ValueError, TypeError) as exc:
            package_error = type(exc).__name__
            trace.append({"stage": "shared_package_rejected", "reason": package_error})
        if package:
            trace.append({"stage": "shared_package_loaded", "package_id": package.get("package_id"),
                          "revision": package.get("revision")})

        def checkpoint_stage(stage):
            # Lifecycle callbacks are cancellation boundaries rather than work
            # steps; model calls are bounded separately by max_model_calls.
            if stage in {"before_research", "after_research", "before_critic", "after_critic",
                         "before_critic_adaptation"}:
                if checkpoint:
                    try:
                        checkpoint(stage)
                    except BaseException as exc:
                        session.checkpoint_exception = exc
                        raise
                return
            session.steps += 1
            if session.steps > request.budget.max_steps:
                raise _WorkflowBudgetExhausted("step_budget_exhausted")
            if checkpoint:
                checkpoint(stage)

        planned: dict[str, str] = {}
        missing: list[str] = []
        research_complete = False
        researcher = services["researcher"]
        if (researcher is not None and role_allowed("researcher") and request.budget.max_model_calls > 0 and
                session.steps < request.budget.max_steps):
            checkpoint_stage("before_research")
            try:
                messages = self._research_messages(request, config=config)
                result = _RoleProxy(researcher, session, "researcher").complete(
                    messages, max_input_tokens=request.budget.max_input_tokens,
                    max_output_tokens=min(512, request.budget.max_output_tokens), json_mode=True)
                plan = ResearchPlan.model_validate(json.loads(result.get("content", "")))
                for item in plan.searches:
                    role, query = item.role, item.query
                    if role in {"external", "bank"} and isinstance(query, str) and query.strip():
                        planned[role] = " ".join(filter(None, [planned.get(role), query.strip()]))[:MAX_QUERY_CHARS]
                missing = plan.missing_context
                research_complete = bool(planned)
                trace.append({"stage": "research_plan", "search_roles": sorted(planned),
                              "query_lengths": {k: len(v) for k, v in planned.items()},
                              "missing_context_count": len(missing), "search_count": len(plan.searches)})
                if not research_complete:
                    trace.append({"stage": "research_unavailable", "reason": "empty_search_plan"})
            except (ValueError, TypeError, ValidationError, json.JSONDecodeError, RuntimeError) as exc:
                trace.append({"stage": "research_unavailable", "reason": type(exc).__name__})
            finally:
                checkpoint_stage("after_research")
        else:
            # The researcher is a logical phase: a deterministic bounded plan is safe
            # when no role model is assigned, and still uses only CorpusStore tools.
            planned = self._default_search_plan(request, config=config)
            research_complete = True
            trace.append({"stage": "research_plan", "planner": "deterministic",
                          "search_roles": sorted(planned), "query_lengths": {k: len(v) for k, v in planned.items()}})
            if researcher is not None and not role_allowed("researcher"):
                trace.append({"stage": "research_model_skipped", "reason": "case_material_not_approved_for_role"})

        analyst = services["analyst"]
        calls_left = max(0, request.budget.max_model_calls - session.calls)
        reserve_critic = bool(services["critic"] and calls_left >= 2)
        analyst_limit = calls_left
        analysis = AnalysisPipeline(self.store, _RoleProxy(analyst, session, "analyst") if analyst else None,
                                    analyst_config=config, registry=self.registry)
        analysis.planned_queries = planned
        analysis.model_call_limit = min(analyst_limit, 2)
        # Reduce the inner stage allowance by researcher steps already consumed.
        remaining_steps = max(1, request.budget.max_steps - session.steps)
        analysis_request = request.model_copy(update={
            "budget": request.budget.model_copy(update={"max_model_calls": analyst_limit,
                                                         "max_steps": remaining_steps})})
        try:
            candidate = analysis.run(analysis_request, checkpoint=checkpoint_stage,shared_event_package=package,
                                     data_classification=data_classification, case_approval=case_approval)
        except _WorkflowBudgetExhausted:
            candidate = analysis.run(request.model_copy(update={"budget": request.budget.model_copy(
                update={"max_model_calls": 0, "max_steps": 1})}), checkpoint=None,
                )
            candidate.status = "partial"
            candidate.clarifications = list(dict.fromkeys(candidate.clarifications + [
                "Анализ остановлен на границе лимита шагов; данных для завершённого вывода недостаточно."]))[:30]
        trace.extend(candidate.trace)
        trace.append({"stage": "analyst_handoff", "status": candidate.status,
                      "evidence_count": len(candidate.evidence), "candidate_claim_count": len(candidate.claims)})

        critic = services["critic"]
        critique_data = None
        if (critic is not None and role_allowed("critic", evidence=candidate.evidence) and
                session.calls < request.budget.max_model_calls and
                session.steps < request.budget.max_steps):
            checkpoint_stage("before_critic")
            try:
                messages = self._critic_messages(request, candidate, include_synthetic_company=(
                    request.synthetic and data_classification == "synthetic" and bool(candidate.evidence)
                    and all(item.data_classification == "synthetic" for item in candidate.evidence)), config=config)
                critic_payload = json.loads(messages[-1]["content"])
                trace.append({"stage": "critic_evidence_projected",
                    "evidence_available": len(candidate.evidence),
                    "evidence_sent": len(critic_payload["evidence"]),
                    "evidence_omitted": len(candidate.evidence) - len(critic_payload["evidence"]),
                    "candidate_citations_matched": critic_payload["evidence_projection"]["candidate_citations_matched"],
                    "primary_fallback_used": critic_payload["evidence_projection"]["primary_fallback_used"]})
                result = _RoleProxy(critic, session, "critic").complete(
                    messages, max_input_tokens=request.budget.max_input_tokens,
                    max_output_tokens=min(1024, request.budget.max_output_tokens), json_mode=True)
                critique = Critique.model_validate(json.loads(result.get("content", "")))
                known = ({c.claim_id for c in candidate.claims}, {e.edge_id for e in candidate.influence_chain},
                         {e.evidence_id for e in candidate.evidence})
                valid_issues = []
                invalid_issue_count = 0
                for issue in critique.issues:
                    target, reason = issue.get("target_id"), issue.get("reason")
                    refs = issue.get("evidence_refs", [])
                    if (not isinstance(target, str) or not isinstance(reason, str) or
                            target not in known[0] | known[1] | {"summary"} or not isinstance(refs, list) or not refs or
                            any(ref not in known[2] for ref in refs)):
                        invalid_issue_count += 1
                        continue
                    valid_issues.append({"target_id": target, "reason": reason[:1000], "evidence_refs": refs})
                critique_data = {"mode": "semantic", "decision": critique.decision, "issues": valid_issues,
                                 "invalid_issue_count": invalid_issue_count,
                                 "clarifications": [x[:1000] for x in critique.clarifications],
                                 "search_count": len(critique.searches)}
                if valid_issues or critique.decision == "review":
                    candidate.status = "partial" if candidate.status == "ready" else candidate.status
                    review_notes = critique_data["clarifications"] or ["Критик запросил ручную проверку оснований."]
                    candidate.clarifications = list(dict.fromkeys(candidate.clarifications + review_notes))[:30]
                trace.append({"stage": "critic_review", "decision": critique.decision,
                              "validated_issue_count": len(valid_issues)})
                if critique.decision == "review" and valid_issues:
                    candidate, adaptation = self._adapt_after_critique(
                        request, candidate, critique.searches, services["analyst"], session,
                        checkpoint_stage, trace, data_classification=data_classification, package=package,
                        case_approval=case_approval, case_documents=case_documents)
                    critique_data["adaptation"] = adaptation
            except (ValueError, TypeError, ValidationError, json.JSONDecodeError, RuntimeError) as exc:
                if session.checkpoint_exception is exc:
                    raise
                trace.append({"stage": "critic_unavailable", "reason": type(exc).__name__})
            finally:
                checkpoint_stage("after_critic")
        else:
            # Structural grounding/shape checks have already run in AnalysisPipeline.
            # Record that semantic critique was skipped rather than implying it happened.
            critique_data = {"mode": "structural_only", "decision": "not_evaluated", "issues": [],
                             "clarifications": []}
            trace.append({"stage": "critic_unavailable", "reason": "role_or_budget_unavailable",
                          "structural_checks_completed": True})
            if critic is not None and not role_allowed("critic", evidence=candidate.evidence):
                trace[-1]["reason"] = "case_material_not_approved_for_role"

        merged_errors = list(dict.fromkeys([*candidate.usage.get("errors", []), *session.errors]))
        candidate.usage = {**candidate.usage, "workflow": "research-analysis-critic-v1",
                           "model_calls": session.calls,
                           "input_tokens": session.accounted_input,
                           "output_tokens": session.accounted_output,
                           "total_tokens": session.accounted_input + session.accounted_output,
                           "reserved_tokens": session.reserved_tokens,
                           "accounting_basis": "provider_reported_and_reserved_estimate" if session.measured_input or session.measured_output else "reserved_estimate",
                           "budget_overrun": session.budget_overrun,
                           "attempts": session.attempts, "errors": merged_errors,
                           "research_missing_context": missing,
                           "critic_review": critique_data}
        if package:
            candidate.usage["shared_package_ref"] = package.get("package_id")
            candidate.shared_package_ref = package.get("package_id")
        if package_error:
            candidate.usage["shared_package_error"] = package_error
        if hasattr(candidate.coverage, "limitations"):
            limitations = list(candidate.coverage.limitations)
            limitations.extend(["external_web_search_unavailable", "independent_origin_count_unavailable"])
            if package_error:
                limitations.append("shared_event_package_rejected")
            if critique_data and critique_data.get("mode") == "structural_only":
                limitations.append("semantic_critic_not_run")
            candidate.coverage.limitations = list(dict.fromkeys(limitations))[:30]
        if hasattr(candidate.coverage, "checked_at"):
            candidate.coverage.checked_at = datetime.now(timezone.utc)
        if hasattr(candidate.coverage, "independent_origins"):
            candidate.coverage.independent_origins = 0
        if hasattr(candidate, "watch_conditions"):
            watches = [condition for claim in candidate.claims
                       for condition in (getattr(claim, "confirmation_conditions", []) + claim.falsifiers)]
            candidate.watch_conditions = list(dict.fromkeys(watches))[:50]
        if missing:
            questions = ["Уточните: " + item.strip()[:900] for item in missing if isinstance(item, str) and item.strip()]
            candidate.clarifications = list(dict.fromkeys([*candidate.clarifications, *questions]))[:30]
        if session.budget_overrun or session.accounted_input + session.accounted_output > request.budget.max_total_tokens:
            candidate.status = "partial" if candidate.status == "ready" else candidate.status
            candidate.usage["budget_overrun"] = True
            if "Фактическое использование превысило резерв; завершённый вывод требует проверки." not in candidate.clarifications:
                candidate.clarifications = list(dict.fromkeys(candidate.clarifications +
                    ["Фактическое использование превысило резерв; завершённый вывод требует проверки."]))[:30]
        research_leads=context_research_leads(project_company_context(request.company,as_of=request.as_of))
        search_results=[item for item in trace if item.get("stage")=="tool_result" and item.get("tool")=="search"]
        search_scope={"mode":"authorized_corpus_only","network_search_available":False,
            "allowed_source_ids":list(request.source_ids),
            "snapshot_document_count":len(request.document_revision_refs),
            "selected_evidence_count":len(candidate.evidence),
            "unconfirmed_lead_ids":[item["item_id"] for item in research_leads],
            "unconfirmed_leads_are_evidence":False,
            "additional_search":"critic_queries_in_same_corpus_and_budget",
            "source_expansion":"requires_new_Core_source_policy",
            "empty_search_roles":sorted({item.get("role") for item in search_results if item.get("result_count")==0})}
        candidate.usage["search_coverage"]=search_scope
        trace.append({"stage":"research_coverage",**search_scope})
        if research_leads:
            candidate.coverage.limitations=list(dict.fromkeys(["unconfirmed_dependencies_research_only",
                *candidate.coverage.limitations]))[:30]
        if candidate.status in {"partial","unknown"}:
            candidate.clarifications=list(dict.fromkeys([
                "Поиск ограничен разрешённым корпусом. Ненайденная связь не означает отсутствия риска; для новых источников нужен снимок политики Core.",
                *candidate.clarifications]))[:30]
        candidate.trace = trace
        if (not research_complete or critique_data is None or package_error) and candidate.status == "ready":
            candidate.status = "partial"
            candidate.clarifications = list(dict.fromkeys(candidate.clarifications +
                ["Не все логические этапы исследования и проверки удалось выполнить в пределах доступного бюджета."]))[:30]
        if critique_data and critique_data.get("mode") == "structural_only" and candidate.status == "ready":
            candidate.status = "partial"
            candidate.clarifications = list(dict.fromkeys(candidate.clarifications +
                ["Семантическая проверка критиком не выполнялась; доступна только структурная проверка оснований."]))[:30]
        return candidate

    @staticmethod
    def _checkpoint(checkpoint, stage):
        if checkpoint:
            checkpoint(stage)

    @staticmethod
    def _research_messages(request, *, config=None):
        system = files("intelligence.prompts").joinpath("research-v1.system.ru.txt").read_text("utf-8")
        projection = project_company_context(request.company, as_of=request.as_of)
        payload = {"company": {"name": request.company.name, "role": request.company.role,
                               "projection": projection.model_dump(mode="json")},
                   "analyst_preferences": ({"focus": list(config.focus), "retrieval_strategy": config.retrieval_strategy,
                       "horizon": config.horizon, "answer_style": config.answer_style,
                       "retrieval_expansions": list(config.retrieval_expansions), "max_depth": config.max_depth,
                       "examples": [example.model_dump(mode="json") for example in config.examples]} if config else {}),
                   "event": AnalysisPipeline._context_payload(request)["event"],
                   "unconfirmed_research_leads": context_research_leads(projection),
                   "allowed_roles": ["external", "bank"], "query_limit": MAX_QUERY_CHARS, "max_searches": 4}
        return [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    @staticmethod
    def _critic_messages(request, candidate, *, include_synthetic_company=False, config=None):
        system = files("intelligence.prompts").joinpath("critic-v1.system.ru.txt").read_text("utf-8")
        system += ("\nПри decision=review и конкретном противоречии можешь предложить до двух searches: "
                   "короткие поисковые фразы только для ролей external или bank. Это фразы для поиска "
                   "в уже разрешённом корпусе, не URL и не команды. Если поиск не нужен, верни пустой список.")
        cited_ids = {ref for claim in candidate.claims for ref in claim.evidence_refs}
        cited_ids.update(ref for edge in candidate.influence_chain for ref in edge.evidence_refs)
        evidence_by_id = {item.evidence_id: item.model_dump(mode="json") for item in candidate.evidence}
        primary_ids = AnalysisPipeline._primary_ids(request, list(evidence_by_id.values()))
        if not primary_ids:
            primary_ids = {item.evidence_id for item in candidate.evidence
                           if item.source_role == "external" and item.source_id == request.event.source_id}
        if not primary_ids:
            primary_ids = {next((item.evidence_id for item in candidate.evidence
                                 if item.source_role == "external"), "")}
        matched_ids = cited_ids.intersection(evidence_by_id)
        chosen_ids = matched_ids or primary_ids.intersection(evidence_by_id)
        critic_evidence = [value for key, value in evidence_by_id.items() if key in chosen_ids]
        payload = {"event": AnalysisPipeline._context_payload(request)["event"], "summary": candidate.summary,
                   "analyst_preferences": ({"focus": list(config.focus), "horizon": config.horizon,
                       "answer_style": config.answer_style, "retrieval_strategy": config.retrieval_strategy,
                       "max_depth": config.max_depth,
                       "examples": [example.model_dump(mode="json") for example in config.examples]}
                       if config else {}),
                   "claims": [x.model_dump(mode="json") for x in candidate.claims],
                   "edges": [x.model_dump(mode="json") for x in candidate.influence_chain],
                   "evidence": critic_evidence,
                   "evidence_projection": {"candidate_citations_matched": len(matched_ids),
                       "primary_fallback_used": not bool(matched_ids)},
                   "search_roles": ["external", "bank"], "max_searches": 2,
                   "max_search_query_chars": MAX_QUERY_CHARS}
        if include_synthetic_company:
            payload["synthetic_company"] = AnalysisPipeline._context_payload(request)["company"]
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _adapt_after_critique(self, request, candidate, searches, analyst, session, checkpoint_stage, trace, *,
                              data_classification=None, package=None, case_approval=None, case_documents=()):
        """Run one critic-directed corpus retrieval and analyst pass under the original budget."""
        def unavailable(reason):
            limitation = "critic_adaptation_" + reason
            candidate.coverage.limitations = list(dict.fromkeys(
                [*candidate.coverage.limitations, limitation]))[:30]
            trace.append({"stage": "critic_adaptation_unavailable", "reason": reason})
            return candidate, {"status": "unavailable", "reason": reason}

        if not searches:
            return unavailable("no_valid_searches")
        if analyst is None or session.calls >= request.budget.max_model_calls:
            return unavailable("model_call_budget_unavailable")
        from .case_permission import role_may_receive_case_data
        if not role_may_receive_case_data(request, case_documents, analyst, case_approval=case_approval,
                evidence=candidate.evidence, package=package, data_classification=data_classification):
            return unavailable("case_material_not_approved_for_role")
        if session.steps >= request.budget.max_steps:
            return unavailable("step_budget_unavailable")

        planned: dict[str, str] = {}
        for item in searches:
            role, query = item.role, item.query.strip()
            if role not in {"external", "bank"} or not query:
                continue
            planned[role] = " ".join(filter(None, [planned.get(role), query]))[:MAX_QUERY_CHARS]
        if not planned:
            return unavailable("no_valid_searches")

        old_refs = {item.evidence_id for item in candidate.evidence}
        remaining_calls = request.budget.max_model_calls - session.calls
        remaining_steps = request.budget.max_steps - session.steps
        remaining_tokens = request.budget.max_total_tokens - session.reserved_tokens
        if remaining_tokens < 2:
            return unavailable("token_budget_unavailable")

        trace.append({"stage": "critic_adaptation_started", "search_roles": sorted(planned),
                      "query_lengths": {role: len(query) for role, query in planned.items()},
                      "remaining_calls": remaining_calls, "remaining_steps": remaining_steps})
        checkpoint_stage("before_critic_adaptation")
        config = self.analyst_config or (self.registry.select(role=request.company.role, industry=request.company.industry)
                                         if self.registry else AnalystConfig(config_id="baseline"))
        analysis = AnalysisPipeline(self.store, _RoleProxy(analyst, session, "analyst"),
                                    analyst_config=config, registry=self.registry)
        analysis.planned_queries = planned
        analysis.model_call_limit = 1
        adapted_request = request.model_copy(update={"budget": request.budget.model_copy(update={
            "max_model_calls": 1, "max_steps": remaining_steps,
            "max_total_tokens": remaining_tokens,
        })})
        try:
            adapted = analysis.run(adapted_request, checkpoint=checkpoint_stage,
                                   shared_event_package=package, data_classification=data_classification,
                                   case_approval=case_approval)
        except _WorkflowBudgetExhausted:
            return unavailable("shared_budget_exhausted")
        except BaseException:
            # Lease cancellation and other checkpoint aborts must propagate unchanged.
            raise
        new_refs = sorted({item.evidence_id for item in adapted.evidence} - old_refs)
        if not new_refs:
            candidate.status = "partial"
            candidate.coverage.limitations = list(dict.fromkeys(
                [*candidate.coverage.limitations, "critic_adaptation_no_new_selected_evidence"]))[:30]
            candidate.clarifications = list(dict.fromkeys(candidate.clarifications + [
                "Поиск после критики не дал новых процитированных оснований; противоречие требует ручной проверки."]))[:30]
            trace.append({"stage": "critic_adaptation_incomplete", "reason": "no_new_selected_evidence"})
            return candidate, {"status": "incomplete", "reason": "no_new_selected_evidence", "new_evidence_refs": []}

        retained = {item.evidence_id: item for item in candidate.evidence}
        for item in adapted.evidence:
            retained.setdefault(item.evidence_id, item)
        adapted.evidence = list(retained.values())
        adapted.status = "partial"
        adapted.clarifications = list(dict.fromkeys(adapted.clarifications + [
            "После адаптации нужны повторная семантическая проверка и подтверждение оснований."]))[:30]
        limitations = list(adapted.coverage.limitations)
        limitations.append("critic_adaptation_not_recritiqued")
        adapted.coverage.limitations = list(dict.fromkeys(limitations))[:30]
        trace.extend(adapted.trace)
        trace.append({"stage": "critic_adaptation_complete", "new_selected_evidence_refs": new_refs,
                      "status": "partial_pending_recritique"})
        return adapted, {"status": "complete_pending_recritique", "new_evidence_refs": new_refs,
                         "model_calls": session.calls}

    @staticmethod
    def _default_search_plan(request, *, config=None):
        projection = project_company_context(request.company, as_of=request.as_of)
        company_terms = [d.label for d in projection.dependencies]
        company_terms += [f.text for f in projection.facts]
        company_terms += [a.label for a in projection.activities]
        company_terms += [a.label for a in projection.assertions]
        if config:
            company_terms += list(config.retrieval_expansions)
        leads=context_research_leads(projection)
        # Keep a separate bounded portion for unknown relations so a long known
        # profile cannot silently remove all exploratory terms.
        external=" ".join((request.event.title[:100]," ".join(company_terms)[:110],
                            " ".join(item["label"] for item in leads)[:110]))[:MAX_QUERY_CHARS]
        return {"external": external,
                "bank": " ".join((request.company.name, *company_terms,
                    *(item["label"] for item in leads), request.event.title))[:MAX_QUERY_CHARS]}


class _WorkflowBudgetExhausted(RuntimeError):
    pass
