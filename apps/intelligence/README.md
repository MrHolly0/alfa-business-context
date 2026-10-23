# Поиск и анализ

Наша Python-часть получает профиль компании и новость, ищет основания в документах и возвращает кандидата оценки. PHP Core проверяет доступ, выдаёт задания и сохраняет опубликованный результат. Его код здесь не реализуется.

## Что работает

- Импорт текста, HTML, RSS и PDF; версии, даты, устранение точных дублей.
- Объяснимый предварительный отбор событий по профилю компании.
- Лексический поиск с ограничением tenant, источников, даты и ревизий.
- План → поиск → проверка → дополнительный поиск при нехватке оснований → анализ → проверка ответа.
- Разделение внешних фактов, банковской аналитики, гипотез, рекомендаций и неизвестного.
- Вызовы разрешённых моделей через хакатонный шлюз или Gemma на OpenRouter: бесплатный профиль и отдельно разрешённый платный с лимитами; журнал расходов SQLite.
- Внутренний HTTP API и worker с heartbeat, защитой от устаревшего результата и сохранением исходящего сообщения до подтверждения Core.
- Учебный набор сценариев и тесты без сети.

Проверка ссылок и структуры не доказывает истинность вывода модели. Реальные CSV и банковские PDF из кейса ещё нужно предоставить и проверить на отложенных примерах. Демо помечено как синтетическое.

## Быстрый запуск из корня репозитория

```sh
docker compose run --build --rm intelligence demo
docker compose run --build --rm intelligence evaluate
docker compose run --build --rm intelligence-test
```

`demo` и `evaluate` по умолчанию работают без модели, ключей и сетевых запросов. Режим `baseline` показывает найденные цитаты и неизвестные данные, но не имитирует рассуждение модели.

Для живого анализа добавьте `OPENROUTER_API_KEY` в корневой `.env`:

```sh
docker compose run --build --rm intelligence analyze \
  --input /app/examples/analysis-request.json --mode model \
  --config /config/providers.json --profile openrouter
```

Исходный шаблон выбирает бесплатную Gemma `:free`. Для разрешённых платных тестов выберите `examples/providers.paid.template.json` через `INTELLIGENCE_CONFIG_FILE` в корневом `.env`: лимит $0.20 в сутки, $0.01 на вызов, цена провайдера ограничена. Самостоятельного переключения между профилями нет. Профиль хакатона включается через `--profile hackathon` после заполнения его тарифов. Образец запроса содержит учебную новость; для банковского материала сначала выполните импорт:

```sh
docker compose run --rm intelligence ingest --input /app/examples/documents.json
```

Свой JSON можно примонтировать read-only: `docker compose run --rm -v /absolute/path/input.json:/input.json:ro intelligence analyze --input /input.json`. Для локального PDF используйте `ingest --input /metadata.json --file /document.pdf` с обоими смонтированными файлами.

## Где что менять

| Файл | Задача |
|---|---|
| `parsers.py`, `corpus.py` | Извлечение текста, версии и поиск |
| `relevance.py` | Отбор и порядок событий |
| `schemas.py` | Формат запроса и кандидата |
| `pipeline.py`, `prompts/` | Шаги анализа и инструкции модели |
| `llm/` | Провайдер, ограничения и расходы |
| `api.py`, `core_client.py`, `worker.py`, `artifacts.py` | Обмен с Core и надёжная доставка результата |
| `cli.py`, `evaluation.py`, `evaluation/` | Запуск и проверка сценариев |

Подробнее: [документы и поиск](docs/corpus.md), [анализ](docs/pipeline.md), [HTTP и worker](docs/runtime.md), [провайдеры и стоимость](../../docs/llm-providers.md), [контракты для Backend](../../contracts/intelligence/README.md).

Отбор событий: `docker compose run --rm intelligence rank --input /app/examples/rank-request.json`. [Метод и ограничения](docs/relevance.md).

[Что проверено и что осталось подтвердить](docs/checks.md).
