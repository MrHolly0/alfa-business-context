# Контрактный стенд Core

`intelligence.core_stand` — локальный сервер для проверки внутреннего API заданий Core. Он не заменяет production Core, не владеет рабочим расписанием и не выдаёт права. Для запуска нужен явный флаг `--fixture-mode`; CLI принимает только loopback-адрес. Стенд не открывает браузер и не обращается к внешней сети.

## Запуск

Из каталога `apps/intelligence`:

```sh
.venv/bin/python -m intelligence core-stand --fixture-mode --state-dir /tmp/alfa-core-stand --port 8765
```

Сервер печатает случайный операторский токен, если оператор не задал `CORE_STAND_OPERATOR_TOKEN`. Не передавайте его за пределы локальной проверки. В тесте сервер можно создать через `create_app(path, fixture_mode=True, operator_token="test")` и проверить с помощью `fastapi.testclient.TestClient`.

Для загрузки примера задания используйте `--seed examples/core-stand-ai-job.json`. Для повторной сборки используйте `--schedule examples/core-stand-ai-task.json --interval 3600`; задание должно иметь `operation=collect`, `max_model_calls=0`, а интервал — не менее 60 секунд. Тот же снимок можно передать через `POST /fixture/schedules` в JSON с полями `schedule_id`, `interval_seconds` и `task`. Стенд создаёт обычные durable `ai-job-v1` jobs. Запланированные задания получают новый request ID, текущее время среза, `trigger=scheduled`, `knowledge_mode=latest`, source IDs из снимка политики и имеющиеся Core proofs. Планировщик стенда не тратит средства на модель.

Пример команды для периодического задания:

```sh
.venv/bin/python -m intelligence core-stand --fixture-mode \
  --state-dir /tmp/alfa-core-scheduled --schedule examples/core-stand-ai-task.json \
  --schedule-id demo-company-collect --interval 3600
```

Первое задание ставится сразу; следующие — с интервалом не менее 60 секунд. Пример содержит пустой список источников, поэтому проверяет только контракт задания, а не сбор новостей.

В другом терминале можно запустить Worker против локального стенда. Подставьте только токен, напечатанный стендом:

```sh
CORE_BASE_URL=http://127.0.0.1:8765 CORE_SERVICE_TOKEN='<fixture token>' \
INTELLIGENCE_TENANTS=demo-tenant .venv/bin/python -m intelligence worker \
  --mode baseline --state-dir /tmp/alfa-core-worker --once
```

Без `--once` Worker продолжает опрашивать задания, пока его не остановят. Не используйте production-токен или реальный tenant.

Операторские fixture маршруты — `POST /fixture/seed`, `GET /fixture/state`, `POST /fixture/jobs/{job_id}/cancel`, `POST /fixture/versions`, `GET /fixture/published-assessments` и `POST /fixture/schedules`. Они требуют loopback и `X-Operator-Token`. Маршруты позволяют создать тестовое задание, посмотреть состояние, отменить задание, изменить тестовые версии, прочитать proofs публикаций и зарегистрировать fixture schedule. Эти маршруты отделены от API, используемого Worker.

## Контракт Worker

`CoreClient` использует следующие маршруты:

- `POST /internal/v1/analysis-jobs/claim` возвращает `{"job": envelope}` или `{"job": null}`.
- `POST /internal/v1/analysis-jobs/{job_id}/heartbeat` принимает `lease_generation` и возвращает подтверждение с новым `lease_until`.
- `POST /internal/v1/analysis-jobs/{job_id}/result` принимает `analysis-result-v1` для старого поля `analysis` или `ai-result-v1` с полем `candidate` формата `ai-data-result-v1`.
- `POST /internal/v1/analysis-jobs/{job_id}/failure` принимает номер поколения и ограниченный код ошибки.

Envelope содержит версии входов, бюджет, scope, deadline, lease и идентификаторы tenant/company/trace. Стенд сверяет версии с заданием и текущими тестовыми версиями, поэтому устаревший результат отклоняется. Повтор того же результата с тем же message ID подтверждается идемпотентно; другой результат после завершения вызывает конфликт.

SQLite стенда хранит состояние заданий, inbox/outbox и опубликованные ревизии. В `ai-data-result-v1` Core увеличивает ревизию только для записей `revisions[]`, где `publication_revision_required: true`. `no_change` не увеличивает ревизию. Для `input_signature` уникальна пара `(tenant, company, event, input_signature)`, поэтому повторная доставка не публикует её второй раз. `CoreStand.published_assessments(tenant, company)` и локальный маршрут `GET /fixture/published-assessments` возвращают подписи и ревизии, которые можно включить в следующий снимок задания. Локальный кеш workflow не является доказательством публикации в Core.

Расписание задаётся через `CoreStand.add_schedule(schedule_id, task, interval_seconds)`. Функция `enqueue_due_schedules()` добавляет не более одного обычного задания на каждое наступившее время и не пытается догонять пропущенные интервалы циклом. Production-расписание остаётся ответственностью Core. Часы стенда можно заменить в тестах для проверки deadline, lease и расписания.
