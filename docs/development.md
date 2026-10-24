# Разработка и локальная проверка

Основной запуск — [Docker Compose](../infra/README.md). Общие границы и статус проекта описаны в [интеграции Backend/Frontend](ai-integration.md), [процессе AI/Data](ai-process.md) и [матрице готовности](ai-scope-and-readiness.md). Live результаты не дублируются здесь: см. [ai-live-checks.md](ai-live-checks.md).

## Python

Из `apps/intelligence` с Python 3.12+ и установленным uv:

```sh
uv sync --frozen
uv run --frozen python -m intelligence --help
uv run --frozen python -m intelligence demo-data --state-dir /tmp/alfa-demo-data
uv run --frozen python -m intelligence run-task --mode baseline \
  --input examples/data-task.json --state-dir /tmp/alfa-data-task
uv run --frozen python -m pytest -q -p no:cacheprovider tests
```

`demo-data` запускает локальную синтетическую фикстуру без HTTP и LLM. `run-task` выполняет разрешённый `ai-data-task-v1` напрямую и не проверяет Core queue/lease/publication lifecycle. Baseline не делает внешние model calls. Результат оценивает структуру/сценарий; качество на неизвестных реальных банковских данных этим не подтверждается.

### Fixture job stand

Чтобы проверить CoreClient/Worker, запустите fixture stand в одном терминале, а worker — в другом из `apps/intelligence`:

```sh
uv run --frozen python -m intelligence core-stand --fixture-mode \
  --state-dir /tmp/alfa-core-stand --seed examples/core-stand-ai-job.json
```

Это явный SQLite-backed stand на loopback, не PHP Core и не production job owner. Он создаёт локальный тестовый service token для fixture; пользовательские права, UI, SSE, публикация и PHP бизнес-таблицы здесь не реализованы.

Чтобы worker подключился к stand, задайте ему адрес и локальный тестовый token, показанный stand, а затем выполните:

```sh
CORE_BASE_URL=http://127.0.0.1:8765 \
CORE_SERVICE_TOKEN='<fixture-token>' \
uv run --frozen python -m intelligence worker --once --mode baseline \
  --state-dir /tmp/alfa-worker
```

Подставляйте token только локально в командную сессию; это не production credential и не значение для репозитория. Для durable расписания используйте `--schedule examples/core-stand-ai-task.json --schedule-id demo-collect --interval 3600`; интервал должен быть не меньше 60 секунд. Stand создаёт обычную fixture job, без браузера и сетевых источников.

## Внутренний HTTP API

Для локального `serve` нужны operator-owned `INTELLIGENCE_SERVICE_TOKEN` и `INTELLIGENCE_TENANTS`; без обоих приватные маршруты закрыты:

```sh
INTELLIGENCE_SERVICE_TOKEN='<local-test-token>' \
INTELLIGENCE_TENANTS='demo-tenant' \
uv run --frozen python -m intelligence serve --mode baseline \
  --host 127.0.0.1 --port 8081 --state-dir /tmp/alfa-api
```

Не используйте эти test values как рабочую настройку. Frontend ходит в Core; Python API предназначен для доверенного service caller. Маршруты и входы перечислены в [контрактах](../contracts/intelligence/README.md).

## Web

Нужен Node.js версии, указанной в `apps/web/.nvmrc`:

```sh
npm ci --prefix apps/web
npm --prefix apps/web test
npm --prefix apps/web run build
npm --prefix apps/web run validate
```

UI до подключения PHP/Core использует demo data; его `/api/` не является настоящим backend.

## Контракты и зависимости

Проверка сгенерированных схем и OpenAPI:

```sh
cd apps/intelligence
uv run --frozen python scripts/export_contracts.py --check
```

После согласованного изменения Python-моделей экспортируйте схемы без `--check` и включите ожидаемые JSON diffs. Lock-файлы обновляйте командами `uv lock` и `uv export`, описанными в [infra README](../infra/README.md). Worker/contract payloads описаны в [`contracts/intelligence`](../contracts/intelligence/README.md).

## Проверяемые границы

- Core — единственный владелец user settings, прав, source policy, профиля и опубликованных business revisions.
- Probe URL — разовая проверка разрешённого grant; он не подписывает на издание.
- Поиск работает по уже разрешённому локальному корпусу, а не по всей сети.
- CSV prepare возвращает только proposal; сохранение контекста делает Core. Неизвестная единица выручки остаётся неизвестной.
- Модель вызывается только в явном `--mode model` с согласованной конфигурацией и budget. Confidential materials блокируют внешний вызов.
- Fixtures подменяют сеть и входные данные, не бизнес-смысл. Полный PHP путь, банковский корпус и независимая экспертная оценка остаются внешними проверками.

## Ветки и коммиты

Основная ветка — `main`. Рабочую ветку создавайте под одну задачу: `feat/<задача>`, `fix/<задача>` или `docs/<задача>`. После включения в `main` удаляйте её. Коммиты оформляйте как `type: описание на русском`, сохраняя своё обычное авторство. Инструкция для нового участника: [передача проекта](handoff.md).
