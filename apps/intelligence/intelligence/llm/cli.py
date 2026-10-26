"""Offline-first command line for provider configuration, calls, and budget review."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
import json
import sys
from pathlib import Path

from .config import load_config
from .ledger import BudgetLedger, BudgetError
from .pricing import Pricing
from .service import DEFAULT_LEDGER_PATH, LLMService
from .transport import ChatCompletionTransport
from .types import Completion, Usage


class _FixtureTransport:
    """Deterministic, offline example. Its usage and costs are synthetic."""
    def complete(self, messages, *, json_mode=False, max_output_tokens=None):
        prompt = messages[-1]["content"]
        return Completion(
            content=f"[offline fixture] Received {len(prompt.encode('utf-8'))} UTF-8 input bytes. No provider was called.",
            model="offline-fixture",
            usage=Usage(prompt_tokens=12, completion_tokens=18, reported_cost=Decimal("0.000001"), cost_currency="USD"),
            finish_reason="stop",
            response_id="fixture-only",
        )


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _dump(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default))


def _input_text(path: str | None) -> str:
    if path:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            raise ValueError("could not read input file") from None
    return sys.stdin.read()


def _parser():
    parser = argparse.ArgumentParser(prog="python -m intelligence.llm",
        description="Offline-first model adapter. Network access occurs only with the explicit 'run' command.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("demo", help="run the offline synthetic fixture (default)")
    validate = commands.add_parser("validate", help="validate a provider profile without reading credentials")
    validate.add_argument("--config", required=True)
    validate.add_argument("--profile", choices=("openrouter", "hackathon"))
    budget = commands.add_parser("budget", help="show this local ledger's current budget status")
    budget.add_argument("--config", required=True)
    budget.add_argument("--profile", choices=("openrouter", "hackathon"))
    budget.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
    configure = commands.add_parser("configure-budget", help="explicitly change an existing ledger account's limits")
    configure.add_argument("--scope", required=True)
    configure.add_argument("--currency", required=True, choices=("USD", "RUB"))
    configure.add_argument("--daily-limit", required=True)
    configure.add_argument("--lifetime-limit", help="omit to remove the lifetime cap")
    configure.add_argument("--expect-daily-limit", required=True)
    configure.add_argument("--expect-lifetime-limit", help="use --expect-no-lifetime-limit when currently unset")
    configure.add_argument("--expect-no-lifetime-limit", action="store_true")
    configure.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
    configure.add_argument("--confirm", action="store_true", help="required to apply the limit change")
    run = commands.add_parser("run", help="make one explicit live model request")
    run.add_argument("--config", required=True)
    run.add_argument("--profile", choices=("openrouter", "hackathon"))
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
    run.add_argument("--input", help="UTF-8 prompt file; omitted means read stdin")
    run.add_argument("--max-input-tokens", type=int, default=8192,
                     help="local conservative text estimate cap (UTF-8 bytes + message overhead)")
    run.add_argument("--max-output-tokens", type=int)
    run.add_argument("--json", action="store_true", dest="json_mode")
    run.add_argument("--compare-hackathon", action="store_true",
                     help="estimate the same reported token usage at configured hackathon RUB tariffs")
    return parser


def _fixture():
    transport = _FixtureTransport()
    completion = transport.complete([{"role": "user", "content": "fixture example"}])
    return {
        "mode": "offline_fixture",
        "network": False,
        "credentials_read": False,
        "cost_is_synthetic": True,
        "content": completion.content,
        "model": completion.model,
        "usage": asdict(completion.usage),
        "provider_reported_cost": completion.usage.reported_cost,
        "provider_cost_currency": completion.usage.cost_currency,
        "projected_hackathon_cost_rub": None,
    }


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command in (None, "demo"):
        _dump(_fixture())
        return 0
    try:
        if args.command == "configure-budget":
            if not args.confirm:
                raise ValueError("configure-budget requires --confirm")
            if args.expect_no_lifetime_limit == (args.expect_lifetime_limit is not None):
                raise ValueError("provide exactly one of --expect-lifetime-limit or --expect-no-lifetime-limit")
            ledger = BudgetLedger(args.ledger)
            expected_life = None if args.expect_no_lifetime_limit else args.expect_lifetime_limit
            ledger.configure_limits(args.scope, args.currency, daily_limit=args.daily_limit,
                lifetime_limit=args.lifetime_limit, expected_daily_limit=args.expect_daily_limit,
                expected_lifetime_limit=expected_life)
            _dump(ledger.summary(args.scope, args.currency))
            return 0
        config = load_config(args.config, args.profile)
        if args.command == "validate":
            _dump({"valid": True, "profile": config.profile, "model": config.model,
                   "base_url": config.base_url, "budget_scope": config.budget_scope,
                   "currency": config.currency, "daily_limit": config.daily_limit,
                   "lifetime_limit": config.lifetime_limit, "pricing_mode": config.pricing_mode,
                   "attempt_reserve_rub": config.attempt_reserve_rub,
                   "max_calls_total": config.max_calls_total,
                   "api_key_env": config.api_key_env})
            return 0
        if args.command == "budget":
            ledger = BudgetLedger(args.ledger)
            _dump(ledger.summary(config.budget_scope, config.currency))
            return 0
        projected = None
        if args.compare_hackathon:
            hack = load_config(args.config, "hackathon")
            if hack.pricing_mode == "fixed_reserve":
                raise ValueError("cannot project hackathon RUB cost while the gateway tariff is unknown")
            if hack.model.removesuffix(":free") != config.model.removesuffix(":free"):
                raise ValueError("comparison requires the same model in both profiles")
            projected = Pricing(hack.input_per_million, hack.output_per_million, "RUB")
        prompt = _input_text(args.input)
        ledger = BudgetLedger(args.ledger)
        service = LLMService(config, ledger=ledger, transport=ChatCompletionTransport(config),
                             projected_hackathon_pricing=projected,
                             projected_hackathon_model=hack.model if args.compare_hackathon else None)
        report = service.complete([{"role": "user", "content": prompt}],
                                  max_input_tokens=args.max_input_tokens,
                                  max_output_tokens=args.max_output_tokens,
                                  json_mode=args.json_mode)
        _dump(report)
        return 0
    except (ValueError, BudgetError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Exceptions from network/decoding are sanitized by the adapter; avoid dumping
        # payloads, environment values, prompts, or transport internals on stderr.
        from .types import ProviderError
        if isinstance(exc, ProviderError):
            print(f"provider error [{exc.code}]: {exc}", file=sys.stderr)
            return 3
        print("error: request failed; inspect local configuration and retry explicitly", file=sys.stderr)
        return 3
