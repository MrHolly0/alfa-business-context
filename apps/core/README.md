# Symfony Core

Core — PHP 8.4 + Symfony 7.4 приложение для локального продукта. В Docker Compose оно обслуживает пользовательский `/api`, хранит Core business data в `business_context_core` и отдельно от AI/Data базы. Входная точка команды — [корневой README](../../README.md); полный запуск через Compose предпочтителен для демонстрации.

## Основной запуск

```sh
docker compose up --build --wait
```

Миграционный контейнер выполняет `bin/schema.php`, затем при `CORE_SEED_DEMO=1` вызывает `bin/seed-demo.php`. Демо-вход: `demo@alpha.test` / `DemoAlpha2026!`. Этот account/company — локальный synthetic seed. Отключить seed можно `CORE_SEED_DEMO=0` до создания/миграции базы.

Core runtime использует роль `core_app`; миграции — отдельную `core_migrator` с членством в `core_owner`. Вне Docker можно запустить Composer и PHP server, но это не даёт полного demo стека и требует самостоятельно передать PDO SQLite или отдельный Core PostgreSQL DSN, APP_SECRET и настроить AI HTTP auth, если нужен news path.

## Реализованные API

Auth использует cookie `alfa_core` с HttpOnly, SameSite=Strict и 30 минутами бездействия. `POST /api/auth/login` проверяет password hash; `GET /api/auth/me` возвращает текущего пользователя; `POST /api/auth/logout` закрывает сессию. Запись проверяет same-origin. Пользователь видит только компании из `company_memberships`; `viewer` не может редактировать контекст/метрики/наблюдения/события.

- `GET /api/companies`, `GET /api/companies/{id}` — список и карточка компании с проверкой членства.
- `GET/PUT /api/companies/{id}/context` — факты компании и CAS по `expectedVersion`; несоответствие версии возвращает `409`.
- `GET/POST /api/companies/{id}/metrics`, `PATCH /api/companies/{id}/metrics/{metricId}`, `POST .../observations` — метрики, версии определений и ручные наблюдения.
- `GET /api/companies/{id}/dashboard` — сводка метрик и бизнес-событий. Аналитический summary остаётся `no_analysis`, если отдельно не записан результат.
- `GET/POST /api/companies/{id}/events` и маршруты metric/event associations — ручные события и подтверждённые связи.
- `GET/POST /api/companies/{id}/feedback` и `/discussions` — обратная связь и обсуждения.
- `GET /api/companies/{id}/news/sources`, `/news/documents`, `/news/runs`, `/news/runs/{runId}`, `/news/events/{eventId}/assessment` — чтение Core news records.
- `GET/PUT /api/me/onboarding` — прогресс знакомства.

Точные аннотации маршрутов находятся в `src/Controller`. Это текущие `/api/...` routes, не более ранний проектный `/api/v1` контракт.

## Core → Intelligence: ограниченная интеграция

`bin/run-news.php COMPANY_ID` формирует `ai-data-task-v1` из Core context/source-policy snapshot и отправляет `POST /internal/v1/analyze` в Python с bearer из `/run/intelligence-auth/service-token`. Для вызывающего tenant берётся `CORE_INTELLIGENCE_TENANT_ID`, Compose default `core-local`. Core проверяет tenant в ответе и сверяет актуальные context/policy версии перед записью. Baseline — режим по умолчанию; платная модель отдельно настраивается в Intelligence.

`bin/run-due-news.php` выбирает компании с enabled sources и запускает их не чаще одного раза в час. Опциональный Compose `scheduler` profile повторяет эту команду. Это прямой ограниченный HTTP workflow; PHP Core не реализует общую Analysis Job queue с worker claim/heartbeat/result/failure, fencing или UI job progress. Python worker из `apps/intelligence` не следует считать подключённым к PHP Core queue.

## Локальные команды и тесты

В `apps/core`:

```sh
composer install
composer test
```

Для Compose-совместимого тестового контейнера из корня:

```sh
docker compose --profile test run --build --rm core-test
```

`tests/run.php` использует временную SQLite fixture и проверяет импорт, хеширование пароля, членство, версионную запись контекста, метрики, события и базовые API permissions. Core production schema запускается на PostgreSQL отдельно от AI DB. Скрипты импорта требуют предоставленных и проверенных файлов; настоящие CSV/PDF не включены.
