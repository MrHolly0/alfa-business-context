# Event grouping, shared impact packages and period research

`intelligence.events.EventStore` adds a durable, tenant-scoped event layer beside `CorpusStore`. It accepts Corpus-compatible document fields, groups publication revisions, extracts attributed claims, and caches a shared `ImpactPackage`. `intelligence.research` provides evidence-bounded dependency paths, observation-only period calculations, and reassessment signatures. These are Python module contracts; they are not yet wired into the existing analysis pipeline or HTTP/job entry points.

## Ingest a Corpus document

```python
from intelligence.events import EventStore

store = EventStore("/var/lib/intelligence/events.sqlite3")
publication = {
    "tenant_id": "tenant-a",
    "source_id": "src-1",
    "source_role": "external",
    "canonical_uri": "https://news.example/story?utm_source=feed",
    "title": "Засуха в регионе",
    "content": "Издатель сообщает о засухе в регионе.",
    "content_type": "text/plain",
    "published_at": "2026-10-24T08:00:00+03:00",
    "fetched_at": "2026-10-24T08:03:00+03:00",
    "synthetic": False,
    "data_classification": "public",
    "publisher": "News",
    "author": "Редакция",
    "document_revision_ref": "doc_x:1",
}
stored = store.ingest(publication)
```

The returned value contains `tenant_id`, `event_id`, event `revision`, `publication_id`, `publication_revision`, `content_hash`, grouping reason, and whether the stored state changed. In production, `data_classification` is supplied from the persisted Core source grant, never trusted from a task payload. Canonical URL identity is scoped by tenant and source. Tracking parameters and fragments are removed. Re-fetching identical content is idempotent. A changed publication appends a revision; adding an independently sourced copy updates the event snapshot. Near-duplicate grouping uses a high text similarity threshold and a seven-day publication window. It is a review grouping hint only and never increases the independent-origin count. Similar subject matter below that threshold remains separate.

`origin_group_id` may be supplied by the source adapter. Otherwise the canonical host is used as a conservative origin group. The store never assumes that different URLs or similar texts are independent. Each ingested publication revision also keeps the Core-assigned `data_classification`. An input marked `synthetic=True` is stored as `synthetic`; otherwise the classification defaults to `confidential`. A task-level request cannot upgrade a persisted source classification.

## Read under the current source policy

Every read or package build requires tenant, policy identity/version, and the allowed source IDs supplied by Core:

```python
snapshot = store.get_event(
    stored["tenant_id"], stored["event_id"],
    source_policy_id="source-policy-7", policy_version="12",
    allowed_source_ids=["src-1", "src-2"],
)
original = store.original(
    stored["tenant_id"], stored["event_id"], stored["publication_id"],
    source_policy_id="source-policy-7", policy_version="12",
    allowed_source_ids=["src-1"],
)
```

Policy filtering is applied before returning source text or building claims. A source excluded from the current policy cannot enter the package from another publication, event revision, or tenant. Callers may request an earlier event revision for audit; its publication content is pinned to the exact version recorded in that snapshot.
Callers that have a narrower Core document snapshot can also pass `allowed_document_revision_refs=[...]` to `get_event`, `original`, `extract_claims`, or `build_impact_package`; reads and package sources are then restricted to those exact references.

## Claims and shared package

```python
package = store.build_impact_package(
    stored["tenant_id"], stored["event_id"],
    source_policy_id="source-policy-7", policy_version="12",
    allowed_source_ids=["src-1", "src-2"],
    llm=llm_service,  # optional
)
payload = package.model_dump(mode="json")
```

`ImpactPackage` is a Pydantic model and includes `package_id`, tenant/event/revision, claims, source references, coverage, model/prompt/policy versions, and bounded literal entities, territories and resources. `mechanisms` contains conditional `ImpactMechanism` hypotheses with an explicit horizon, conditions, alternatives, falsifiers, and the publication IDs supporting the mechanism. Every mechanism reference must belong to a publication that has a quote-validated claim. The older `possible_mechanisms` string field remains as a compatibility view of accepted mechanism descriptions. These mechanisms are hypotheses, not causal findings. The package ID includes event revision, tenant, policy ID/version, allowed source IDs, exact allowed document revisions when supplied, and model/prompt identity. It is cached in SQLite and reused across company analyses using the same tenant and policy snapshot. Source references include persisted classification, canonical URI, exact source content hash, publication metadata, origin group, and optional Corpus revision reference; `original()` retrieves the exact text while policy permits it.

