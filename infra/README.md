# Docker Compose и состояние

Команды запускаются из корня репозитория. Состав сервисов описан в [`compose.yaml`](../compose.yaml).

## Web demo

```sh
docker compose up --build --wait
```

React demo доступен на http://localhost:4173, порт опубликован только на loopback. Node.js находится в build/test stages; runtime контейнер — nginx со статическими файлами. Пока Symfony Core не подключён, экран использует собственные данные браузерного прототипа; `/api/` возвращает `503`. React demo и Python AI/Data не соединены.

## Python, тесты и offline demo

```sh
docker compose run --build --rm intelligence demo
docker compose run --build --rm intelligence demo-data
docker compose run --build --rm intelligence demo-personalization
docker compose run --build --rm intelligence evaluate --mode baseline
docker compose run --build --rm intelligence-test
docker compose run --build --rm web-test
```

`demo-data` выполняет отдельный полный локальный Python поток на синтетической RSS-фикстуре, без HTTP и внешнего LLM. `demo-personalization` сравнивает три синтетические компании на одном наборе событий и выводит полный JSON в терминал; его сигналы — эвристики, не вероятности. Эти команды не подменяют действия в React UI. `evaluate` проверяет функциональные cases, а не реальную точность модели. Test containers запускаются с `network_mode: none`.

Хостовый эквивалент, из каталога `apps/intelligence`:

```sh
uv sync --frozen
uv run --frozen python -m intelligence demo-data --state-dir /tmp/alfa-demo
uv run --frozen python -m intelligence run-task --mode baseline \
  --input examples/data-task.json --state-dir /tmp/alfa-task
uv run --frozen python -m pytest -q -p no:cacheprovider tests
```

`run-task` исполняет переданный `ai-data-task-v1` snapshot локально. Рабочая очередь и публикация бизнес-результата должны появиться на стороне Core; PHP Core в текущем Compose отсутствует.

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

`intelligence-api`, CLI `intelligence` и worker используют named volume `alfa-business-context-intelligence-state` под `/app/.state`: corpus, events, budgets, workflow state, results/outbox и private raw blobs. Обычный `docker compose down` сохраняет данные. Удаление volume через `docker compose down -v` необратимо удаляет этот локальный state; выполняйте его только если хотите очистить demo.

Хостовый запуск с `--state-dir /tmp/...` хранится отдельно от Docker. Настройки provider монтируются read-only: по умолчанию `apps/intelligence/examples/providers.template.json`; другой config path задаётся оператором через `INTELLIGENCE_CONFIG_FILE`. Тестовые containers не получают provider credentials.

## Модель и изменения зависимостей

Провайдеры используют отдельные operator-owned конфигурации; документация по режимам и live результатам: [провайдеры](../docs/llm-providers.md), [ai-live-checks](../docs/ai-live-checks.md). Секреты не помещайте в JSON configs, командные примеры или Git.

При согласованном изменении Python dependencies обновляйте lock-файлы из `apps/intelligence`:

```sh
uv lock
uv export --frozen --no-dev --no-emit-project -o requirements.lock
uv export --frozen --only-group dev --no-emit-project -o requirements-dev.lock
```

После этого выполните Docker test и schema check, прежде чем считать сборку воспроизводимой. Составная архитектура и внешние зависимости описаны в [architecture](../docs/architecture.md), [contracts](../contracts/intelligence/README.md) и [readiness matrix](../docs/ai-scope-and-readiness.md).


Профиль с несколькими направлениями и локальными источниками проверяется командой `docker compose run --build --rm intelligence demo-business`. Сравнение одинаковых событий по зависимостям: `docker compose run --build --rm intelligence demo-personalization` (JSON выводится в терминал). На хосте из `apps/intelligence` используйте `uv run --frozen python -m intelligence demo-personalization --output /tmp/personalization-comparison.json`. По умолчанию эти команды не вызывают модель. Для модели нужен явный `--mode model`, проверенный provider config и прежний общий ledger в named volume; новый state path не даёт нового разрешения на расходы. Описание и границы персонализации: [docs/personalization.md](../docs/personalization.md).

Дополнительный пример без внешней сети/модели: `docker compose run --build --rm intelligence demo-research`. Он проверяет получение промежуточных документов и ограничения покрытия; реальные выводы модели и турнир описаны [отдельно](../docs/research-and-tournament-checks.md).
