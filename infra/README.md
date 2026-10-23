# Docker

Все команды выполняются из корня репозитория. Нужны Docker и Docker Compose.

## Веб-интерфейс

```sh
docker compose up --build --wait
```

Открыть http://localhost:4173. В контейнере nginx раздаёт готовые файлы React; Node нужен только при сборке. PHP-ядро в этот запуск не входит — его создаёт Backend. До подключения Core API интерфейс использует демонстрационные данные, сетевые функции отключены.

Остановка: `docker compose down`. Порт можно изменить через `WEB_PORT` в `.env`.

## Наша Python-часть

Пример без сети и ключей:

```sh
docker compose run --build --rm intelligence
```

Тесты без сетевого доступа:

```sh
docker compose run --build --rm intelligence-test
docker compose run --build --rm web-test
```

## Модели и расходы

По умолчанию конфиг берётся из `apps/intelligence/examples/providers.template.json` и доступен в контейнере как `/config/providers.json`. Свой файл можно выбрать переменной `INTELLIGENCE_CONFIG_FILE` в `.env`. Ключи `OPENROUTER_API_KEY` и `HACKATHON_API_KEY` передаются только Python-контейнеру при запуске.

Реальный запрос выполняется только явной командой `run`; [пример и тарифы](../docs/llm-providers.md). Обычный запуск и тесты модель не вызывают.

Журнал расходов хранится в volume `alfa-business-context-intelligence-state` и переживает удаление контейнера. Не используйте `docker compose down -v` для обычной остановки: это удаляет данные volume. Хостовый Python и контейнер используют разные журналы.

## Добавление PHP

Backend подключит собственное приложение и заменит ответ `503` для `/api/` в `apps/web/nginx.conf` на проксирование в PHP Core. Реализация ядра остаётся в его зоне.