An external extractor call is allowed only when every included publication revision is persisted as `public` or `synthetic`; this check runs before both claim-cache and package-cache lookup. It receives at most 10,000 UTF-8 bytes of publication text total, uses a 24,000-byte-conservative input-token cap, and makes one call. It receives no company profile or private facts. LLM claims require exact quotes in the identified permitted publication. Entities, territories, and resources are retained only when they are literal strings present in allowed source text. Mechanisms with malformed fields or publication references outside the validated claims are dropped with explicit coverage limitations. Programmatic guards downgrade a claim labeled `fact` when its quote explicitly contains opinion or forecast wording.

When `LLMService` is absent or returns malformed/unverifiable output, the deterministic path treats exact source excerpts as `reported_fact` claims with a note that source attribution does not establish truth. Failed semantic extraction stores a safe error class, a fallback mode, and limitations; it does not claim successful mechanism extraction. The model cannot make a quote into independent corroboration; distinct `origin_group_id` values are counted separately. Exact package and semantic claim-extraction caches are tenant-, policy-, allow-list-, model-, and prompt-scoped. The semantic cache stores both validated claims and bounded semantic fields, keyed on source content hashes and origin groups, so another receipt for the same content from an already represented origin does not trigger another model parse; the receipt remains in the package source list. A new origin changes the semantic signature. Prompt/cache identity is versioned as `event-claims-v2` and `event-impact-v2`.

## Business impact paths

`intelligence.research.DependencyEdge` represents one dependency supplied by a caller. Each edge requires status, evidence reference, evidence basis, and optional validity dates. `impact_candidates(package, company_id, edges, as_of=..., max_hops=2)` returns paths of at least two and at most three supplied edges that terminate at an explicit package entity. It does not create edges or infer purchase/supply relationships from payment observations. A returned path says only that a possible dependency path exists; it does not assert that the event caused a business outcome.

The caller remains responsible for loading company dependencies under the correct tenant and including only evidence refs it is authorized to use. The input edge list must represent business relationships already established by the owning Core/data layer.

Before deciding which authorized companies receive a full personalized model analysis,
`select_affected_businesses(package, businesses, as_of=..., max_hops=3)` can rank a
provided company snapshot using its actual `dependency_graph`, `CompanyContext`
dependencies and facts. It matches only explicit package entities, resources and
territories, with conservative token/suffix normalization. Direct one-edge matches
appear in this selection API; the existing `impact_candidates(...)` compatibility
API continues to return only two- or three-edge paths. A payment observation is not
accepted as a dependency edge. Future or expired edges are excluded at `as_of`.

The result is one `ImpactSelection` per supplied business, sorted by heuristic score
and company ID. `status="no_signal_found"` and zero score mean only that the supplied
snapshot had no matching signal; they do not mark a company safe. Scores are deterministic **queue-priority hints**, not probabilities,
semantic model confidence, or evidence that a business will be affected. Explicit
active confirmed dependency paths rank highest; assumed paths, facts and text matches
rank lower; industry-only overlap is a weak signal. A zero score means no match was
found in the supplied snapshot, not that the company is safe. Each row includes
matched nodes, evidence-backed paths, context/source coverage, package limitations
and uncertainty reasons. A caller may use this ordering to prioritize scheduled work
and avoid low-signal scheduled model calls, but must retain the no-match state as
`not evaluated` and allow explicit manual analysis. It must not claim a negative
impact finding from a skipped analysis.

This selection step uses only the company snapshots already supplied by the caller;
it never searches for another company's private context. The heuristic is not a
trained or quality-validated ranking model and does not replace the personalized
analysis and hard evidence checks.

## Period calculations and reassessment

`analyze_period(observations, period_start=..., period_end=..., known_at=..., mode="retrospective"|"as_known")` accepts only supplied numeric observations with a metric, unit, observed time, availability time, revision, and source reference. It selects the latest version per observation ID, calculates first-to-last changes in code, and excludes observations or revisions from the future. `as_known` uses only revisions available by `known_at`; `retrospective` uses the latest supplied revisions available at the current run time. Coverage reports selected revisions, excluded later revisions, source refs, and multi-point metrics. Results are descriptive and explicitly make no causal attribution.

`reassess(previous, current)` signs only event/evidence refs, company context, source policy, analyst config, model, and prompt versions. It returns a field-by-field change report and `publication_revision_required=False` when a repeated check has no decision-relevant changes. Callers should persist that signature with their own candidate revision; this helper does not publish a business situation or write to Core.

## Integration boundary

The current implementation does not fetch RSS/API/URLs, manage source policy, schedule background jobs, query `CorpusStore` automatically, maintain a company dependency graph, or publish/revise a product situation. The collection adapter or Core job worker must pass authorized `CorpusStore` document revisions and source-policy snapshots into these APIs. Full HTTP integration, backfill scheduling, graph storage, and publication fencing remain separate work. SQLite persistence here is a local module store, not the proposed shared bank PostgreSQL deployment.
