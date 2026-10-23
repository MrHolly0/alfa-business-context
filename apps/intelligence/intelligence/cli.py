"""Operator commands. Network model calls always require explicit --mode model."""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import sys
import tempfile
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .corpus import CorpusStore
from .pipeline import AnalysisPipeline
from .schemas import AnalysisRequest

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = APP_ROOT / '.state'


def read_json(path: str | Path):
    source = Path(path)
    if source.stat().st_size > 30_000_000:
        raise ValueError('input_file_too_large')
    return json.loads(source.read_text(encoding='utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non_finite_json')))


def write_json(value, output=None):
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    def encode(obj):
        if isinstance(obj, Decimal): return str(obj)
        if isinstance(obj, datetime): return obj.isoformat()
        raise TypeError(type(obj).__name__)
    text = json.dumps(value, ensure_ascii=False, indent=2, default=encode, allow_nan=False) + '\n'
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)


def make_pipeline(state_dir: str | Path, *, mode='baseline', config=None, profile=None, compare_hackathon=False, analyst=None):
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    store = CorpusStore(state / 'corpus.sqlite3')
    llm = None
    if mode == 'model':
        from .llm.config import load_config
        from .llm.service import LLMService
        configured = load_config(config or APP_ROOT/'examples/providers.template.json', profile=profile)
        # Paid testing requires explicit consent and bounded prices in provider configuration.
        configured.ensure_spend_allowed()
        comparison = {}
        if compare_hackathon:
            from .llm.pricing import Pricing
            target = load_config(config or APP_ROOT/'examples/providers.template.json', profile='hackathon')
            if configured.model.removesuffix(':free') != target.model:
                raise ValueError('cost_comparison_requires_same_model')
            comparison = {'projected_hackathon_pricing': Pricing(target.input_per_million,target.output_per_million,'RUB'),
                          'projected_hackathon_model':target.model}
        llm = LLMService(configured, ledger_path=state/'llm-budget.sqlite3', **comparison)
    from .analysts import PromptRegistry, builtin_configs
    registry = PromptRegistry(state/'analysts.sqlite3')
    for item in builtin_configs(): registry.register(item)
    return store, AnalysisPipeline(store, llm, analyst_config=registry.get(analyst) if analyst else None, registry=registry)


