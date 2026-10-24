# Бизнес-контекст

Сервис помогает руководителю понять, какие новости могут повлиять на его компанию, и увидеть основания, ограничения и неизвестное.

[Начать работу с проектом](docs/handoff.md) · [Разработка](docs/development.md) · [Контракты](contracts/intelligence/README.md)

## Компоненты и границы

| Часть | Каталог | Ответственность |
|---|---|---|
| Пользовательский экран | `apps/web` | React-интерфейс; до Core работает на демонстрационных данных |
| Бизнес-ядро | `apps/core` | Symfony/PHP Core: пользователи, права, компании, настройки источников, расписания и публикация результатов; production-реализации пока нет |
| Анализ данных | `apps/intelligence` | Python: сбор по разрешённым snapshot, корпус, события, поиск, анализ и Core worker |

Core владеет правами, контекстом и source policy. Python не меняет эти бизнес-данные: он получает версионированную задачу и возвращает результат-кандидат. Fixture Core stand позволяет проверить контракт с настоящим Python worker, но не заменяет PHP. Оценка на реальных банковских материалах не проведена.

Читайте [интеграцию Backend/Frontend](docs/ai-integration.md), [процесс работы](docs/ai-process.md) и [матрицу функций и готовности](docs/ai-scope-and-readiness.md). Текущие live-проверки и расходы ведутся отдельно в [ai-live-checks.md](docs/ai-live-checks.md).

## Запуск интерфейса и Python в Docker

Веб-экран доступен на http://localhost:4173:

```sh
docker compose up --build --wait
```

Без ключей и запросов к внешним сервисам:

```sh
docker compose run --build --rm intelligence demo
docker compose run --build --rm intelligence demo-data
docker compose run --build --rm intelligence evaluate --mode baseline
docker compose run --build --rm intelligence-test
docker compose run --build --rm web-test
```

`demo-data` исполняет локальный сквозной путь на синтетической RSS-фикстуре. Остановка: `docker compose down`; обычный `down` сохраняет named volume с Python state.

## Python на хосте

Команды выполняются из `apps/intelligence`:

```sh
.venv/bin/python -m intelligence --help
.venv/bin/python -m intelligence demo-data --state-dir /tmp/alfa-demo
.venv/bin/python -m intelligence run-task --mode baseline \
  --input examples/data-task.json --state-dir /tmp/alfa-task
.venv/bin/python -m pytest -q -p no:cacheprovider tests
```

Для worker/API используйте инструкции в [интеграционном руководстве](docs/ai-integration.md); внешняя модель запускается только при явном выборе `--mode model` и настроенном разрешённом профиле. Реальные банковские CSV/PDF не включены в репозиторий.

## Разработка и контракты

- [Python CLI и внутренняя реализация](apps/intelligence/README.md)
- [Контракты Python для Backend](contracts/intelligence/README.md)
- [Устройство проекта](docs/architecture.md)
- [Локальная разработка и проверки](docs/development.md)
- [Docker и volumes](infra/README.md)
- [Backlog и внешние зависимости](docs/backlog.md)
