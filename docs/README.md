# Документация

## Сначала

- [Корневой README](../README.md) — назначение, текущие runtime-компоненты и быстрый запуск.
- [Демо-сценарий](demo.md) — Core UI и ограниченный Core→Python новостной путь.
- [Архитектура](architecture.md) — границы систем и реализованное/планируемое.
- [Статус web](web-status.md) и [Symfony Core](../apps/core/README.md) — действующие API и ограничения.
- [Python AI/Data](../apps/intelligence/README.md) и [AI integration](ai-integration.md) — модели данных, задачи и внутренние контракты.

## Разработка

- [Development guide](development.md), [Docker Compose](../infra/README.md), [PostgreSQL](postgresql.md).
- [Symfony integration](symfony-integration.md) — контракты и целевые jobs/worker решения; текущий прямой вызов отмечен отдельно.
- [Ближайшие задачи](backlog.md), [матрица готовности](ai-scope-and-readiness.md), [контракт Python](../contracts/intelligence/README.md).
- [Источники моделей и бюджет](llm-providers.md), [подготовка клиентского контекста](client-data.md).

Архивные макеты и решения находятся в [`reference`](reference/README.md) и [`apps/web/docs`](../apps/web/docs/README.md); они могут описывать ранние состояния. Если команда расходится с текущим кодом, сверяйте актуальные compose.yaml, контроллеры и команды миграции.

[Проверка общего Docker-запуска](product-docker-checks.md).
