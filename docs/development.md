# Запуск и работа

Основной способ — [Docker](../infra/README.md). Команды выполняются из корня репозитория.

## Python без Docker

Нужны Python 3.12+ и uv:

```sh
cd apps/intelligence
uv sync --frozen
uv run --frozen python -m intelligence demo
uv run --frozen python -m intelligence evaluate
uv run --frozen python -m pytest -q
```

Для ключа из корневого `.env` добавьте `--env-file ../../.env` к `uv run`. Без явного `--mode model` анализ выполняет baseline. Для живого примера:

```sh
uv run --frozen --env-file ../../.env python -m intelligence analyze \
  --input examples/analysis-request.json --mode model --profile openrouter
```

Для разрешённого платного режима добавьте к команде анализа `--config examples/providers.paid.template.json`. [Ограничения и тарифы](llm-providers.md).

После изменения зависимостей обновите lock и производные файлы контейнера:

```sh
uv lock
uv export --frozen --no-dev --no-emit-project -o requirements.lock
uv export --frozen --only-group dev --no-emit-project -o requirements-dev.lock
```

Проверьте команды и схемы: `uv run python scripts/export_contracts.py --check`. Без `--check` команда обновляет JSON Schema и OpenAPI в `contracts/intelligence`.

## Интерфейс

Нужен Node.js из `.nvmrc`:

```sh
npm ci --prefix apps/web
npm --prefix apps/web test
npm --prefix apps/web run build
npm --prefix apps/web run validate
```

Готовые файлы находятся в `apps/web/dist`, их отдаёт Docker/nginx. Python API не заменяет пользовательский PHP API.

## Передача работы

Форматы Python-запросов и кандидата — в [contracts/intelligence](../contracts/intelligence/README.md). Backend реализует выдачу задания и сохранение результата на своей стороне; Frontend подключается к Core. Вместе проверяем пример с двумя компаниями и двумя ролями. Без реальных данных и прогонов модели не объявляем метрики качества достигнутыми.
