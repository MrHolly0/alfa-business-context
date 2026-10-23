# Запуск и работа

Основной способ запуска и проверки — [Docker](../infra/README.md).

## Python без Docker

Нужен Python 3.12+. Из корня:

```sh
PYTHONPATH=apps/intelligence python3 -m intelligence.llm
PYTHONPATH=apps/intelligence python3 -m unittest discover -s apps/intelligence/tests -v
```

## Сборка интерфейса без Docker

Нужен Node.js из `.nvmrc`:

```sh
npm ci --prefix apps/web
npm --prefix apps/web test
npm --prefix apps/web run build
npm --prefix apps/web run validate
```

Результат — статические файлы `apps/web/dist`. Для просмотра используйте Docker nginx.

## Передача работы

Наша реализация — `apps/intelligence`. Форматы заданий и результатов согласуем с Backend через `contracts`; PHP-код он пишет в своей зоне. Frontend подключает экран к тому же формату.

Перед передачей приложите пример входа и выхода, команду запуска и результат проверок. Если изменился формат, одновременно обновите описание и пример в `contracts`. Порядок задач — в [плане](backlog.md).
