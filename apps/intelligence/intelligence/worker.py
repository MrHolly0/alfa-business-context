from __future__ import annotations

import hashlib
import os
import random
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pydantic import ValidationError

from .artifacts import ArtifactStore, canonical_json, payload_hash
from .llm.pricing import Pricing


class LeaseLost(RuntimeError): pass


class InvalidJob(ValueError): pass


class Worker:
    def __init__(self, core, pipeline, artifact_store: ArtifactStore, *, heartbeat_interval: float = 10.0,
                 clock=time.time, tenant_ids: set[str] | None = None, max_delivery_attempts: int = 8,
                 delivery_batch_size: int = 10, retry_base_seconds: float = 1.0,
                 retry_cap_seconds: float = 3600.0, jitter=random.uniform):
        self.core, self.pipeline, self.artifacts = core, pipeline, artifact_store
        self.heartbeat_interval = max(.05, float(heartbeat_interval))
        self.clock = clock
        self.max_delivery_attempts = max(1, min(8, int(max_delivery_attempts)))
        self.delivery_batch_size = max(1, min(10, int(delivery_batch_size)))
        self.retry_base_seconds = max(.01, float(retry_base_seconds))
        self.retry_cap_seconds = max(self.retry_base_seconds, float(retry_cap_seconds))
        self.jitter = jitter
        self.tenant_ids = set(tenant_ids if tenant_ids is not None else (v.strip() for v in os.getenv("INTELLIGENCE_TENANTS", "").split(",") if v.strip()))

    def _check(self, job):
        request = job.get("analysis")
        scope = job.get("tenant_scope")
        versions = job.get("input_versions")
        gen = job.get("lease_generation")
        if job.get("schema_version") != "analysis-job-v1" or not isinstance(request, dict): raise InvalidJob("invalid job envelope")
        required = ("message_id", "job_id", "tenant_scope", "company_id", "trace_id", "deadline", "lease_until")
        if any(not isinstance(job.get(k), str) or not job[k] or len(job[k]) > 256 for k in required): raise InvalidJob("missing or invalid identifier")
        if not scope or scope not in self.tenant_ids or request.get("tenant_id") != scope or request.get("company_id") != job.get("company_id"):
            raise InvalidJob("scope mismatch")
        if not isinstance(versions, dict) or versions.get("context_version") != request.get("context_version") or versions.get("source_policy_version") != request.get("source_policy_version"):
            raise InvalidJob("input version mismatch")
        expected_events = [{"event_id":request.get("event", {}).get("event_id"), "revision":request.get("event", {}).get("revision",1)}]
        if versions.get("event_versions") != expected_events:
            raise InvalidJob("event version mismatch")
        scope_data = job.get("scope")
        if not isinstance(scope_data, dict): raise InvalidJob("missing source scope")
        if isinstance(scope_data, dict):
            if scope_data.get("source_ids") != request.get("source_ids") or scope_data.get("document_revision_refs", []) != request.get("document_revision_refs", []):
                raise InvalidJob("source scope mismatch")
        if not isinstance(gen, int) or isinstance(gen, bool) or gen < 1: raise InvalidJob("invalid lease generation")
        try:
            deadline_dt = datetime.fromisoformat(job["deadline"].replace("Z", "+00:00"))
            lease_dt = datetime.fromisoformat(job["lease_until"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError): raise InvalidJob("invalid deadline")
        if deadline_dt.tzinfo is None or lease_dt.tzinfo is None: raise InvalidJob("timestamps require timezone")
        deadline, lease_until = deadline_dt.timestamp(), lease_dt.timestamp()
        if deadline <= self.clock(): raise InvalidJob("job expired")
        if lease_until <= self.clock(): raise InvalidJob("lease expired")
        if lease_until > deadline: raise InvalidJob("lease exceeds deadline")
        from .schemas import AnalysisBudget, AnalysisRequest
        try: parsed = AnalysisRequest.model_validate(request)
        except ValidationError as exc: raise InvalidJob("invalid analysis request") from exc
        worker_budget = job.get("budget")
        if not isinstance(worker_budget, dict): raise InvalidJob("missing worker budget")
        try:
            calls = worker_budget["max_model_calls"]
            tokens = worker_budget.get("max_tokens", worker_budget.get("max_total_tokens"))
            reservation = worker_budget["cost_reservation_rub"]
        except (KeyError, TypeError, ValueError): raise InvalidJob("invalid worker budget")
        if not isinstance(reservation, (int, float, Decimal)):
            raise InvalidJob("invalid worker budget")
        try: reservation_decimal = Decimal(str(reservation))
        except (InvalidOperation, ValueError): raise InvalidJob("invalid worker budget")
        if (not isinstance(calls, int) or isinstance(calls, bool) or not isinstance(tokens, int) or isinstance(tokens, bool)
            or isinstance(reservation, bool) or not reservation_decimal.is_finite()):
            raise InvalidJob("invalid worker budget")
        if calls < 0 or tokens < 1 or reservation_decimal < 0: raise InvalidJob("invalid worker budget")
        if parsed.budget.max_model_calls > calls or parsed.budget.max_total_tokens > tokens:
            raise InvalidJob("request exceeds reserved budget")
        llm = getattr(self.pipeline, "llm", None)
        if llm is not None and parsed.budget.max_model_calls:
            config = getattr(llm, "config", None)
            if config is None: raise InvalidJob("model pricing configuration unavailable")
            profile, model = getattr(config, "profile", None), getattr(config, "model", None)
            if profile == "openrouter":
                paid = not str(model).endswith(":free")
                if paid:
                    try:
                        config.ensure_spend_allowed()
                        usd = worker_budget.get("cost_reservation_usd")
                        if isinstance(usd,bool) or not isinstance(usd,(int,float,Decimal)):
                            raise ValueError("explicit USD reservation required")
                        reservation_decimal = Decimal(str(usd))
                        if not reservation_decimal.is_finite() or reservation_decimal < 0:
                            raise ValueError("finite USD reservation required")
                    except (AttributeError,ValueError,InvalidOperation) as exc:
                        raise InvalidJob("paid OpenRouter requires consent and a USD reservation") from exc
                elif config.input_per_million != 0 or config.output_per_million != 0:
                    raise InvalidJob("free OpenRouter requires zero prices")
            elif profile != "hackathon" or getattr(config, "currency", None) != "RUB":
                raise InvalidJob("model profile is not permitted")
            try:
                pricing = Pricing(config.input_per_million, config.output_per_million, config.currency)
                in_tokens = min(parsed.budget.max_input_tokens, tokens)
                out_tokens = min(parsed.budget.max_output_tokens, config.max_output_tokens)
                input_capacity = in_tokens * parsed.budget.max_model_calls
                output_capacity = out_tokens * parsed.budget.max_model_calls
                total_capacity = min(parsed.budget.max_total_tokens, input_capacity + output_capacity)
                if pricing.input_per_million >= pricing.output_per_million:
                    worst_input = min(input_capacity, total_capacity)
                    worst_output = min(output_capacity, max(0, total_capacity - worst_input))
                else:
                    worst_output = min(output_capacity, total_capacity)
                    worst_input = min(input_capacity, max(0, total_capacity - worst_output))
                worst_case = pricing.estimate(worst_input, worst_output)
            except (AttributeError, ValueError, TypeError) as exc: raise InvalidJob("invalid model pricing") from exc
            if worst_case > reservation_decimal: raise InvalidJob("cost reservation is too small")
        return parsed, deadline, lease_until

    def _assert_live(self, job, deadline, lost, lease_state):
        if lost.is_set(): raise LeaseLost("lease lost")
        now = self.clock()
        if now >= deadline: raise LeaseLost("deadline expired")
        with lease_state["lock"]:
            if now >= lease_state["lease_until"]: raise LeaseLost("lease expired")

    def _checkpoint(self, job, request, deadline, lost, lease_state):
        def check(stage: str):
            self._assert_live(job, deadline, lost, lease_state)
            if (request.tenant_id != job.get("tenant_scope") or
                request.company_id != job.get("company_id") or
                job.get("input_versions", {}).get("context_version") != request.context_version or
                job.get("input_versions", {}).get("source_policy_version") != request.source_policy_version):
                raise InvalidJob("snapshot changed")
        return check

    def _heartbeat_loop(self, job, deadline, stop, lost, lease_state):
        while not stop.wait(self.heartbeat_interval):
            if self.clock() >= deadline:
                lost.set(); return
            try:
                ack = self.core.heartbeat(job["job_id"], job["lease_generation"])
                if not isinstance(ack, dict) or ack.get("lease_generation") != job["lease_generation"] or ack.get("accepted") is not True or not isinstance(ack.get("lease_until"), str):
                    lost.set(); return
                renewed = datetime.fromisoformat(ack["lease_until"].replace("Z", "+00:00"))
                if renewed.tzinfo is None or renewed.timestamp() <= self.clock() or renewed.timestamp() > deadline:
                    lost.set(); return
                with lease_state["lock"]: lease_state["lease_until"] = renewed.timestamp()
            except Exception:
                lost.set(); return

    def _delivery_failure(self, entry, reason):
        delay = self.retry_base_seconds * (2 ** max(0, entry["attempts"]-1))
        delay = min(self.retry_cap_seconds, max(0.0, delay * self.jitter(.5, 1.5)))
        self.artifacts.retry_delivery(entry["message_id"], next_attempt_at=self.clock()+delay,
                                      error=reason, max_attempts=self.max_delivery_attempts,
                                      claim_token=entry["claim_token"], now=self.clock())
        return {"status":"pending", "message_id":entry["message_id"]}

    def _deliver_due(self, limit=None):
        delivered, quarantined, pending = [], [], []
        dead_before = {item["message_id"] for item in self.artifacts.dead_letters()}
        limit = self.delivery_batch_size if limit is None else max(0, min(self.delivery_batch_size, int(limit)))
        attempted = set()
        for _ in range(limit):
            entries = self.artifacts.due_pending(self.clock(), 1, self.max_delivery_attempts,
                                                 exclude_ids=attempted, claim_seconds=60)
            if not entries:
                break
            entry = entries[0]
            payload = __import__("json").loads(entry["payload"])
            message_id, claim_token = entry["message_id"], entry["claim_token"]
            attempted.add(message_id)
            if not self.tenant_ids or payload.get("tenant_scope") not in self.tenant_ids:
                if self.artifacts.quarantine(message_id, "tenant_scope_denied", claim_token):
                    quarantined.append(message_id)
                else:
                    pending.append(message_id)
                continue
            try:
                ack = self.core.submit_result(payload)
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 409:
                    changed = self.artifacts.quarantine(message_id, "core_conflict", claim_token)
                    (quarantined if changed else pending).append(message_id)
                elif status in (401, 403):
                    changed = self.artifacts.quarantine(message_id, "authorization_denied", claim_token)
                    (quarantined if changed else pending).append(message_id)
                else:
                    self._delivery_failure(entry, f"transport_{status or type(exc).__name__}")
                    pending.append(message_id)
                continue
            if isinstance(ack, dict) and ack.get("accepted") is True and ack.get("message_id") == message_id:
                if self.artifacts.acknowledge(message_id, claim_token):
                    delivered.append(message_id)
                else:
                    pending.append(message_id)
            elif isinstance(ack, dict) and ack.get("accepted") is False and ack.get("code") in ("stale_lease", "conflict"):
                reason = "stale_lease" if ack.get("code")=="stale_lease" else "core_conflict"
                changed = self.artifacts.quarantine(message_id, reason, claim_token)
                (quarantined if changed else pending).append(message_id)
            elif isinstance(ack, dict) and ack.get("accepted") is False and ack.get("code") == "authorization_denied":
                changed = self.artifacts.quarantine(message_id, "authorization_denied", claim_token)
                (quarantined if changed else pending).append(message_id)
            else:
                self._delivery_failure(entry, "unconfirmed_ack")
                pending.append(message_id)
        dead_after = {item["message_id"] for item in self.artifacts.dead_letters()}
        quarantined.extend(sorted(dead_after - dead_before - set(quarantined)))
        return {"delivered":delivered, "quarantined":quarantined, "pending":pending}

    def run_once(self):
        """Deliver a bounded due batch, then claim and process a new job if available."""
        delivery = self._deliver_due()
        envelope = self.core.claim()
        if not envelope:
            if self.artifacts.has_pending() and not delivery["pending"]:
                delivery["pending"] = self.artifacts.pending_ids(self.delivery_batch_size)
            if delivery["delivered"] or delivery["quarantined"] or delivery["pending"]:
                if delivery["pending"]: status = "pending"
                elif delivery["quarantined"]: status = "quarantined"
                else: status = "retransmitted"
                return {"status":status, "delivery_summary":delivery}
            return None
        job = envelope.get("job") if "job" in envelope else envelope
        if not isinstance(job, dict):
            if delivery["delivered"] or delivery["quarantined"] or delivery["pending"]:
                status = "pending" if delivery["pending"] else ("quarantined" if delivery["quarantined"] else "retransmitted")
                return {"status":status, "delivery_summary":delivery}
            return None
        try:
            request, deadline, lease_until = self._check(job)
        except Exception as exc:
            try: self.core.submit_failure(job.get("job_id", ""), job.get("lease_generation", 0), "invalid_job")
            except Exception: pass
            return {"status": "rejected", "reason": str(exc)}
        stable_inputs = {key:job.get(key) for key in ("job_id", "tenant_scope", "company_id", "trace_id", "input_versions",
                                                       "lease_generation", "deadline", "budget", "scope", "analysis")}
        input_hash = payload_hash(stable_inputs)
        try:
            run = self.artifacts.begin_job(job["job_id"], job["lease_generation"], input_hash)
        except ValueError as exc:
            return {"status":"rejected", "reason":str(exc)}
        if run["status"] == "running": return {"status":"in_progress", "job_id":job["job_id"]}
        if run["status"] == "acknowledged": return {"status":"already_acknowledged", "job_id":job["job_id"]}
        if run["status"] == "complete": return {"status":"outbox_missing", "job_id":job["job_id"]}
        if run["status"] == "dead": return {"status":"quarantined", "job_id":job["job_id"]}
        if run["status"] != "started": return {"status":"rejected", "reason":"invalid run reservation state"}
        stop, lost = threading.Event(), threading.Event()
        lease_state = {"lock": threading.Lock(), "lease_until": lease_until}
        beat = threading.Thread(target=self._heartbeat_loop, args=(job, deadline, stop, lost, lease_state), daemon=True)
        beat.start()
        try:
            self._assert_live(job, deadline, lost, lease_state)
            result = self.pipeline.run(request, checkpoint=self._checkpoint(job, request, deadline, lost, lease_state))
            self._assert_live(job, deadline, lost, lease_state)
            candidate = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            if (candidate.get("tenant_id") != job["tenant_scope"] or candidate.get("company_id") != job["company_id"] or
                candidate.get("request_id") != request.request_id):
                raise InvalidJob("candidate scope mismatch")
            versions = candidate.get("versions", {})
            if versions and (versions.get("context") != request.context_version or
                             versions.get("source_policy") != request.source_policy_version):
                raise InvalidJob("candidate version mismatch")
            identity = canonical_json([job["job_id"], job["lease_generation"], input_hash])
            message_id = hashlib.sha256(identity.encode()).hexdigest()
            payload = {"schema_version": "analysis-result-v1", "message_id": message_id,
                       "job_id": job["job_id"], "tenant_scope": job["tenant_scope"],
                       "company_id": job["company_id"], "trace_id": job["trace_id"],
                       "lease_generation": job["lease_generation"], "input_versions": job["input_versions"],
                       "candidate": candidate, "result_ref": None}
            self._assert_live(job, deadline, lost, lease_state)
            self.artifacts.complete_job(job["job_id"], job["lease_generation"], input_hash, message_id, payload)
        except LeaseLost:
            return {"status": "lease_lost"}
        except Exception:
            try: self.core.submit_failure(job["job_id"], job["lease_generation"], "analysis_failed")
            except Exception: pass
            return {"status": "failed"}
        finally:
            stop.set(); beat.join(timeout=1)
        try:
            self._assert_live(job, deadline, lost, lease_state)
        except LeaseLost:
            self.artifacts.quarantine(message_id, "lease_lost_before_submit")
            self.artifacts.quarantine_job_run(job["job_id"], job["lease_generation"], "lease_lost_before_submit")
            return {"status":"lease_lost"}
        attempted_before = sum(len(delivery[key]) for key in ("delivered", "quarantined", "pending"))
        current_delivery = self._deliver_due(self.delivery_batch_size-attempted_before)
        for key in ("delivered", "quarantined", "pending"):
            delivery[key].extend(current_delivery[key])
        if message_id in delivery["delivered"]:
            status = "submitted"
        elif message_id in delivery["quarantined"]:
            status = "quarantined"
        else:
            status = "pending"
        return {"status":status, "message_id":message_id, "delivery_summary":delivery}
