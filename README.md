# Бизнес-контекст

Сервис помогает руководителю понять, какие новости важны для его компании и почему.

## Кто где работает

| Часть | Каталог | Ответственный |
|---|---|---|
| Сценарий и ожидаемые ответы | `docs/product` | БА + промпты |
| React-интерфейс | `apps/web/src` | Frontend |
| PHP-ядро и API | `apps/core` | Backend |
| Поиск, анализ и модели | `apps/intelligence` | ML |

Наша текущая реализация — Python-компонент. PHP-ядро создаёт Backend: сейчас в `apps/core` только описание. Веб-приложение содержит интерфейс и демонстрационные данные. Node используется для его сборки и тестов, готовые файлы раздаёт nginx. Полный путь через PHP и Python ещё нужно соединить.

## Запуск через Docker

Веб-интерфейс на http://localhost:4173:

```sh
docker compose up --build --wait
```

Python-пример без ключей и вызовов моделей:

```sh
docker compose run --build --rm intelligence
```

Проверки:

```sh
docker compose run --build --rm web-test
docker compose run --build --rm intelligence-test
```

Остановка: `docker compose down`. Журнал расходов Python остаётся в отдельном Docker volume.

## С чего начать

- [Ближайшие задачи](docs/backlog.md) и [работа вчетвером](docs/team.md).
- [Устройство проекта](docs/architecture.md).
- [Docker и конфигурация](infra/README.md).
- [Модели, ключи и расходы](docs/llm-providers.md).
