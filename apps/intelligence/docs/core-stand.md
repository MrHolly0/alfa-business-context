# Fixture-only Core job stand

`intelligence.core_stand` is a local contract-test server for the private Core API. It does not replace production Core, own production scheduling, or create operator permissions. Startup requires explicit `fixture_mode=True` (or CLI `--fixture-mode`) and the CLI refuses non-loopback bind addresses. It has no browser or external-network behavior.

## Run

```sh
cd apps/intelligence
.venv/bin/python -m intelligence core-stand --fixture-mode --state-dir /tmp/alfa-core-stand --port 8765
```

The server prints a random operator token unless `CORE_STAND_OPERATOR_TOKEN` is set by the operator. Keep it local. Use the template task/job to seed fixtures. A stand can also be embedded in a test with `create_app(path, fixture_mode=True, operator_token="test")` and `fastapi.testclient.TestClient`.

You can seed the sample envelope before startup with `--seed examples/core-stand-ai-job.json`. A scheduled fixture can be configured with `--schedule examples/core-stand-ai-task.json --interval 3600`; the task must be `operation=collect`, `max_model_calls=0`, and the interval must be at least 60 seconds. Alternatively, `POST /fixture/schedules` accepts `{"schedule_id":"demo-company-collect","interval_seconds":3600,"task":<ai-data-task-v1>}`. The stand's lifespan checks due schedules and creates normal durable `ai-job-v1` jobs. Each scheduled job gets a unique request ID, current due-time cutoff, `trigger=scheduled`, `knowledge_mode=latest`, effective source IDs from the immutable source-policy snapshot, and existing Core publication proofs. The fixture scheduler cannot spend on a model.

For example, start with the job envelope seed:

```sh
.venv/bin/python -m intelligence core-stand --fixture-mode \
  --state-dir /tmp/alfa-core-stand --seed examples/core-stand-ai-job.json
```

To exercise recurring collection, use the task sample instead:

```sh
.venv/bin/python -m intelligence core-stand --fixture-mode \
  --state-dir /tmp/alfa-core-scheduled --schedule examples/core-stand-ai-task.json \
  --schedule-id demo-company-collect --interval 3600
```

The first job is due immediately; subsequent jobs are at least 60 seconds apart. In another terminal, point the Python worker to the printed local stand URL and token. The sample has no sources, so it proves contract/scheduler plumbing only, not news collection.

Run a baseline worker against that local fixture (replace only the token placeholder with the value printed by the stand):

```sh
cd apps/intelligence
CORE_BASE_URL=http://127.0.0.1:8765 CORE_SERVICE_TOKEN='<fixture token>' \
INTELLIGENCE_TENANTS=demo-tenant .venv/bin/python -m intelligence worker \
  --mode baseline --state-dir /tmp/alfa-core-worker
```

The worker polls until stopped; pass `--once` to claim at most one job and exit. Do not use a production token or real tenant with this fixture.

Seed endpoint: `POST /fixture/seed`, loopback client plus `X-Operator-Token`, JSON `{"job": <envelope>}`. Fixture operator endpoints `GET /fixture/state`, `POST /fixture/jobs/{job_id}/cancel`, `POST /fixture/versions`, `GET /fixture/published-assessments`, and `POST /fixture/schedules` have the same loopback/operator-token gate. They inspect state, cancel a queued/leased job, advance current context/policy versions for a tenant/company, read publication proofs, or register a durable fixture schedule. A changed version makes a previously leased result stale. Fixture endpoints are intentionally separate from the Core worker API.

## Worker API contract

The existing routes and payload shape used by `CoreClient` are preserved:

- `POST /internal/v1/analysis-jobs/claim` returns `{"job": envelope}` or `{"job": null}`.
- `POST /internal/v1/analysis-jobs/{job_id}/heartbeat` accepts `{"lease_generation": n}` and returns an acceptance plus renewed `lease_until`.
- `POST /internal/v1/analysis-jobs/{job_id}/result` accepts either `analysis-result-v1` for the legacy `analysis` body or `ai-result-v1` for `.task` containing `ai-data-task-v1`.
- `POST /internal/v1/analysis-jobs/{job_id}/failure` accepts generation and a bounded failure code.

The envelope carries `input_versions` with `context_version`, `source_policy_version`, and `event_versions`; `budget`, `scope`, deadline, tenant/company/trace identities and generation are required. The stand compares result envelope versions with the seeded job and the latest fixture versions. When a candidate includes its `versions`, it accepts either the legacy keys `context`/`source_policy` or the `*_version` names, and rejects stale values. A repeated byte-equivalent logical result (same canonical JSON and message id) is acknowledged idempotently; a different result after completion returns conflict.

The durable SQLite database contains queued/leased/completed job state, result inbox/outbox records, and publication revisions. AI DataResult revisions are created only when a `revisions[]` item has `publication_revision_required: true`. A `no_change` revision stays in the accepted result without incrementing a publication revision. When an item carries `input_signature`, `(tenant, company, event, input_signature)` is unique across jobs/messages: a retried publication is acknowledged but never increments twice. `CoreStand.published_assessments(tenant, company)` and the loopback-only `GET /fixture/published-assessments?tenant_scope=...&company_id=...` expose exact signature/revision proofs for fixture task snapshots. A local workflow cache is not itself proof that Core published an assessment.

## Test scheduling

`CoreStand.add_schedule(schedule_id, task, interval_seconds)` accepts a validated DataTask fixture and a minimum interval of 60 seconds. `enqueue_due_schedules()` emits at most one ordinary durable `ai-job-v1` job per due schedule and advances the due time from the current fixture clock (no unbounded catch-up loop). The task and policy snapshot remain intact. Production scheduling remains a Core responsibility; the fixture scheduler exists only to exercise the job contract.

The stand’s clock is injectable for deterministic deadline, lease, and due-time tests. Do not point production workers at this fixture server.
