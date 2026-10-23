"""Explicit operator commands for prompt experiments and reviewed activation."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from .cli import APP_ROOT, DEFAULT_STATE, read_json, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description='Настройки аналитика и отдельные турниры промптов')
    parser.add_argument('--state-dir', default=str(DEFAULT_STATE))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='Зарегистрированные версии настроек')
    export = commands.add_parser('export', help='Показать настройки версии для редактирования новой ревизии')
    export.add_argument('--ref', required=True)
    export.add_argument('--output')
    active = commands.add_parser('active', help='Какая конфигурация будет выбрана для роли и отрасли')
    active.add_argument('--role', required=True)
    active.add_argument('--industry', required=True)
    register = commands.add_parser('register', help='Добавить неизменяемую версию из JSON')
    register.add_argument('--input', required=True)
    for name in ('generate', 'run'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--mode', choices=['baseline', 'model'], default='baseline')
        cmd.add_argument('--config', default=str(APP_ROOT/'examples/providers.template.json'))
        cmd.add_argument('--profile', choices=['openrouter', 'hackathon'])
        cmd.add_argument('--experiment-limit', help='Дневной лимит экспериментов в валюте профиля, <=20%% общего')
        cmd.add_argument('--max-calls', type=int, default=40)
        cmd.add_argument('--output')
        if name == 'generate':
            cmd.add_argument('--base', default='baseline@1')
            cmd.add_argument('--count', type=int, default=3)
        else:
            cmd.add_argument('--run-id', required=True)
            cmd.add_argument('--dataset', default=str(APP_ROOT/'evaluation/tournament-demo.json'))
            cmd.add_argument('--candidates', help='Через запятую; по умолчанию четыре встроенные версии')
            cmd.add_argument('--judge-config', help='Отдельный профиль судьи; тот же общий счёт расходов')
            cmd.add_argument('--judge-profile', choices=['openrouter', 'hackathon'])
            cmd.add_argument('--shadow', action='store_true')
            cmd.add_argument('--critic', action='store_true', help='Один дополнительный разбор оснований перед holdout; без автопереписывания')
    budget = commands.add_parser('budget', help='Расходы общего счёта и экспериментального пула, без сетевых вызовов')
    budget.add_argument('--config', default=str(APP_ROOT/'examples/providers.template.json'))
    budget.add_argument('--profile', choices=['openrouter', 'hackathon'])
    show = commands.add_parser('show')
    show.add_argument('--run-id', required=True)
    show.add_argument('--output')
    pack = commands.add_parser('review-pack', help='Слепой пакет для человека без имён конфигураций')
    pack.add_argument('--run-id', required=True)
    pack.add_argument('--output')
    review = commands.add_parser('review', help='Сохранить ручную слепую оценку отчёта')
    review.add_argument('--run-id', required=True)
    review.add_argument('--reviewer', required=True)
    review.add_argument('--ratings', required=True, help='JSON с оценками по формату отчёта')
    review.add_argument('--output')
    for name in ('promote', 'rollback'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--scope', required=True, help='impact:finance:* и другие области применения')
        cmd.add_argument('--reviewer', required=True)
        cmd.add_argument('--expected-revision', type=int, required=True)
        if name == 'promote':
            cmd.add_argument('--run-id', required=True)
    args = parser.parse_args(argv)
    try:
        from .analysts import AnalystConfig, PromptRegistry, builtin_configs
        from .tournament import TournamentRunner, TournamentStore
        state = Path(args.state_dir)
        state.mkdir(parents=True, exist_ok=True)
        registry = PromptRegistry(state/'analysts.sqlite3')
        for config in builtin_configs():
            registry.register(config)
        arena = TournamentStore(state/'tournaments.sqlite3')
        if args.command == 'list':
            write_json(registry.list())
        elif args.command == 'export':
            write_json(registry.get(args.ref), args.output)
        elif args.command == 'active':
            write_json(registry.select(role=args.role,industry=args.industry))
        elif args.command == 'review-pack':
            write_json(arena.review_pack(args.run_id), args.output)
        elif args.command == 'register':
            write_json(registry.register(AnalystConfig.model_validate(read_json(args.input))))
        elif args.command == 'budget':
            from .llm.config import load_config
            from .llm.ledger import BudgetLedger
            config = load_config(args.config, profile=args.profile)
            ledger = BudgetLedger(state/'llm-budget.sqlite3')
            write_json({'global':ledger.summary(config.budget_scope,config.currency),
                'experiments':ledger.summary('prompt-experiments-'+config.currency,config.currency)})
        elif args.command == 'show':
            write_json(arena.get(args.run_id), args.output)
        elif args.command == 'review':
            write_json(arena.review(args.run_id, reviewer=args.reviewer, ratings=read_json(args.ratings)), args.output)
        elif args.command == 'promote':
            report = arena.get(args.run_id)
            write_json(registry.promote(scope=args.scope, ref=report['winner_ref'], report=report,
                reviewer=args.reviewer, expected_revision=args.expected_revision))
        elif args.command == 'rollback':
            write_json(registry.rollback(scope=args.scope, reviewer=args.reviewer, expected_revision=args.expected_revision))
        else:
            from .contenders import CandidateGenerator, PairJudge
            services = []
            identities = {}
            if args.mode == 'model':
                from .experiments import ExperimentLLM
                from .llm.config import load_config
                from .llm.service import LLMService
                from .llm.ledger import BudgetLedger
                config = load_config(args.config, profile=args.profile)
                limit = Decimal(args.experiment_limit) if args.experiment_limit else min(config.daily_limit*Decimal('.2'), Decimal('.03') if config.currency == 'USD' else Decimal('100'))
                configs = [config]
                if args.command == 'run' and args.judge_config:
                    configs.append(load_config(args.judge_config, profile=args.judge_profile))
                if any((c.currency,c.budget_scope,c.daily_limit) != (config.currency,config.budget_scope,config.daily_limit) for c in configs):
                    raise ValueError('roles_must_share_global_account_and_currency')
                ledger = BudgetLedger(state/'llm-budget.sqlite3')
                for index, c in enumerate(configs):
                    service = LLMService(c, ledger=ledger)
                    services.append(ExperimentLLM(service, ledger=ledger, scope='prompt-experiments-'+config.currency, daily_limit=limit, max_calls=args.max_calls))
                    identities[str(index)] = asdict(c)
            if args.command == 'generate':
                configs = CandidateGenerator(services[0] if services else None).generate(base=registry.get(args.base), count=args.count)
                write_json({'registered':[registry.register(c) for c in configs], 'requested_count':args.count, 'accepted_count':len(configs), 'mode':args.mode,
                    'costs':[s.stats() for s in services]},args.output)
            else:
                from .pipeline import AnalysisPipeline
                refs = args.candidates.split(',') if args.candidates else [c.ref for c in builtin_configs()]
                report = TournamentRunner(arena, registry,
                    pipeline_factory=lambda corpus, config: AnalysisPipeline(corpus, services[0] if services else None, analyst_config=config),
                    judge=PairJudge(services[-1] if services else None),
                    critic=services[-1] if args.critic and services else None).run(
                        run_id=args.run_id, dataset=read_json(args.dataset), candidate_refs=refs,
                        mode=args.mode, shadow=args.shadow, execution_identity=identities)
                write_json(report,args.output)
        return 0
    except Exception as exc:
        # Provider data, request bodies and credentials must never enter CLI diagnostics.
        import re
        import sys
        from .llm.types import ProviderError
        code = exc.code if isinstance(exc,ProviderError) else str(exc) if type(exc) is ValueError else ''
        suffix = ' ('+code+')' if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,100}',code) else ''
        status = ' HTTP '+str(exc.http_status) if isinstance(exc,ProviderError) and type(exc.http_status) is int else ''
        sys.stderr.write('Ошибка турнира: '+type(exc).__name__+suffix+status+'\n')
        return 2
