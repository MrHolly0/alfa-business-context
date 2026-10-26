# Компоненты и ответственность

| Каталог | Ответственность | Состояние |
|---|---|---|
| `apps/web` | React UI: вход в Core, выбор разрешённой компании, чтение dashboard/news, редактирование контекста и метрик. | Работает через same-origin `/api/` proxy в nginx. Старое локальное демо открывается отдельной явной кнопкой. |
| `apps/core` | Symfony 7.4/PHP Core: cookie-auth, users/memberships, company access, контекст с CAS, метрики, dashboard, ручные события, feedback/discussions, news source/run storage. | Runtime есть в Compose; PostgreSQL Core database, роль приложения и мигратор отделены от AI. Общей Analysis Job queue пока нет. |
| `apps/intelligence` | Python/FastAPI, сбор/хранение источников, события, baseline и model workflows, внутренний API и контрактный worker. | API интегрирован с Core для прямого news-analysis call; полная очередь Core claim/lease/result не реализована. |
| `compose.yaml`, `infra/` | Локальные контейнеры, раздельная инициализация баз и ролей, persistent service bearer, Core/Python/web. | Основной локальный запуск `docker compose up --build --wait`; отдельные `test`, `tools`, `scheduler`, `worker` profiles. |
| `contracts/intelligence` | Версионируемые Python request/result schemas, OpenAPI и examples. | Описывает AI-side contract и fixture/worker flows. Не является свидетельством, что PHP job API реализован. |
| `docs` | Архитектура, команды, ограничение поведения и планы. | В каждой инструкции различаются работающий runtime и ещё не реализованные функции. |

## Владение данными и вызовы

Core PostgreSQL database содержит users, memberships, companies, версии контекста, метрики, источники, news runs/events/assessments и discussions. AI PostgreSQL database хранит документы, извлечённые данные, события, артефакты и индексы. Сервисы имеют отдельные application/migration credentials; один Postgres container в Compose не означает общую схему/права.

- **Браузер → Core:** cookie-сессия, проверка membership и role; UI не получает provider/service credentials.
- **Core → Python:** текущий news-run snapshot передаётся прямым внутренним HTTP-вызовом `/internal/v1/analyze`; режим Compose — baseline, tenant deployment scope по умолчанию `core-local`.
- **Python worker → Core:** отдельный worker contract в Intelligence, но PHP API endpoints claim/heartbeat/result/failure и durable Core jobs пока отсутствуют. Не запускать как продуктовый job pipeline.
- **Core scheduler → Core CLI → Python API:** опциональный PHP scheduler вызывает существующий `run-due-news.php`; он обрабатывает enabled news sources не чаще раза в час. Это не общая очередь.

Детали: [архитектура](architecture.md), [интеграция AI](ai-integration.md), [Symfony Core](symfony-integration.md), [локальный запуск](development.md).