def _options(parser):
    parser.add_argument('--state-dir', default=str(DEFAULT_STATE), help='Private SQLite storage directory')
    parser.add_argument('--mode', choices=['baseline', 'model'], default='baseline')
    parser.add_argument('--config', help='Provider configuration, without credentials')
    parser.add_argument('--profile', choices=['openrouter', 'hackathon'])
    parser.add_argument('--analyst', help='Explicit analyst configuration ref; otherwise select active role/industry configuration')
    parser.add_argument('--compare-hackathon', action='store_true', help='Project RUB cost with explicit hackathon tariffs; same model only')


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == 'arena':
        from .arena_cli import main as arena_main
        return arena_main(arguments[1:])
    if arguments and arguments[0] == 'llm':
        from .llm.cli import main as llm_main
        return llm_main(arguments[1:])
    parser = argparse.ArgumentParser(description='Загрузка материалов, поиск и анализ бизнес-контекста')
    commands = parser.add_subparsers(dest='command', required=True)
    demo = commands.add_parser('demo', help='Учебный сквозной пример, без сети и ключей')
    demo.add_argument('--output')
    ingest = commands.add_parser('ingest', help='Импорт одного документа или JSON-массива документов')
    ingest.add_argument('--input', required=True)
    ingest.add_argument('--file', help='Local content file for a single metadata document; PDF encoded automatically')
    ingest.add_argument('--state-dir', default=str(DEFAULT_STATE))
    search = commands.add_parser('search', help='Поиск по разрешённым источникам')
    search.add_argument('--state-dir', default=str(DEFAULT_STATE))
    search.add_argument('--tenant', required=True)
    search.add_argument('--sources', required=True, help='Comma-separated permitted source IDs')
    search.add_argument('--as-of', required=True)
    search.add_argument('--query', required=True)
    search.add_argument('--role', choices=['external','bank'])
    search.add_argument('--limit', type=int, default=8)
    rank = commands.add_parser('rank', help='Объяснимый предварительный отбор событий')
    rank.add_argument('--input', required=True)
    rank.add_argument('--output')
    inspect = commands.add_parser('inspect', help='Состояние локальных выполнений и незавершённых запросов')
    inspect.add_argument('--state-dir', default=str(DEFAULT_STATE))
    reset = commands.add_parser('reset-request', help='Снять неопределённую reservation после ручной проверки')
    reset.add_argument('--state-dir', default=str(DEFAULT_STATE))
    reset.add_argument('--tenant',required=True)
    reset.add_argument('--request-id',required=True)
    reset.add_argument('--confirm-stopped',action='store_true',required=True)
    reset_job = commands.add_parser('reset-job', help='Разрешить повтор прерванного поколения после ручной проверки')
    reset_job.add_argument('--state-dir', default=str(DEFAULT_STATE))
    reset_job.add_argument('--job-id',required=True)
    reset_job.add_argument('--generation',type=int,required=True)
    reset_job.add_argument('--confirm-stopped',action='store_true',required=True)
    analyze = commands.add_parser('analyze', help='Candidate assessment; explicit --mode model calls provider')
    _options(analyze)
    analyze.add_argument('--input', required=True)
    analyze.add_argument('--output')
    evaluate = commands.add_parser('evaluate', help='Проверка сценариев; не заменяет проверку качества на реальных данных')
    _options(evaluate)
    evaluate.add_argument('--dataset', default=str(APP_ROOT/'evaluation/cases.json'))
    evaluate.add_argument('--output')
    serve = commands.add_parser('serve', help='Внутренний HTTP API; bearer token and tenant allowlist required')
    _options(serve)
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', default=8081, type=int)
    worker = commands.add_parser('worker', help='Получать задания PHP Core через внутренний API')
    _options(worker)
    worker.add_argument('--once', action='store_true')
    worker.add_argument('--poll-seconds', type=float, default=5)
    worker.add_argument('--heartbeat-seconds', type=float, default=10)
    args = parser.parse_args(arguments)
    try:
        if args.command == 'demo':
            with tempfile.TemporaryDirectory(prefix='business-context-demo-') as tmp:
                store, pipeline = make_pipeline(tmp)
                for document in read_json(APP_ROOT/'examples/documents.json'): store.ingest(document)
                request = AnalysisRequest.model_validate(read_json(APP_ROOT/'examples/analysis-request.json'))
                write_json(pipeline.run(request), args.output)
        elif args.command == 'ingest':
            data = read_json(args.input)
            documents = data if isinstance(data, list) else [data]
            if not documents or len(documents)>500: raise ValueError('document_batch_limit')
            if args.file:
                if len(documents) != 1: raise ValueError('file_requires_single_document')
                path = Path(args.file)
                if path.stat().st_size > 20_000_000: raise ValueError('content_file_too_large')
                documents[0]['content'] = (base64.b64encode(path.read_bytes()).decode() if documents[0].get('content_type')=='application/pdf'
                                           else path.read_text(encoding='utf-8'))
            store = CorpusStore(Path(args.state_dir)/'corpus.sqlite3')
            write_json([store.ingest(document) for document in documents])
        elif args.command in {'inspect','reset-request','reset-job'}:
            from .artifacts import ArtifactStore
            artifacts=ArtifactStore(Path(args.state_dir)/'artifacts.sqlite3')
            if args.command=='inspect':
                write_json({'requests':artifacts.list_reservations(),'jobs':artifacts.list_job_runs(),
                            'pending_deliveries':artifacts.pending(),'dead_deliveries':artifacts.dead_letters()})
            elif args.command=='reset-request':
                artifacts.reset_candidate(args.tenant,args.request_id,force=args.confirm_stopped)
                write_json({'status':'reset'})
            else:
                artifacts.reset_job_run(args.job_id,args.generation,force=args.confirm_stopped)
                write_json({'status':'reset'})
        elif args.command == 'rank':
            from .relevance import RankRequest,rank_events
            request=RankRequest.model_validate(read_json(args.input))
            write_json(rank_events(**request.model_dump(mode='json')),args.output)
        elif args.command == 'search':
            store = CorpusStore(Path(args.state_dir)/'corpus.sqlite3')
            write_json(store.search(tenant_id=args.tenant,source_ids=args.sources.split(','),as_of=args.as_of,
                                   query=args.query,role=args.role,limit=args.limit))
        elif args.command in {'analyze','evaluate','serve','worker'}:
            store, pipeline = make_pipeline(args.state_dir,mode=args.mode,config=args.config,profile=args.profile,compare_hackathon=args.compare_hackathon,analyst=args.analyst)
            if args.command == 'analyze':
                request = AnalysisRequest.model_validate(read_json(args.input))
                write_json(pipeline.run(request), args.output)
            elif args.command == 'evaluate':
                from .evaluation import evaluate_dataset
                report = evaluate_dataset(read_json(args.dataset), pipeline_factory=lambda corpus: AnalysisPipeline(corpus,pipeline.llm,analyst_config=pipeline.analyst_config,registry=pipeline.registry))
                write_json(report,args.output)
                return 0 if report['passed'] else 1
            elif args.command == 'serve':
                import uvicorn
                from .api import create_app
                from .artifacts import ArtifactStore
                app = create_app(store=store,pipeline=pipeline,artifact_store=ArtifactStore(Path(args.state_dir)/'artifacts.sqlite3'))
                uvicorn.run(app,host=args.host,port=args.port,access_log=False,log_level='warning')
            else:
                from .artifacts import ArtifactStore
                from .core_client import CoreClient
                from .worker import Worker
                if not 0.1 <= args.poll_seconds <= 3600: raise ValueError('invalid_poll_interval')
                if not 0.05 <= args.heartbeat_seconds <= 60: raise ValueError('invalid_heartbeat_interval')
                core = CoreClient()
                executor = Worker(core,pipeline,ArtifactStore(Path(args.state_dir)/'artifacts.sqlite3'),heartbeat_interval=args.heartbeat_seconds)
                stop = threading.Event()
                def halt(*_): stop.set()
                signal.signal(signal.SIGINT,halt)
                signal.signal(signal.SIGTERM,halt)
                try:
                    while not stop.is_set():
                        try:
                            result=executor.run_once()
                            if result: write_json(result)
                        except Exception:
                            # No provider or HTTP exception text in operator logs.
                            write_json({'status':'core_unavailable'})
                        if args.once: break
                        stop.wait(args.poll_seconds)
                finally: core.close()
        return 0
    except (ValueError, OSError, KeyError) as exc:
        # Validation errors may contain the full untrusted input; print only the class.
        sys.stderr.write(json.dumps({'error':type(exc).__name__,'message':'Проверьте входной JSON, конфигурацию и доступность файлов.'},ensure_ascii=False)+'\n')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
