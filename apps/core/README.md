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
- `POST /api/companies/{id}/news/runs` — синхронно запустить ограниченный сбор и анализ по сохранённой политике источников. Только `admin` и `editor`, JSON `{}` или `{"maxEvents":1}`; ответ `200 {"run":...}` с результатом, `409` при активном запуске/отсутствии политики, `403` для `viewer`, `502` при неудаче или устаревшем снимке.
- `GET/PUT /api/me/onboarding` — прогресс знакомства.

Точные аннотации маршрутов находятся в `src/Controller`. Это текущие `/api/...` routes, не более ранний проектный `/api/v1` контракт.

## Core → Intelligence: ограниченная интеграция

`POST /api/companies/{id}/news/runs` вызывает общий runner `bin/run-news.php COMPANY_ID --max-events 1`. Запуск ограничен одним событием и тремя модельными вызовами. Обычный режим: таймаут внутреннего запроса 180 секунд и лимит Core-процесса 190 секунд. При `CORE_JUDGING_CASE=1`: внутренний таймаут 600 секунд и лимит Core-процесса 610 секунд, чтобы покрыть несколько вызовов Alfa LLM. Runner сохраняет короткую DB-backed блокировку по компании и версию контекста/политики, коммитит до HTTP-запроса, затем повторно проверяет версии перед публикацией. Внешний вызов выполняется вне DB-транзакции. В разрешённом judging-режиме runner использует `facts_json.ai_profile` из принятой версии и накладывает на него подтверждённые вручную поля; иначе использует legacy-поля контекста. Ответ запуска содержит status, версии и сохранённые coverage/events/candidates/collection, включая provider metadata, если она пришла от Intelligence. Устаревший snapshot сохраняется как stale и не публикуется как успешный результат.

Runner отправляет `ai-data-task-v1` в `POST /internal/v1/analyze` с bearer из `/run/intelligence-auth/service-token`; для tenant берётся `CORE_INTELLIGENCE_TENANT_ID` (Compose default `core-local`). В обычном режиме срабатывает baseline, а классификация ограничивает внешний model route. Для operator case Intelligence применяет case policy к сбору разрешённых источников; policy snapshot всё равно формируется из Core allowlist. Обработка confidential case-профиля разрешена только отдельным явным gateway allowlist с операторским флагом, tenant и подтверждённым происхождением импорта; это не разрешение отправлять тот же материал в произвольного провайдера. LLM-модель отдельно задаётся настройками Intelligence.

`bin/run-due-news.php` выбирает компании с enabled sources и запускает их не чаще одного раза в час. Опциональный Compose `scheduler` profile повторяет эту команду. HTTP endpoint и scheduler используют прямой ограниченный синхронный workflow; это не durable analysis job queue и не предоставляет claim/heartbeat/result/failure, lease/fencing, отмену или прогресс фоновой задачи. Python worker из `apps/intelligence` не следует считать подключённым к PHP Core queue.

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

`tests/run.php` использует временную SQLite fixture и проверяет импорт, хеширование пароля, членство, версионную запись контекста с сохранением импортированного профиля, метрики, события, ACL запуска и ограничение тела `/news/runs`. Проверки маршрута не запускают платную модель. Core production schema запускается на PostgreSQL отдельно от AI DB. Скрипты импорта требуют предоставленных и проверенных файлов; настоящие CSV/PDF не включены.
