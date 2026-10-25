"""Offline, synthetic end-to-end demo for the AI/Data workflow.

The feed transport is deliberately replaced by a local fixture reader. No URL in
the demo task is contacted, and a fixture URL outside the two documented hosts
causes the demo fetcher to fail closed.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .collection import FetchResult, NewsCollector
from .corpus import CorpusStore
from .workflow import DataResult, DataTask, DataWorkflow


APP_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_FEED_URL = "https://feeds.example/demo.rss"
FIXTURE_BANK_URL = "https://bank.example/demo-feed.rss"
FIXTURE_AS_OF = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)


class _LocalFixtureFetcher:
    """Return only local fixture bytes; never falls back to HTTP or DNS."""

    def __init__(self, feed_bytes: bytes):
        self.feed_bytes = feed_bytes
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], **limits: Any) -> FetchResult:
        self.calls.append(url)
        if url == FIXTURE_FEED_URL:
            return FetchResult(status=200,
                headers={"content-type": "application/rss+xml; charset=utf-8", "etag": '"demo-feed-v1"'},
                body=self.feed_bytes)
        if url == FIXTURE_BANK_URL:
            # The bank-role sample document is manually ingested from a separate
            # local text fixture below; this feed grant deliberately has no bytes.
            return FetchResult(status=304, headers={"etag": '"demo-bank-empty-v1"'})
        raise RuntimeError("offline_demo_disallows_unlisted_url")


def _run(state_dir: Path) -> DataResult:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads((APP_ROOT / "examples" / "data-task.json").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    payload["request_id"] = f"offline-demo-{uuid.uuid4().hex}"
    payload["as_of"] = now.isoformat()
    payload["source_policy"]["issued_at"] = now.isoformat()
    task = DataTask.model_validate(payload)

    workflow = DataWorkflow(state_dir)
    # The note is a local synthetic corpus document, visibly disclaimed in its
    # body and publisher metadata. It is not attributed to Alfa Bank.
    bank_text = (APP_ROOT / "examples" / "feeds" / "demo-bank-note.txt").read_text(encoding="utf-8")
    workflow.store.ingest({
        "tenant_id": task.tenant_id,
        "source_id": "demo-bank-note",
        "source_role": "bank",
        "data_classification": "synthetic",
        "canonical_uri": "https://bank.example/manual/demo-bank-note",
        "title": "СИНТЕТИЧЕСКАЯ УЧЕБНАЯ ЗАПИСКА; НЕ ПОЗИЦИЯ АЛЬФА-БАНКА",
        "content": bank_text,
        "content_type": "text/plain",
        "published_at": FIXTURE_AS_OF.isoformat(),
        "fetched_at": now.isoformat(),
        "synthetic": True,
        "publisher": "Учебная фикстура; не Альфа-Банк",
        "author": "Локальный демо-материал",
        "language": "ru",
    })
    feed = (APP_ROOT / "examples" / "feeds" / "demo.rss").read_bytes()
    fetcher = _LocalFixtureFetcher(feed)
    workflow.collector = NewsCollector(workflow.store,
        state_path=state_dir / "collection.sqlite3", fetcher=fetcher,
        raw_dir=state_dir / "raw" / "collection")

    result = workflow.run(task)
    report = {
        "mode": "offline_synthetic_fixture",
        "network_access": False,
        "source_ids": task.source_policy.effective_source_ids(),
        "fetch_calls": len(fetcher.calls),
        "events": len(result.events),
        "companies": len(task.businesses),
        "candidates": len(result.candidates),
        "configuration_refs": [item.get("config_ref") for item in result.configurations],
        "baseline_only": True,
        "fixture_dates_fixed": True,
    }
    result.coverage["demo_summary"] = report
    result.trace.append({"stage": "offline_demo_summary", **report})
    return result


def run_demo(state_dir: str | Path | None = None) -> DataResult:
    """Run the documented local-feed → corpus → event → multi-company flow.

    If `state_dir` is omitted, temporary raw storage is removed before
    returning. For durable original bytes, pass an explicit private state path.
    """
    if state_dir is not None:
        return _run(Path(state_dir))
    with tempfile.TemporaryDirectory(prefix="alfa-offline-data-demo-") as temporary:
        return _run(Path(temporary))


def run_business_demo(state_dir, *, llm=None, role_services=None, analyst_config=None):
    """New mixed business example. Source fetches are local fixtures; models explicit."""
    payload=json.loads((APP_ROOT/'examples/business-scenario.json').read_text())
    payload['request_id']='business-demo-'+uuid.uuid4().hex
    payload['operation']='collect'
    payload['budget']['max_model_calls']=3 if llm or role_services else 0
    task=DataTask.model_validate(payload)
    instance=DataWorkflow(state_dir,llm=llm,role_services=role_services,
        analyst_config=analyst_config,clock=lambda:task.as_of)
    sources={source.source_id:source for source in task.source_policy.sources}
    fixtures={sources[key].uri:(APP_ROOT/'examples/feeds'/name).read_bytes()
        for key,name in [('wire-a','business-a.rss'),('wire-b','business-b.rss')]}
    calls=[]
    def fetch(url,headers,**limits):
        if url not in fixtures: raise ValueError('offline_fixture_url_not_permitted')
        calls.append(url)
        return FetchResult(status=200,headers={'content-type':'application/rss+xml'},body=fixtures[url])
    instance.collector=NewsCollector(instance.store,state_path=Path(state_dir)/'collection.sqlite3',
        fetcher=fetch,raw_dir=Path(state_dir)/'raw'/'collection')
    result=instance.run(task)
    result.coverage['demo_summary']={'source_transport':'local_synthetic_rss',
        'source_network_access':False,'model_requested':bool(llm or role_services),
        'fetch_calls':len(calls),'companies':len(task.businesses),
        'events':len(result.events),'candidates':len(result.candidates),
        'semantic_expectations':'evaluation/business-scenario.json'}
    return result
