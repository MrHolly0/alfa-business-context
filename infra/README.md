# Docker

Из корня репозитория:

```sh
docker compose up --build --wait
```

Веб-интерфейс — http://localhost:4173. Node собирает React, nginx отдаёт готовые файлы. До подключения PHP Core интерфейс работает на демонстрационных данных; `/api/` отвечает `503`.

## Python

Сквозной пример, сценарии и тесты без ключей:

```sh
docker compose run --build --rm intelligence demo
docker compose run --build --rm intelligence evaluate
docker compose run --build --rm intelligence-test
```

Конфиг моделей монтируется read-only в `/config/providers.json`. По умолчанию используется `apps/intelligence/examples/providers.template.json`; другой файл выбирается через `INTELLIGENCE_CONFIG_FILE` в `.env`. Ключи моделей передаются только Python-контейнерам. [Живой анализ и тарифы](../docs/llm-providers.md).

## Внутренний HTTP API

В корневом `.env` задайте `INTELLIGENCE_SERVICE_TOKEN` и список `INTELLIGENCE_TENANTS`, например `demo`. При пустом значении приватные маршруты закрыты. Токен выбираете сами и передаёте только Backend.

```sh
docker compose up --build --wait intelligence-api
curl --fail http://localhost:8081/health
```

По умолчанию `INTELLIGENCE_MODE=baseline`. Значение `model` включает модель только для принятых запросов анализа. `INTELLIGENCE_PROFILE=openrouter` использует выбранный конфиг Gemma: исходный бесплатный либо отдельный платный с лимитами; у хакатонного профиля предварительно заполняются тарифы. Пользовательский интерфейс обращается к PHP, внутренний Python API нужен Backend.

## Worker для Core

После запуска PHP задайте `CORE_BASE_URL`, `CORE_SERVICE_TOKEN`, `INTELLIGENCE_TENANTS` и режим анализа в `.env`:

```sh
docker compose up --build intelligence-worker
```

Для однократного получения задания: `docker compose run --rm intelligence-worker worker --once --mode baseline`. [Маршруты и подтверждения](../contracts/intelligence/README.md). Без PHP worker не сможет получить задание; сам PHP этот Compose не создаёт.

## Хранение и остановка

`docker compose down` останавливает контейнеры. SQLite-корпус, результаты и журнал расходов остаются в volume `alfa-business-context-intelligence-state`. Не добавляйте `-v` для обычной остановки. Хостовый запуск использует отдельный каталог `apps/intelligence/.state`, итоги с Docker не объединяются.

Тестовые контейнеры работают без сетевого доступа и не получают ключи. Веб-тесты: `docker compose run --build --rm web-test`.

При прерванном запросе: `docker compose run --rm intelligence inspect`. Сначала остановите API/worker и проверьте неопределённый расход; только затем при необходимости `reset-request --tenant demo --request-id <ID> --confirm-stopped` или `reset-job --job-id <ID> --generation <N> --confirm-stopped`. Сброс не закрывает неопределённые записи денежного журнала и не гарантирует отсутствие повторного списания.

Турнир настроек использует тот же образ и volume Python: `docker compose run --build --rm intelligence arena run --run-id demo-1`. По умолчанию работает без сети; отчёты и реестр остаются в `.state` внутри volume. [Команды и модельный режим](../apps/intelligence/README.md#подбор-настроек-и-турнир).

Просмотр очереди доставки: `docker compose run --rm intelligence inspect`. Новые поля ревизий корпуса и повторов доставки добавляются в существующую SQLite автоматически, без очистки volume и сохранённых сообщений. Параллельные миграции сериализуются. `.env` для обновления не меняется.
