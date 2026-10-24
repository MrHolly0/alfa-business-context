"""Bounded plan → retrieve → check → adapt analysis pipeline."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from importlib.resources import files
from typing import Any, Callable
from collections.abc import Mapping

from pydantic import ValidationError

from .llm.service import estimate_text_tokens
from .parsers import parse_content
from .schemas import AnalysisRequest, CandidateAssessment, CandidateClaim, InfluenceEdge, ModelAnswer
from .analysts import AnalystConfig, PromptRegistry
from .events import ImpactPackage

PROMPT_VERSION = "analysis-v2"
PIPELINE_VERSION = "bounded-pipeline-v1"
MAX_MODEL_EVENT_CHARS = 2_000
MAX_QUERY_CHARS = 320
MAX_EVIDENCE = 16
ANALYST_PREFERENCE_GUIDANCE = (
    "Bounded analyst preference meanings (fixed application guidance): focus values are review lenses, "
    "not claims or evidence requirements; costs means operating/input costs, liquidity means cash and financing, "
    "supply means suppliers/routes/availability, regulation means rules and compliance, growth means demand/capacity, "
    "uncertainty means explicitly state unknowns. retrieval_strategy event_first means search the event channel before company context, "
    "dependencies_first means search company context before the event channel, balanced means search both channels with a balanced evidence allowance. "
    "answer_style concise means prioritize the main finding and one check, "
    "balanced means explain finding, basis, and uncertainty, detailed means include material alternatives and checks. "
    "horizon short means immediate to 30 days, quarter means up to 3 months, long means beyond 3 months. "
    "These preferences never relax grounding, evidence, uncertainty, JSON-schema, or safety requirements."
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class AnalysisPipeline:
    def __init__(self, store, llm=None, *, analyst_config=None, registry=None):
        self.store = store
        self.llm = llm
        self.analyst_config = (analyst_config if isinstance(analyst_config, AnalystConfig)
                               else AnalystConfig.model_validate(analyst_config) if analyst_config is not None
                               else None)
        self.registry = registry
        # Internal orchestration hook. Values still pass through the same scoped
        # CorpusStore search and evidence validators as the deterministic plan.
        self.planned_queries: dict[str, str] = {}
        # Orchestrators may reserve calls for later logical stages (for example critic).
        self.model_call_limit: int | None = None

    def run(self, request: AnalysisRequest, checkpoint: Callable[[str], None] | None = None, *,
            shared_event_package: Mapping[str, Any] | Any | None = None, data_classification: str | None = None) -> CandidateAssessment:
        if not isinstance(request, AnalysisRequest):
            request = AnalysisRequest.model_validate(request)
        config = self.analyst_config or (self.registry.select(role=request.company.role, industry=request.company.industry)
                                         if self.registry else AnalystConfig(config_id="baseline"))
        package = self._normalize_shared_package(shared_event_package, request)
        trace: list[dict] = []
        usage: dict[str, Any] = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0,
                                 "total_tokens": 0, "accounting_basis": "provider_reported",
                                 "attempts": [], "errors": []}
        steps = 0

        def step(stage: str) -> None:
            nonlocal steps
            steps += 1
            trace.append({"stage": stage, "step": steps})
            if steps > request.budget.max_steps:
                raise _StepLimit
            if checkpoint:
                try:
                    checkpoint(stage)
                except BaseException as exc:
                    raise _CheckpointAbort(exc) from exc

        try:
            step("plan")
            event_ingest = self._ingest_event(request)
            trace.append({"stage": "event_ingested", "revision": event_ingest.get("document_revision_ref")})
            trace.append({"stage": "plan_created", "tools": ["search_external", "search_bank"],
                          "max_steps": request.budget.max_steps,
                          "shared_package_ref": package.get("package_id") if package else None})
            evidence = self._retrieve(request, step, trace=trace, event_ref=event_ingest["document_revision_ref"], config=config)
            step("check")
            supported_context = self._has_active_company_context(request)
            external_found = any(e["source_role"] == "external" for e in evidence)
            bank_found = any(e["source_role"] == "bank" for e in evidence)
            trace.append({"stage": "check_result", "external_evidence": external_found,
                          "bank_evidence": bank_found, "confirmed_company_context": supported_context})
            if not evidence or not bank_found or not supported_context:
                # Adapt by broadening only within the same tenant, sources and time boundary.
                step("adapt")
                trace.append({"stage": "adapt_plan", "reason": ("no_evidence" if not evidence else
                              "missing_bank_material" if not bank_found else "missing_confirmed_company_context"),
                              "strategy": "broaden_bank_context_query"})
                primary_count = len(evidence)
                adapted = self._retrieve(request, step, broad=True, trace=trace, config=config)
                by_id = {item["evidence_id"]: item for item in evidence}
                by_id.update({item["evidence_id"]: item for item in adapted})
                evidence = list(by_id.values())[:config.evidence_limit]
                trace.append({"stage": "adapt_result", "primary_count": primary_count,
                              "additional_count": len(adapted), "evidence_count": len(evidence),
                              "external_evidence": sum(e["source_role"] == "external" for e in evidence),
                              "bank_evidence": sum(e["source_role"] == "bank" for e in evidence)})
            if self.llm is None:
                candidate = self._baseline(request, evidence, trace, usage)
            else:
                candidate = self._model_candidate(request, evidence, trace, usage, step, config, package=package, data_classification=data_classification)
            candidate = self._validate_candidate(candidate, request, evidence, usage, trace, event_ingest, config)
            if package:
                candidate.shared_package_ref = package.get("package_id")
                candidate.usage["shared_package_ref"] = package.get("package_id")
            return candidate
        except _CheckpointAbort as abort:
            raise abort.original
        except _StepLimit:
            usage["errors"].append("step_budget_exhausted")
            return self._unknown(request, trace, usage, "partial", config=config)
        except Exception as exc:
            # Avoid returning exception text, which may include untrusted/provider content.
            usage["errors"].append(type(exc).__name__)
            return self._unknown(request, trace, usage, "partial", config=config)

    @staticmethod
    def _normalize_shared_package(package, request):
        if package is None:
            return None
        validated = ImpactPackage.model_validate(package)
        value = validated.model_dump(mode="json")
        if value.get("tenant_id") != request.tenant_id:
            raise ValueError("shared_event_package_tenant_mismatch")
        if value.get("event_id") != request.event.event_id:
            raise ValueError("shared_event_package_event_mismatch")
        if str(value.get("policy_version")) != str(request.source_policy_version):
            raise ValueError("shared_event_package_policy_version_mismatch")
        if request.document_revision_refs and any(source.get("document_revision_ref") not in request.document_revision_refs for source in value["sources"]):
            raise ValueError("shared_event_package_document_snapshot_mismatch")
        if value.get("revision") != request.event.revision:
            raise ValueError("shared_event_package_revision_mismatch")
        if any(source.get("source_id") not in request.source_ids or source.get("source_role") not in {"external", "bank"}
               for source in value["sources"]):
            raise ValueError("shared_event_package_source_scope_mismatch")
        # The package can guide reasoning, but its claims are not CorpusStore evidence refs.
        return value

    def _ingest_event(self, request: AnalysisRequest) -> dict:
        if request.event.document_revision_ref:
            if request.document_revision_refs and request.event.document_revision_ref not in request.document_revision_refs:
                raise ValueError("event_revision_outside_snapshot")
            documents = self.store.list_documents(tenant_id=request.tenant_id, source_ids=[request.event.source_id], as_of=request.as_of.isoformat())
            original = next((d for d in documents if d["document_revision_ref"] == request.event.document_revision_ref), None)
            if original is None or original["content"] != request.event.text or original["title"] != request.event.title:
                raise ValueError("event_revision_mismatch")
            return original
        as_of = request.as_of.isoformat()
        return self.store.ingest({
            "tenant_id": request.tenant_id,
            "source_id": request.event.source_id,
            "source_role": "external",
            "canonical_uri": f"urn:event:{request.event.event_id}",
            "title": request.event.title,
            "content": request.event.text,
            "content_type": "text/plain",
            "published_at": request.event.published_at.isoformat(),
            "fetched_at": as_of,
            "synthetic": bool(request.synthetic or request.event.synthetic),
            "publisher": "",
            "language": "ru",
        })

    def _retrieve(self, request: AnalysisRequest, step, broad: bool = False, trace=None, event_ref=None, config=None) -> list[dict]:
        # A small, deterministic plan: external and bank channels are queried separately.
        results: list[dict] = []
        primary_event_ids: set[str] = set()
        order = ("external", "bank") if config is None or config.retrieval_strategy != "dependencies_first" else ("bank", "external")
        for role in order:
            step("tool_search_" + role)
            if role == "external":
                terms = [request.event.title]
                if not broad:
                    terms.append(request.event.text[:MAX_QUERY_CHARS])
            else:
                terms = [d.label for d in request.company.dependencies if d.status == "confirmed"]
                terms += [f.text for f in request.company.facts]
                if broad:
                    terms += [request.company.name, request.company.description, request.company.role, request.company.industry]
                # Analyst profile expansions are constrained query terms. They
                # only broaden the existing bank corpus search; they are never
                # interpreted as instructions or used to change source scope.
                if config is not None:
                    terms += list(getattr(config, "retrieval_expansions", ()))
                terms.append(request.event.title)
            # Preserve profile terms before long event text so they survive query bounds.
            query = " ".join(terms)[:MAX_QUERY_CHARS]
            planned = self.planned_queries.get(role)
            if planned:
                if role == "bank":
                    query = (str(planned) + " " + " ".join(terms))[:MAX_QUERY_CHARS]
                else:
                    query = (str(planned) if not broad else " ".join(terms) + " " + str(planned))[:MAX_QUERY_CHARS]
            pinned = []
            if role == "external" and event_ref:
                pinned = self.store.search(tenant_id=request.tenant_id, source_ids=[request.event.source_id],
                    as_of=request.as_of.isoformat(), query=request.event.text[:MAX_QUERY_CHARS],
                    document_revision_refs=[event_ref], role="external", limit=4)
                primary_event_ids.update(str(item.get("evidence_id")) for item in pinned if item.get("evidence_id"))
            found = self.store.search(
                tenant_id=request.tenant_id,
                source_ids=request.source_ids,
                as_of=request.as_of.isoformat(),
                query=query,
                document_revision_refs=request.document_revision_refs or None,
                role=role,
                limit=min(8, config.evidence_limit if config else MAX_EVIDENCE),
            )
            found = pinned + found
            trace_ids = [item.get("evidence_id") for item in found if isinstance(item, dict) and item.get("evidence_id")]
            step_trace = {"stage": "tool_result", "tool": "search", "role": role,
                          "result_count": len(found), "evidence_ids": trace_ids[:8]}
            # The trace records identifiers/counts only; source text remains in evidence.
            if trace is not None:
                trace.append(step_trace)
            results.extend(found)
        unique: dict[str, dict] = {}
        for item in results:
            if isinstance(item, dict) and item.get("evidence_id"):
                if item.get("source_id") not in request.source_ids or item.get("source_role") not in {"external", "bank"}:
                    continue
                if len(item.get("quote", "")) > 1000 or not item.get("quote"):
                    continue
                if hashlib.sha256(item["quote"].encode("utf-8")).hexdigest() != item.get("content_hash"):
                    continue
                published = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
                fetched = datetime.fromisoformat(item["fetched_at"].replace("Z", "+00:00"))
                if published > request.as_of or fetched > request.as_of:
                    continue
                unique[str(item["evidence_id"])] = item
        ordered = sorted(unique.values(), key=lambda item: (item["evidence_id"] not in primary_event_ids,))
        return ordered[:(config.evidence_limit if config else MAX_EVIDENCE)]

    @staticmethod
    def _context_is_current(item, as_of):
        valid_from = getattr(item, "valid_from", None)
        valid_until = getattr(item, "valid_until", None)
        return not ((valid_from is not None and valid_from > as_of) or
                    (valid_until is not None and valid_until < as_of))

    @classmethod
    def _has_active_company_context(cls, request):
        return (any(d.status == "confirmed" and cls._context_is_current(d, request.as_of)
                    for d in request.company.dependencies) or
                any(cls._context_is_current(f, request.as_of) for f in request.company.facts))

    @staticmethod
    def _primary_ids(request, evidence):
        original = parse_content(request.event.text, "text/plain").text
        return {e["evidence_id"] for e in evidence if e["source_role"] == "external"
                and e["source_id"] == request.event.source_id and e["quote"] in original}

    def _model_candidate(self, request, evidence, trace, usage, step, config=None, *, package=None, data_classification=None) -> dict:
        profile=getattr(getattr(self.llm,"config",None),"profile",None)
        if profile in {"openrouter", "hackathon"} and (not (request.synthetic or data_classification in {"public","synthetic"}) or
                any(item.get("data_classification","confidential") not in {"public","synthetic"} for item in evidence)):
            usage["errors"].append("external_model_data_classification_denied")
            return self._empty_payload(evidence)
        if not self._primary_ids(request, evidence):
            usage["errors"].append("primary_event_evidence_missing")
            return self._empty_payload(evidence)
        configured_output = getattr(getattr(self.llm, "config", None), "max_output_tokens", request.budget.max_output_tokens)
        max_output = min(request.budget.max_output_tokens, configured_output)
        max_calls = min(request.budget.max_model_calls, self.model_call_limit if self.model_call_limit is not None else 2)
        if max_calls < 1:
            usage["errors"].append("model_call_budget_zero")
            return self._empty_payload(evidence)
        context = self._context_payload(request)
        if context["event_text_truncated"]:
            trace.append({"stage": "event_context_bounded", "original_characters": len(request.event.text),
                          "projected_characters": len(context["event"]["text"])})
        user_base = files("intelligence.prompts").joinpath(f"{PROMPT_VERSION}.user.ru.txt").read_text("utf-8")
        system = files("intelligence.prompts").joinpath(f"{PROMPT_VERSION}.system.ru.txt").read_text("utf-8")
        payload = {"REQUEST_CONTEXT": context, "RETRIEVED_EVIDENCE": evidence,
                   "ANALYST_PREFERENCES": {"focus": config.focus, "answer_style": config.answer_style,
                      "horizon": config.horizon, "retrieval_strategy": config.retrieval_strategy},
                   "OUTPUT_JSON_SCHEMA": ModelAnswer.model_json_schema(),
                   "PRIMARY_EVENT_EVIDENCE_IDS": sorted(self._primary_ids(request,evidence)),
                   "CONFIRMED_CONTEXT_IDS": [d.dependency_id for d in request.company.dependencies if d.status=="confirmed" and self._context_is_current(d,request.as_of)] + [f.fact_id for f in request.company.facts if self._context_is_current(f,request.as_of)],
                   "UNCONFIRMED_CONTEXT_IDS": [d.dependency_id for d in request.company.dependencies if d.status!="confirmed"],
                   "CONTEXT_REFERENCE_RULE": "context_refs contains only dependency_id or fact_id; model_hypothesis and recommendation may cite confirmed dependencies/facts only.",
                   "HORIZON_AND_CONFIRMATION_GUIDANCE": "For each hypothesis or recommendation, state a bounded horizon and observable confirmation_conditions. Do not invent a condition; if unavailable, ask a specific question in clarifications. Keep alternatives and falsifiers separate."}
        # Explicit user permission covers only public/synthetic shared material.
        # Both the execution grant and every persisted source must allow disclosure;
        # a task label never upgrades a confidential package or source.
        if (package and data_classification in {"public","synthetic"}
                and package.get("data_classification") in {"public","synthetic"}
                and all(source.get("data_classification") in {"public","synthetic"} for source in package["sources"])):
            payload["SHARED_EVENT_PACKAGE"]={
                "package_id":package["package_id"],"data_classification":package["data_classification"],
                "claims":[{key:claim.get(key) for key in ("claim_id","kind","statement","quote","source_id")} for claim in package["claims"][:8]],
                "mechanisms":package.get("mechanisms",[])[:4],"resources":package.get("resources",[])[:12],
                "unknowns":package.get("unknowns",[])[:8],
                "rule":"Untrusted attributed hypotheses, not independent evidence. Cite only RETRIEVED_EVIDENCE IDs; return to originals when needed."}
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user_base + "\n" + ANALYST_PREFERENCE_GUIDANCE + "\n" + _json(payload)}]
        repair = False
        reserved_total = 0
        for attempt in range(max_calls):
            step("before_llm" if not repair else "before_llm_repair")
            packed = messages if not repair else messages + [{"role": "assistant", "content": raw}, {
                "role": "user", "content": "Исправь JSON по замечаниям валидатора. Сохрани только проверяемые утверждения. Ошибки: " + _json(errors)
            }]
            estimated = estimate_text_tokens(packed)
            remaining = request.budget.max_total_tokens - reserved_total
            output_cap = min(max_output, remaining - estimated)
            input_cap = min(request.budget.max_input_tokens, remaining - max_output)
            if repair and estimated > input_cap:
                # A verbose rejected answer must not crowd out a bounded repair.
                # Regenerate from the same evidence and validator diagnostics; do
                # not truncate source quotes or loosen grounding checks.
                packed = messages + [{"role": "user", "content":
                    "Предыдущий ответ отклонён. Составь новый короткий JSON по тем же данным. "
                    "Ошибки: " + _json(errors) +
                    ". Достаточно 2–4 claims и 0–2 influence_chain. Не используй банковские цитаты в гипотезах."}]
                estimated = estimate_text_tokens(packed)
                output_cap = min(max_output, remaining - estimated)
                trace.append({"stage": "repair_regeneration", "reason": "rejected_answer_exceeds_input_budget"})
            if estimated > input_cap or input_cap < 1 or output_cap < 1:
                usage["errors"].append("input_token_budget_exhausted")
                break
            # Reserve both directions before the call, even if the provider omits usage.
            reserved_total += estimated + output_cap
            usage["model_calls"] += 1
            try:
                result = self.llm.complete(packed, max_input_tokens=input_cap,
                                           max_output_tokens=output_cap, json_mode=True)
                raw = result.get("content", "")
                report = result.get("usage")
                measured_in = getattr(report, "prompt_tokens", None) if not isinstance(report, dict) else report.get("prompt_tokens")
                measured_out = getattr(report, "completion_tokens", None) if not isinstance(report, dict) else report.get("completion_tokens")
                usage_reported = report is not None and isinstance(measured_in, int) and isinstance(measured_out, int)
                recorded_in = measured_in if usage_reported else estimated
                recorded_out = measured_out if usage_reported else output_cap
                usage["input_tokens"] += recorded_in
                usage["output_tokens"] += recorded_out
                usage["total_tokens"] += recorded_in + recorded_out
                if not usage_reported:
                    usage["accounting_basis"] = "reserved_estimate_due_to_missing_provider_usage"
                usage["attempts"].append({"model": result.get("model"), "response_id": result.get("response_id"),
                                          "input_tokens": measured_in if usage_reported else None,
                                          "output_tokens": measured_out if usage_reported else None,
                                          "reserved_input_tokens": estimated,
                                          "reserved_output_tokens": output_cap,
                                          "cost": str(result.get("cost")) if result.get("cost") is not None else None,
                                          "cost_currency": result.get("cost_currency"),
                                          "cost_source": result.get("cost_source"),
                                          "projected_hackathon_cost_rub": (str(result["projected_hackathon_cost_rub"])
                                                                            if result.get("projected_hackathon_cost_rub") is not None else None),
                                          "projected_hackathon_basis": result.get("projected_hackathon_basis")})
            except Exception as exc:
                meta = getattr(exc, "usage", None)
                error_in = getattr(meta, "prompt_tokens", None) if meta is not None else None
                error_out = getattr(meta, "completion_tokens", None) if meta is not None else None
                if isinstance(meta, dict):
                    error_in, error_out = meta.get("prompt_tokens"), meta.get("completion_tokens")
                error_in = error_in if isinstance(error_in, int) else 0
                error_out = error_out if isinstance(error_out, int) else 0
                has_error_usage = error_in > 0 or error_out > 0
                recorded_error_in = error_in if has_error_usage else estimated
                recorded_error_out = error_out if has_error_usage else output_cap
                usage["input_tokens"] += recorded_error_in
                usage["output_tokens"] += recorded_error_out
                usage["total_tokens"] += recorded_error_in + recorded_error_out
                if not has_error_usage:
                    usage["accounting_basis"] = "reserved_estimate_due_to_missing_provider_usage"
                usage["attempts"].append({"provider_error": getattr(exc, "code", type(exc).__name__),
                                          "http_status": getattr(exc,"http_status",None),
                                          "billing_uncertain": getattr(exc,"billing_uncertain",True),
                                          "usage": ({"prompt_tokens": error_in, "completion_tokens": error_out,
                                                     "reported_cost": str(meta.get("reported_cost") if isinstance(meta, dict) else getattr(meta, "reported_cost", None))
                                                     if (meta.get("reported_cost") if isinstance(meta, dict) else getattr(meta, "reported_cost", None)) is not None else None,
                                                     "cost_currency": meta.get("cost_currency") if isinstance(meta, dict) else getattr(meta, "cost_currency", None)} if meta is not None else None),
                                          "reserved_input_tokens": estimated, "reserved_output_tokens": output_cap})
                usage["errors"].append(getattr(exc, "code", type(exc).__name__))
                step("after_llm_error" if not repair else "after_llm_repair_error")
                break
            step("after_llm" if not repair else "after_llm_repair")
            if not usage_reported:
                usage["errors"].append("provider_usage_missing;repair_stopped")
                break
            try:
                parsed = json.loads(raw)
                self._check_model_payload(parsed, request, evidence)
                return parsed
            except (ValueError, TypeError, ValidationError, json.JSONDecodeError) as exc:
                errors = self._repair_diagnostics(exc)
                trace.append({"stage": "model_output_rejected", "reason": type(exc).__name__, "diagnostics": errors})
                if attempt + 1 >= max_calls:
                    usage["errors"].append("model_output_rejected:" + type(exc).__name__)
                    break
                repair = True
        return self._empty_payload(evidence)

    @staticmethod
    def _check_model_payload(payload, request, evidence):
        if not isinstance(payload, dict):
            raise ValueError("invalid_model_shape")
        answer = ModelAnswer.model_validate(payload)
        payload.clear()
        payload.update(answer.model_dump(mode="json"))
        normalized_claims = [CandidateClaim.model_validate(c) for c in payload["claims"]]
        claim_ids = [c.claim_id for c in normalized_claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("duplicate_claim_id")
        payload["claims"] = [c.model_dump(mode="json") for c in normalized_claims]
        refs = {x["evidence_id"]: x for x in evidence}
        primary_ids = AnalysisPipeline._primary_ids(request, evidence)
        contexts = {x.dependency_id: x for x in request.company.dependencies}
        contexts.update({x.fact_id: x for x in request.company.facts})
        for claim_index, claim in enumerate(payload["claims"]):
            # CandidateClaim validation above normalizes defaults before ref checking.
            if any(ref not in refs for ref in claim["evidence_refs"]):
                raise ValueError("unknown_evidence_id")
            if any(ref not in contexts for ref in claim["context_refs"]):
                raise ValueError("unknown_context_id")
            if any(not AnalysisPipeline._context_is_current(contexts[ref], request.as_of)
                   for ref in claim["context_refs"]):
                raise ValueError("context_outside_validity_window")
            if claim["kind"] == "external_fact" and (not claim["evidence_refs"] or any(refs[r]["source_role"] != "external" for r in claim["evidence_refs"])):
                raise _GroundingError("external_fact_requires_external_evidence", ["claims", claim_index, "evidence_refs"])
            if claim["kind"] == "bank_view":
                if claim.get("origin") != "bank" or not claim["evidence_refs"] or any(refs[r]["source_role"] != "bank" for r in claim["evidence_refs"]):
                    raise _GroundingError("bank_view_requires_bank_evidence", ["claims", claim_index, "evidence_refs"])
            if claim["kind"] in {"model_hypothesis", "recommendation"} and not primary_ids.intersection(claim["evidence_refs"]):
                raise ValueError("impact_requires_primary_event_evidence")
            if claim["kind"] == "model_hypothesis":
                if not claim["context_refs"] or not claim["evidence_refs"] or not claim["alternatives"] or not claim["falsifiers"]:
                    raise ValueError("hypothesis_missing_context_or_checks")
                if any(refs[r]["source_role"] != "external" for r in claim["evidence_refs"]):
                    raise ValueError("hypothesis_requires_external_event_evidence")
                for ref in claim["context_refs"]:
                    item = contexts[ref]
                    if getattr(item, "status", "confirmed") != "confirmed":
                        raise ValueError("hypothesis_uses_unconfirmed_context")
            if claim["kind"] == "recommendation":
                if (not claim["evidence_refs"] or not claim["context_refs"]
                        or any(refs[r]["source_role"] != "external" for r in claim["evidence_refs"])):
                    raise ValueError("recommendation_requires_event_evidence")
                if any(getattr(contexts[r], "status", "confirmed") != "confirmed" for r in claim["context_refs"]):
                    raise ValueError("recommendation_uses_unconfirmed_context")
            if claim["kind"] == "unknown" and claim.get("origin") == "bank" and claim["evidence_refs"]:
                raise ValueError("bank_unknown_must_not_cite_evidence")
        for edge_index, item in enumerate(payload["influence_chain"]):
            normalized = InfluenceEdge.model_validate(item)
            item.update(normalized.model_dump(mode="json"))
            if item["status"] != "hypothesis":
                raise ValueError("model_influence_edge_must_be_hypothesis")
            if not item["evidence_refs"] or not item["context_refs"] or not item["alternatives"] or not item["falsifiers"]:
                raise ValueError("hypothesis_chain_missing_basis_or_checks")
            if len(set(item["evidence_refs"])) != len(item["evidence_refs"]):
                raise ValueError("duplicate_chain_evidence_ref")
            if len(set(item["context_refs"])) != len(item["context_refs"]):
                raise ValueError("duplicate_chain_context_ref")
            for ref in item.get("evidence_refs", []):
                if ref not in refs:
                    raise ValueError("unknown_chain_evidence")
                if refs[ref]["source_role"] != "external":
                    raise _GroundingError("influence_edge_requires_external_evidence", ["influence_chain", edge_index, "evidence_refs"])
            if not primary_ids.intersection(item["evidence_refs"]):
                raise _GroundingError("influence_edge_requires_primary_event_evidence", ["influence_chain", edge_index, "evidence_refs"])
            for ref in item.get("context_refs", []):
                if ref not in contexts:
                    raise ValueError("unknown_chain_context")
                if getattr(contexts[ref], "status", "confirmed") != "confirmed":
                    raise ValueError("chain_uses_unconfirmed_context")
                if not AnalysisPipeline._context_is_current(contexts[ref], request.as_of):
                    raise ValueError("chain_context_outside_validity_window")
        edge_ids = [item["edge_id"] for item in payload["influence_chain"]]
        if len(set(edge_ids)) != len(edge_ids):
            raise ValueError("duplicate_edge_id")

    @staticmethod
    def _repair_diagnostics(exc: Exception) -> list[dict[str, Any]]:
        """Return validator codes and locations only; never echo untrusted input."""
        if isinstance(exc, _GroundingError):
            return [{"type": exc.code, "loc": exc.loc}]
        if isinstance(exc, ValidationError):
            return [{"type": str(item.get("type", "validation_error")),
                     "loc": list(item.get("loc", []))} for item in exc.errors(include_input=False)]
        return [{"type": str(exc), "loc": []}]

    def _baseline(self, request, evidence, trace, usage):
        trace.append({"stage": "baseline"})
        external = [e for e in evidence if e["source_role"] == "external"]
        bank = [e for e in evidence if e["source_role"] == "bank"]
        claims = []
        if external:
            claims.append({"claim_id": "event-evidence", "kind": "external_fact",
                           "text": f"Внешний материал описывает событие: {external[0]['quote'][:500]}",
                           "evidence_refs": [external[0]["evidence_id"]], "context_refs": [],
                           "assumptions": [], "alternatives": [], "falsifiers": []})
        if bank:
            claims.append({"claim_id": "bank-position", "kind": "bank_view",
                           "text": f"В банковском материале указано: {bank[0]['quote'][:500]}",
                           "evidence_refs": [bank[0]["evidence_id"]], "context_refs": [],
                           "assumptions": [], "alternatives": [], "falsifiers": [], "origin": "bank"})
        else:
            claims.append({"claim_id": "bank-unknown", "kind": "unknown",
                           "text": "Среди использованных материалов не подтверждена позиция банка по этому событию.",
                           "evidence_refs": [], "context_refs": [], "assumptions": [],
                           "alternatives": [], "falsifiers": [], "origin": "bank"})
        supported = self._has_active_company_context(request)
        status = "partial" if not external or not supported else "ready"
        return {"summary": "Детерминированный baseline: автоматическая причинная оценка не выполнялась.",
                "claims": claims, "influence_chain": [],
                "clarifications": [] if supported else ["Подтвердите зависимости или факты компании, связанные с событием."]}, status

    def _validate_candidate(self, payload, request, evidence, usage, trace, event_ingest, config=None):
        status = "partial"
        if isinstance(payload, tuple):
            payload, status = payload
        elif self.llm is not None and not usage["errors"]:
            status = "partial"
        try:
            self._check_model_payload(payload, request, evidence)
            claims = [CandidateClaim.model_validate(c) for c in payload["claims"]]
        except Exception as exc:
            usage["errors"].append("candidate_validation:" + type(exc).__name__)
            return self._unknown(request, trace, usage, "partial", evidence, config)
        missing_confirmation = False
        for claim in claims:
            if claim.kind in {"model_hypothesis", "recommendation"}:
                if not claim.horizon:
                    claim.horizon = config.horizon if config else "quarter"
                if not claim.confirmation_conditions:
                    missing_confirmation = True
        if (self.llm is not None and not usage["errors"]
                and any(e["source_role"] == "external" for e in evidence)
                and self._has_active_company_context(request)
                and any(c.kind in {"model_hypothesis", "recommendation"} for c in claims)):
            status = "ready"
        if not any(c.kind == "bank_view" for c in claims):
            if not any(c.kind == "unknown" and c.origin == "bank" for c in claims):
                claim_id = "bank-unknown"
                if any(c.claim_id == claim_id for c in claims):
                    claim_id = "bank-unknown-generated"
                claims.append(CandidateClaim(claim_id=claim_id, kind="unknown",
                    text="Среди использованных материалов не подтверждена позиция банка по этому событию.",
                    origin="bank"))
            status = "partial" if status == "ready" else status
        if any(c.kind == "unknown" and c.origin == "bank" for c in claims):
            status = "partial"
        if not evidence:
            status = "unknown"
        elif (not any(e["source_role"] == "external" for e in evidence)
              or not self._has_active_company_context(request)
              or not any(c.kind in {"model_hypothesis", "recommendation"} for c in claims)):
            status = "partial"
        clarifications = list(payload.get("clarifications", []))
        if missing_confirmation:
            clarifications.append("Уточните наблюдаемый показатель или событие, которое подтвердит гипотезу.")
            status = "partial"
        limitations = []
        if len(request.event.text) > MAX_MODEL_EVENT_CHARS:
            limitations.append("event_text_excerpt_only")
            if status == "ready": status = "partial"
        return CandidateAssessment(request_id=request.request_id, tenant_id=request.tenant_id,
            company_id=request.company_id, status=status,
            execution_mode="baseline" if self.llm is None else "model",
            summary=payload.get("summary", "Оснований для надёжного вывода недостаточно."), claims=claims,
            evidence=evidence,
            influence_chain=payload.get("influence_chain", []),
            clarifications=list(dict.fromkeys(clarifications))[:30],
            coverage={"external": sum(e["source_role"] == "external" for e in evidence),
                      "bank": sum(e["source_role"] == "bank" for e in evidence),
                      "confirmed_dependencies": sum(d.status == "confirmed" and self._context_is_current(d, request.as_of)
                                                     for d in request.company.dependencies),
                      "company_facts": len(request.company.facts), "limitations": limitations},
            versions={"prompt": PROMPT_VERSION, "analyst_config": config.ref if config else "baseline@1", "pipeline": PIPELINE_VERSION,
                      "model": next((a.get("model") for a in reversed(usage["attempts"]) if a.get("model")), None),
                      "context": request.context_version, "source_policy": request.source_policy_version,
                      "as_of": request.as_of, "event_id": request.event.event_id,
                      "event_revision": request.event.revision,
                      "event_document_revision_ref": event_ingest.get("document_revision_ref", "unavailable"),
                      "synthetic": bool(request.synthetic or request.event.synthetic)}, usage=usage,
            trace=trace, created_at=datetime.now(timezone.utc))

    @staticmethod
    def _context_payload(request):
        event = request.event.model_dump(mode="json")
        # This projection never changes the request or its pinned corpus revision.
        # Exact primary quotes remain available in RETRIEVED_EVIDENCE.
        event["text"] = request.event.text[:MAX_MODEL_EVENT_CHARS]
        return {"company": request.company.model_dump(mode="json"), "event": event,
                "event_original_characters": len(request.event.text),
                "event_text_truncated": len(request.event.text) > MAX_MODEL_EVENT_CHARS,
                "context_version": request.context_version,
                "source_policy_version": request.source_policy_version}

    @staticmethod
    def _empty_payload(evidence):
        return {"summary": "Модель не вернула проверяемый разбор.", "claims": [],
                "influence_chain": [], "clarifications": ["Проверьте исходные материалы и контекст компании."]}

    def _unknown(self, request, trace, usage, status, evidence=None, config=None):
        evidence = evidence or []
        bank_missing = not any(e.get("source_role") == "bank" for e in evidence)
        unknown_claim = CandidateClaim(claim_id="bank-unknown" if bank_missing else "analysis-unknown",
            kind="unknown", text=("Среди использованных материалов не подтверждена позиция банка по этому событию."
                                   if bank_missing else "Не удалось получить проверяемый вывод из доступных материалов."),
            origin="bank" if bank_missing else None)
        return CandidateAssessment(request_id=request.request_id, tenant_id=request.tenant_id,
            company_id=request.company_id, status=status,
            execution_mode="baseline" if self.llm is None else "model",
            summary="Данных недостаточно для обоснованного вывода о влиянии события.",
            claims=[unknown_claim], evidence=evidence, influence_chain=[],
            clarifications=["Добавьте подтверждённые материалы и проверьте контекст компании."],
            coverage={"external": sum(e.get("source_role") == "external" for e in evidence),
                      "bank": sum(e.get("source_role") == "bank" for e in evidence),
                      "confirmed_dependencies": sum(d.status == "confirmed" and self._context_is_current(d, request.as_of)
                                                     for d in request.company.dependencies),
                      "company_facts": len(request.company.facts)},
            versions={"prompt": PROMPT_VERSION, "analyst_config": config.ref if config else self.analyst_config.ref if self.analyst_config else "baseline@1", "pipeline": PIPELINE_VERSION, "model": None,
                      "context": request.context_version, "source_policy": request.source_policy_version,
                      "as_of": request.as_of, "event_id": request.event.event_id,
                      "event_revision": request.event.revision,
                      "event_document_revision_ref": "unavailable",
                      "synthetic": bool(request.synthetic or request.event.synthetic)}, usage=usage,
            trace=trace, created_at=datetime.now(timezone.utc))


class _StepLimit(Exception):
    pass


class _CheckpointAbort(Exception):
    def __init__(self, original: BaseException):
        super().__init__(type(original).__name__)
        self.original = original


class _GroundingError(ValueError):
    """A fixed validator code and location, without source text or model content."""
    def __init__(self, code: str, loc: list):
        super().__init__(code)
        self.code, self.loc = code, loc
