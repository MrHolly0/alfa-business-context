# Статус web-приложения

React UI сейчас подключён к Symfony Core через nginx reverse proxy: browser requests на `/api/` направляются в `core:8080`. Node нужен только для сборки и тестов; runtime image обслуживает статические файлы nginx.

## Рабочий Core-путь

При первом открытии показывается форма Core login. Core проверяет пароль и устанавливает HTTP-only SameSite=Strict cookie. Если сессия активна, UI загружает список компаний, company/context, metrics, dashboard и news read views. Список ограничен memberships в Core. Контекст сохраняется с `expectedVersion`; stale write получает `409`. Роль `viewer` показывает форму только для чтения. Выход вызывает Core logout.

Seed account `demo@alpha.test` / `DemoAlpha2026!` существует только для локального Compose demo. Это не банковский SSO и не credentials для внешнего окружения. Для старого клиентского макета есть явный переход «Посмотреть демо»; там данные/изменения остаются в `localStorage`, это отдельный режим без server-side авторизации.

## Что UI пока не делает

UI читает Core news sources/documents/runs/assessments; пользовательский экран не создаёт источник, не инициирует Python сбор и не запускает общий Analysis Job. PHP/Core пока не публикует job status/progress API, а UI не подключён к Python worker pipeline. Dashboard метрики/события читаются из Core, но аналитическое summary может быть `no_analysis`. Нет SSE; отдельные обновления требуют обычных HTTP reads.

Новостной сбор настраивается оператором вне UI через Core CLI и может запускаться вручную или scheduler profile. Он вызывает Python API прямым HTTP baseline путём. См. [демо](demo.md), [Core](../apps/core/README.md), [Docker Compose](../infra/README.md).
