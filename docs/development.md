# Разработка и локальная проверка

## Полный локальный запуск

Нужны Docker и Compose. Из корня:

```sh
docker compose up --build --wait
```

Откройте http://localhost:4173 и войдите `demo@alpha.test` / `DemoAlpha2026!` (только для локального демо). Для чистого браузерного прототипа нажмите «Посмотреть демо». Параметры локальных портов и dev DB пароли перечислены в `.env.example`; не копируйте эти значения в общий deployment.

Первый запуск автоматически создаёт отдельные Core/AI базы, роли, таблицы и Core demo seed. Переменные `CORE_DB_PASSWORD`, `CORE_MIGRATION_DB_PASSWORD`, `INTELLIGENCE_DB_PASSWORD` и другие bootstrap credentials применяются при инициализации volume; изменение env не меняет уже сохранённые пароли. Для обычной остановки используйте `docker compose down`, не `down -v`, если нужно сохранить данные.

## Тесты

Из корня запустите:

```sh
npm test
```

Отдельные Docker-команды:

```sh
docker compose --profile test run --build --rm core-test
docker compose --profile test run --build --rm web-test
docker compose --profile test run --build --rm intelligence-test
```

Core-тесты запускаются без сети и используют временную SQLite fixture для миграции/бизнес API; Compose Core production использует отдельную PostgreSQL database. Web-тесты запускаются в offline test image. Python suite использует `business_context_ai_test` и отдельную роль приложения/мигратора; провайдерские ключи ему не передаются. GitHub Actions настроен на те же три тестовых контура. Отдельная проверка общего запуска и браузерных сценариев описана в [отчёте интеграции](product-docker-checks.md).

## Новостной путь Core → Python

Для локального baseline-пути сначала запустите полный Compose stack и получите company ID:

```sh
docker compose exec core php bin/seed-demo.php
docker compose exec core php -r 'require "vendor/autoload.php"; $db=App\Database::connect(); echo $db->query("SELECT id FROM companies ORDER BY id LIMIT 1")->fetchColumn(), PHP_EOL;'
```

Добавьте только разрешённый учебный RSS URL с правом на запросы и доменами статей, затем вручную запустите сбор:

```sh
docker compose exec core php bin/add-news-source.php COMPANY_ID https://example.org/feed.xml rss external public 'Учебный источник' 'Издатель' 2026-10-26T00:00:00+00:00 example.org
docker compose exec core php bin/run-news.php COMPANY_ID
```

Замените `COMPANY_ID` на прочитанный ID и примерный домен/URL на источник, который разрешено опрашивать. Команда не нужна для базового UI: без источников dashboard и news read views пусты. Не добавляйте закрытые материалы, банковские PDF/CSV или ссылки без права на загрузку.

Опциональный расписанный сбор:

```sh
docker compose --profile scheduler up --build --wait
```

Профиль запускает тот же PHP CLI `run-due-news.php` примерно раз в минуту; скрипт не чаще раза в час обрабатывает каждую компанию с enabled sources. Это scheduler конкретного news command, не общий job worker и не Python queue.

## Python отдельно

Для синтетических тестовых путей без LLM:

```sh
docker compose --profile tools run --build --rm intelligence demo-data
docker compose --profile tools run --build --rm intelligence evaluate --mode baseline
```

Команды Python API/worker для разработки, schemas и fixture stand описаны в [README Intelligence](../apps/intelligence/README.md). Fixture worker не подтверждает Core job queue: полноценные claim/lease/result endpoints в текущем Core отсутствуют. Модельный режим требует явно настроенного provider-конфига и ключа в локальном env; baseline — значение по умолчанию.

## Изменение PHP и web

Core работает из `apps/core`; зависимости фиксируются `composer.lock`. Web package находится в `apps/web` и собирается Node 22.15 в build stage; runtime — nginx. Изменение frontend проверяйте `npm --prefix apps/web test`, `npm run build`, `npm run validate`. Core unit/integration suite: `composer test` из `apps/core` либо Compose `core-test`. Не редактируйте `.env` с личными настройками для проверки проекта; пример переменных для репозитория — `.env.example`.

См. [infra README](../infra/README.md), [Symfony/Core](../apps/core/README.md), [web](../apps/web/README.md), [архитектуру](architecture.md).

## Данные организаторов и Альфа

Отдельный режим готовит оригинальные CSV/PDF, локальный OCR, источники и модельный анализ без изменения `.env`. Команда и порядок проверки — в [инструкции для судей](judging.md). Для обычного запуска и тестов используется базовый Compose; внешний провайдер включается только judging overlay.

На 26 октября зафиксированы чистая первая и повторная подготовка judging-данных и ограниченный HTTP сценарий с тремя модельными профилями; ответы имеют `partial`. Детали результата и границы проверки приведены в [отчёте Docker/Core/Web/Python](product-docker-checks.md).
