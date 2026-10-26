"""Execute a Core-authorized DataTask. --fixture-feed is only for offline tests."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from intelligence.collection import FetchResult, NewsCollector
from intelligence.workflow import DataTask, DataWorkflow


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--task', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--state-dir', required=True)
    p.add_argument('--fixture-feed')
    args = p.parse_args()
    task = DataTask.model_validate_json(Path(args.task).read_text(encoding='utf-8'))
    workflow = DataWorkflow(Path(args.state_dir))
    if args.fixture_feed:
        feed = Path(args.fixture_feed).read_bytes()
        allowed = {s.uri for s in task.source_policy.effective_sources()}
        if allowed != {'https://feeds.example/demo.rss'}:
            raise ValueError('fixture_feed_requires_exact_demo_source')
        def fetch(url: str, headers: dict, **limits):
            if url != 'https://feeds.example/demo.rss':
                raise ValueError('fixture_disallows_other_url')
            return FetchResult(status=200, headers={'content-type':'application/rss+xml'}, body=feed)
        workflow.collector = NewsCollector(workflow.store, state_path=Path(args.state_dir)/'collection.sqlite3', fetcher=fetch)
    result = workflow.run(task)
    Path(args.output).write_text(result.model_dump_json(indent=2), encoding='utf-8')
    print(json.dumps({'status':result.status,'documents':len((result.collection or {}).get('documents',[])),'events':len(result.events),'candidates':len(result.candidates)}))

if __name__ == '__main__': main()
