# Docker Compose и состояние

Команды запускаются из корня репозитория. Состав сервисов описан в [`compose.yaml`](../compose.yaml).

## Web demo

```sh
docker compose up --build --wait
```

React demo доступен на http://localhost:4173, порт опубликован только на loopback. Node.js находится в build/test stages; runtime контейнер — nginx со статическими файлами. Пока Symfony Core не подключён, экран использует собственные данные браузерного прототипа; `/api/` возвращает `503`. React demo и Python AI/Data не соединены.

## Python, тесты и offline demo

```sh
docker compose --profile tools run --build --rm intelligence demo
docker compose --profile tools run --build --rm intelligence demo-data
docker compose --profile tools run --build --rm intelligence demo-personalization
docker compose --profile tools run --build --rm intelligence evaluate --mode baseline
docker compose --profile test run --build --rm intelligence-test
docker compose --profile test run --build --rm web-test
```

PostgreSQL 17 хранит рабочие данные Python Intelligence в отдельной базе `business_context_ai`; Compose автоматически запускает `intelligence-migrate` перед CLI/API/worker. `--profile tools` включает этот поток. Тестовый профиль использует отдельную `business_context_ai_test` и выполняет `intelligence-test-migrate` до тестов. Роли и DSN описаны в [руководстве PostgreSQL](../docs/postgresql.md).

`demo-data` выполняет отдельный полный локальный Python поток на синтетической RSS-фикстуре, без внешнего HTTP и LLM. `demo-personalization` сравнивает три синтетические компании на одном наборе событий и выводит полный JSON в терминал; его сигналы — эвристики, не вероятности. Эти команды не подменяют действия в React UI. `evaluate` проверяет функциональные cases, а не реальную точность модели. Тестовый сервис получает отдельную PostgreSQL test-базу и не получает provider credentials.

Для хостовых команд из корня сначала запустите PostgreSQL, затем перейдите в `apps/intelligence`:

```sh
docker compose --profile database up -d --wait postgres
cd apps/intelligence
uv sync --frozen
export INTELLIGENCE_DATABASE_URL='postgresql://intelligence_app:local-ai-only@127.0.0.1:55483/business_context_ai'
INTELLIGENCE_MIGRATION_DATABASE_URL='postgresql://intelligence_migrator:local-migration-only@127.0.0.1:55483/business_context_ai' \
  uv run --frozen python -m intelligence db migrate
uv run --frozen python -m intelligence db check
uv run --frozen python -m intelligence demo-data --state-dir /tmp/alfa-demo
uv run --frozen python -m intelligence run-task --mode baseline \
  --input examples/data-task.json --state-dir /tmp/alfa-task
```

`run-task` исполняет переданный `ai-data-task-v1` snapshot локально. Рабочая очередь и публикация бизнес-результата должны появиться на стороне Core; PHP Core в текущем Compose отсутствует.

Для тестов из корня выполните миграции test DB, затем задайте обе test DSN и запустите хостовый pytest:

```sh
docker compose --profile test up -d --wait postgres
docker compose --profile test run --build --rm intelligence-test-migrate
cd apps/intelligence
INTELLIGENCE_DATABASE_URL='postgresql://intelligence_app:local-ai-only@127.0.0.1:55483/business_context_ai_test' \
INTELLIGENCE_MIGRATION_DATABASE_URL='postgresql://intelligence_migrator:local-migration-only@127.0.0.1:55483/business_context_ai_test' \
  uv run --frozen python -m pytest -q -p no:cacheprovider tests
```

## Внутренний Python API

Compose-профиль `analysis` включает API с loopback port mapping. Для закрытых маршрутов оператор задаёт локально `INTELLIGENCE_SERVICE_TOKEN` и `INTELLIGENCE_TENANTS`; при пустой настройке приватные вызовы отклоняются.

```sh
docker compose --profile analysis up --build --wait intelligence-api
curl --fail http://127.0.0.1:8081/health
```

Режим по умолчанию — `INTELLIGENCE_MODE=baseline`. Внешняя модель возможна только при явном выборе режима, настроенном профиле/provider и допустимых source/data classifications/budget. Web UI обращается к Core; Python API — внутренняя граница для доверенного Backend.

## Core worker

Compose profile `worker` запускает Python worker. Он требует доступный Core endpoint и `CORE_SERVICE_TOKEN`; `CORE_BASE_URL` по умолчанию указывает на `http://core:8080`, которого в этом Compose нет. Worker не заработает production-путём без настроенного Core:

```sh
docker compose --profile worker up --build intelligence-worker
```

Для проверки контракта без PHP запустите standalone fixture-only stand из `apps/intelligence` с `--fixture-mode` и тестовый worker по командам из [development guide](../docs/development.md). Stand слушает только loopback и не является заменой production auth, permissions или Core job storage.

## Volumes и остановка

Private raw blobs и файловые артефакты хранятся в `alfa-business-context-intelligence-state`; рабочие таблицы — в PostgreSQL volume `alfa-business-context-postgres`. Обычный `docker compose down` сохраняет оба volume. Удаление через `docker compose down -v` удаляет и БД, и private filesystem; это очистка локального стенда.

Хостовый запуск использует ту базу/namespace, которые заданы DSN и `INTELLIGENCE_STORAGE_NAMESPACE`; `--state-dir` формирует logical namespace и путь файлового хранилища. Настройки provider монтируются read-only: по умолчанию `apps/intelligence/examples/providers.template.json`; другой config path задаётся оператором через `INTELLIGENCE_CONFIG_FILE`. Тестовые containers не получают provider credentials.

## Модель и изменения зависимостей

Провайдеры используют отдельные operator-owned конфигурации; документация по режимам и live результатам: [провайдеры](../docs/llm-providers.md), [ai-live-checks](../docs/ai-live-checks.md). Секреты не помещайте в JSON configs, командные примеры или Git.

При согласованном изменении Python dependencies обновляйте lock-файлы из `apps/intelligence`:

```sh
uv lock
uv export --frozen --no-dev --no-emit-project -o requirements.lock
uv export --frozen --only-group dev --no-emit-project -o requirements-dev.lock
```

После этого выполните Docker test и schema check, прежде чем считать сборку воспроизводимой. Составная архитектура и внешние зависимости описаны в [architecture](../docs/architecture.md), [contracts](../contracts/intelligence/README.md) и [readiness matrix](../docs/ai-scope-and-readiness.md).


Профиль с несколькими направлениями и локальными источниками проверяется командой `docker compose --profile tools run --build --rm intelligence demo-business`. Сравнение одинаковых событий по зависимостям: `docker compose --profile tools run --build --rm intelligence demo-personalization` (JSON выводится в терминал). На хосте из `apps/intelligence` используйте `uv run --frozen python -m intelligence demo-personalization --output /tmp/personalization-comparison.json`. По умолчанию эти команды не вызывают модель. Для модели нужен явный `--mode model`, проверенный provider config и общая PostgreSQL budget storage scope; новый logical path не создаёт нового разрешения на расходы. Описание и границы персонализации: [docs/personalization.md](../docs/personalization.md).

Дополнительный пример без внешней сети/модели: `docker compose --profile tools run --build --rm intelligence demo-research`. Он проверяет получение промежуточных документов и ограничения покрытия; реальные выводы модели и турнир описаны [отдельно](../docs/research-and-tournament-checks.md).
