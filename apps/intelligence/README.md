# AI/Data: поиск и анализ

Python получает из Core разрешённый snapshot: source policy, контекст, даты и версии. Он собирает только выданные источники, сохраняет оригиналы в private filesystem, а документы, ревизии, события и evidence — в PostgreSQL. Затем возвращает кандидатов. Symfony Core подключён через внутренний HTTP API; контрактный fixture stand остаётся отдельным средством тестирования. Режим данных организаторов, локальный OCR и запуск на моделях Альфы описаны в [инструкции для судей](../../docs/judging.md).

## Быстрый запуск без внешних источников и ключей

Сначала запустите PostgreSQL и примените миграции по [инструкции разработки](../../docs/development.md), затем установите окружение. Командам нужны app DSN `INTELLIGENCE_DATABASE_URL`; миграции и импорты выполняются отдельно через `INTELLIGENCE_MIGRATION_DATABASE_URL`. Затем из каталога `apps/intelligence`:

```sh
.venv/bin/python -m intelligence --help
.venv/bin/python -m intelligence db check
.venv/bin/python -m intelligence demo
.venv/bin/python -m intelligence demo-data --state-dir /tmp/alfa-data-demo
.venv/bin/python -m intelligence demo-business --state-dir /tmp/alfa-business-demo
.venv/bin/python -m intelligence demo-personalization --output /tmp/personalization-comparison.json
.venv/bin/python -m intelligence prepare-description --input examples/description-context.json \
  --output /tmp/description-proposal.json
.venv/bin/python -m intelligence evaluate --mode baseline
```

`demo-data` выполняет Collector → Corpus → события/общий пакет → анализ двух синтетических компаний на локальном RSS fixture. Он не обращается к сети или модели. `evaluate --mode baseline` — функциональная проверка сценариев, не оценка смысла или полезности на реальных данных.

Тесты запускайте по [инструкции разработки](../../docs/development.md): им нужна отдельная PostgreSQL база `business_context_ai_test` и роль миграции для importer tests.

Пример полного разрешённого task snapshot:

```sh
.venv/bin/python -m intelligence run-task --mode baseline \
  --input examples/data-task.json --state-dir /tmp/alfa-data-task \
  --output /tmp/alfa-data-result.json
```

`run-task` исполняет `ai-data-task-v1` локально и обходит durable Core job lifecycle. Целевой job-контракт предусматривает `ai-job-v1`, claim/heartbeat/result и `ai-result-v1`; в текущем PHP Core этот lifecycle ещё не реализован. Рабочий продукт использует прямой `/internal/v1/analyze` с `ai-data-task-v1` и `ai-data-result-v1`. Детали полей — [контракты](../../contracts/intelligence/README.md) и [интеграция](../../docs/ai-integration.md). Persistence-команды `python -m intelligence db migrate`, `db check` и `db import-sqlite` управляют миграцией, проверкой подключения и ограниченным импортом.

## Контрактный Core stand

Стенд требует явного fixture opt-in и слушает loopback. SQLite используется только локальным fixture Core; рабочее состояние Python Intelligence остаётся в PostgreSQL. Stand не содержит бизнес-интерфейс, production-права или настоящий PHP:

```sh
.venv/bin/python -m intelligence core-stand --fixture-mode \
  --state-dir /tmp/alfa-core-stand --seed examples/core-stand-ai-job.json
```

Периодическая фикстура (минимум 60 секунд):

```sh
.venv/bin/python -m intelligence core-stand --fixture-mode \
  --state-dir /tmp/alfa-core-stand \
  --schedule examples/core-stand-ai-task.json \
  --schedule-id demo-collect --interval 3600
```

Scheduler создаёт обычные fixture jobs. Реальную очередь и планировщик владеет Core.

## API и публичные операции

Внутренний FastAPI требует service bearer token и tenant allowlist. Основные маршруты: `/internal/v1/analyze` (legacy `AnalysisRequest` или `DataTask`), `/internal/v1/search`, `/internal/v1/rank`, `/internal/v1/company-context/prepare`, `/internal/v1/sources/probe`, `/internal/v1/originals` и `/internal/v1/executions/{request_id}`. Frontend обращается к PHP/Core, а не к Python API.

`prepare-context` и `/company-context/prepare` преобразуют обезличенные строки в предложение CompanyContext; профиль не сохраняется автоматически и модель не вызывается. Единица выручки должна быть подтверждена явно. CSV кейса загружается отдельной [командой подготовки](../../docs/judging.md); исходники не включаются в Git.

