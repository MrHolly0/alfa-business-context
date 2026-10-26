# Локальная система Docker Compose

Команды выполняются из корня. Главный режим демонстрации собирает и запускает web, PHP Core, Python Intelligence API и PostgreSQL:

```sh
docker compose up --build --wait
```

Web открывается по http://localhost:4173, Core непосредственно доступен на http://localhost:8090, Python API — http://localhost:8081; опубликованные порты привязаны к loopback. Для основного сценария входите через web, чтобы cookie оставалась same-origin. Локальный demo user создаётся миграционным контейнером: `demo@alpha.test` / `DemoAlpha2026!`. Эти известные значения предназначены только для локального режима.

## Что запускается по умолчанию

`postgres` поднимает один локальный PostgreSQL 17.11 и при первом создании volume подготавливает отдельные базы `business_context_ai` и `business_context_core` с разными owner, app и migration roles. У приложений нет общей схемы и runtime SQL credentials между базами. `intelligence-migrate` и `core-migrate` выполняются до запуска сервисов; последний применяет Core schema и при `CORE_SEED_DEMO=1` (по умолчанию) создаёт demo user/company. `service-auth` генерирует внутренний bearer и пишет его в persistent volume, откуда Core и Intelligence API читают файл read-only. Web nginx проксирует `/api/*` в Core.

Dev значения паролей в Compose fallback и `.env.example` слабые, loopback-only и только для локальной разработки. Для общего/внешнего окружения заменить все bootstrap/application/migration пароли отдельными секретами. Смена env после первого создания volume не меняет роли PostgreSQL автоматически. `INTELLIGENCE_SERVICE_TOKEN`, если задан при первом создании, должен содержать минимум 24 символа и становится сохранённым токеном; не меняйте его поверх существующего service-auth volume без согласованной ротации. Provider keys передаются Python, не PHP/web.

## Проверки

```sh
npm test
```

Или по сервисам:

```sh
docker compose --profile test run --build --rm core-test
docker compose --profile test run --build --rm web-test
docker compose --profile test run --build --rm intelligence-test
```

Полный тестовый профиль не запускает product login stack: `core-test` использует отдельную тестовую SQLite fixture, `web-test` работает offline, AI tests используют `business_context_ai_test` после миграции. CI запускает все три. Результаты проверки полного локального Compose/Core/Web/Python пути приведены в [отчёте 26 октября](../docs/product-docker-checks.md). Конфигурацию Compose можно отдельно проверить командой `docker compose config --quiet`.

## Новости через PHP Core

Демо seed не создаёт news sources. Оператор может зарегистрировать источник с `php bin/add-news-source.php` внутри Core и выполнить `php bin/run-news.php COMPANY_ID`. Для команды и ограничений см. [разработку](../docs/development.md) и [Core README](../apps/core/README.md).

`--profile scheduler` включает повторное исполнение `php bin/run-due-news.php` из PHP Core. Оно выбирает компании с разрешёнными enabled sources, не проверявшиеся в последний час. Core строит snapshot и вызывает Intelligence API `POST /internal/v1/analyze` напрямую; tenant scope берётся из `CORE_INTELLIGENCE_TENANT_ID` (по умолчанию `core-local`). Внутренний API допускает этот точный ID. Режим по умолчанию `baseline` не вызывает внешнюю модель. Это рабочая интеграция сбора новостей, но не полноценный Core job queue, lease worker, result endpoint или очередь Python.

Python профиль `worker` существует как контрактный worker для CoreClient/worker сценариев, но его endpoints claim/heartbeat/result/failure не реализованы в PHP Core. Не включайте его как способ запуска продуктового анализа. Для автономных Python fixture-прогонов используйте `--profile tools` и команды в [README Intelligence](../apps/intelligence/README.md).

## Данные и остановка

PostgreSQL хранится в volume `alfa-business-context-postgres`; файлы Python — в `alfa-business-context-intelligence-state`; service bearer, PHP app secret и session files — в своих volumes. `docker compose down` останавливает контейнеры и оставляет данные. `docker compose down -v` удаляет локальные базы, raw files, пользовательские сессии и внутренний bearer; применяйте только если действительно хотите очистить локальное состояние.

Полный набор переменных — `.env.example`; пользовательский `.env` не входит в работу и не должен перезаписываться. Модельные конфиги монтируются в Python read-only, а ключи не включаются в образ. Сторонние зависимости и ограничения перечислены в [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md).

## Данные организаторов и Альфа

Отдельный режим готовит оригинальные CSV/PDF, локальный OCR, источники и модельный анализ без изменения `.env`. Команда и порядок проверки — в [инструкции для судей](../docs/judging.md). Для обычного запуска и тестов используется базовый Compose; внешний провайдер включается только judging overlay.
