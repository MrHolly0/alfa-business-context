# Документация

## Знакомство с решением

1. [README проекта](../README.md) — назначение, состав и быстрый запуск.
2. [Сценарий демонстрации](demo.md) — что проверить в интерфейсе и Python отдельно.
3. [Как работает анализ](ai-process.md) — вход, этапы и результат.
4. [Архитектура](architecture.md) — границы интерфейса, PHP и Python.
5. [Ревью и исправления](ai-review-and-fixes.md) — дополнительные сценарии, результаты и ограничения восстановления.
6. [Проверки и расходы](ai-live-checks.md) — выполненные прогоны и ограничения качества.
7. [Матрица готовности](ai-scope-and-readiness.md) — реализация и проверка каждого требования.

## Разработка и интеграция

- [Установка и команды](development.md), [Docker](../infra/README.md).
- [Подключение Backend/Frontend](ai-integration.md), [контракты Python](../contracts/intelligence/README.md).
- [Python-модуль](../apps/intelligence/README.md), [состояние интерфейса](web-status.md).
- [Настройки аналитика](../apps/intelligence/docs/analysts.md), [турнир](../apps/intelligence/docs/tournament.md).
- [Источники моделей и бюджеты](llm-providers.md), [подготовка CSV](client-data.md).
- [Правила изменений](../CONTRIBUTING.md), [распределение работы](team.md), [ближайшие задачи](backlog.md).
- [Требования кейса](requirements.md), [решение о Symfony/PHP](decisions/001-symfony-core.md), [целевая модель данных](data-model.md).

## Архив

[Ранние планы](reference/README.md) и [версии макетов](../apps/web/docs/README.md) сохранены для истории. Они не описывают текущий запуск. Действующие команды находятся в README проекта и инструкции разработки.

Подробная проверка многопрофильного сценария, версий и подключения: [сквозной сценарий](ai-workflow-checks.md).