Для URL используется одноразовый grant Core (`probe-source`/`sources/probe`); probe — не подписка. `NewsCollector` принимает неизменяемый snapshot источников и ограничивает HTTPS, DNS/IP, redirect, время и размер ответа. `collect` может быть отдельной task operation. RSS/Atom, HTML, PDF и ограниченный JSON adapter поддерживаются кодом; оригинальные байты хранятся с hash в tenant-scoped private raw store.

## Анализ, роли и конфигурации

Исследователь, аналитик и критик — этапы одного общего pipeline, а не три обязательных независимых агента. Поиск идёт в разрешённом corpus. Общий event impact package переиспользуется между бизнесами; кандидаты содержат версии, evidence, coverage, unknowns и `publication_revision_required`. Только Core решает, публиковать ли новую business revision.

Обычный workflow использует baseline-конфигурацию или подходящую активную analyst config. `arena` — отдельный эксперимент с одинаковыми входами, hard gates, blind review, holdout/shadow и ручной публикацией/rollback. Малые синтетические прогонки подтверждают механику, но не выигрыш по качеству. Настроенные модели запускаются только явно; Конфиденциальные данные по умолчанию анализируются локально. Исключение для материалов организаторов требует точного operator approval: tenant, hash профиля, версии и хеши PDF, разрешённый шлюз Альфы. Для OpenRouter это исключение не действует.

## Где менять код

| Файлы | Назначение |
|---|---|
| `collection.py`, `parsers.py`, `corpus.py` | Ограниченный сбор, разбор, raw receipts, ревизии и поиск |
| `events.py`, `relevance.py`, `research.py` | События, claims, отбор воздействия, пути и периодный анализ |
| `workflow.py`, `watches.py`, `periods.py` | DataTask orchestration, повторная оценка и watch условия |
| `pipeline.py`, `multi_agent.py`, `prompts/` | Кандидат, структурные проверки и логические роли |
| `analysts.py`, `tournament.py`, `experiments.py` | Версии конфигураций и экспериментальный турнир |
| `api.py`, `core_client.py`, `worker.py`, `core_stand.py` | Внутренние API, Core job transport, worker и fixture стенд |
| `client_data.py` | Чистая подготовка анонимизированного CSV-профиля |
| `llm/` | Model registry, маршрутизация, лимитирование и ledger |

Читайте [процесс](../../docs/ai-process.md), [границы и готовность](../../docs/ai-scope-and-readiness.md), [PostgreSQL](../../docs/postgresql.md), [сбор источников](docs/collection.md), [проверки](docs/checks.md), [аналитические конфигурации](docs/analysts.md) и [турнир](docs/tournament.md). Актуальный отчёт о live-проверках: [ai-live-checks.md](../../docs/ai-live-checks.md).

Успешные schema/tests не доказывают точность выводов. В репозитории нет реальных 9 банковских PDF и CSV кейса, PHP интеграции, лицензированных production источников или независимой оценки на 200–300 примерах.

`demo-business` проверяет новый смешанный профиль, два сообщения о терминале и слабую аналогию на локальных RSS. `optimize`/`rollback_config` — отдельные операции DataTask; обычный анализ использует выбранную конфигурацию без нового турнира. Профиль `company-context-v2`, предпочтения, версии и порядок подключения описаны в [процессе](../../docs/ai-process.md) и [контракте](../../contracts/intelligence/README.md).

`prepare-description` — детерминированный CLI для proposal из `DescriptionPrepareRequest`; команда не обновляет канонический Core-профиль. Операция DataTask `prepare_description` может использовать разрешённый LLM-маршрут, но тоже возвращает только предложение; workflow сохраняет результат обычным способом в result cache. `demo-personalization` сравнивает три синтетических профиля на общих событиях без сети/моделей по умолчанию. Сигналы — эвристический приоритет, не вероятность или оценка убытка. Подробно: [персонализация](../../docs/personalization.md), [сравнительный отчёт](../../docs/checks/personalization-comparison-2026-10-25.json).

Косвенное исследование и опровержение: `docker compose --profile tools run --build --rm intelligence demo-research` из корня, либо `.venv/bin/python -m intelligence demo-research --output /tmp/indirect-research.json` из этого каталога. Встроенный набор синтетический, baseline не вызывает модель и не делает причинного вердикта. [Реальные проверки](../../docs/research-and-tournament-checks.md), [примеры Symfony](../../docs/symfony-integration.md).
