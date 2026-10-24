# API и сообщения: текущий прототип и проектный контракт

Реализованная Python-сторона, JSON-модели и внутренние маршруты описаны в [intelligence contracts](intelligence/README.md). Ниже — проектный draft пользовательского PHP API и будущей инфраструктуры; эти маршруты не являются запущенным backend. JSON в `examples/` синтетический; `*-draft-1` не является runtime-форматом.

## Текущее состояние

Web-контейнер раздаёт только статический интерфейс через nginx. Node.js используется для сборки и тестов, не как backend runtime. PHP Core пока не подключён; запросы `/api/` получают `503`. Python AI/Data реализует внутренние `POST /internal/v1/analyze`, `POST /internal/v1/search`, `POST /internal/v1/rank`, `POST /internal/v1/sources/probe`, документы/originals и execution inspection. Это внутренние сервисные маршруты, не public user API и не замена Core authorization.

Примеры ниже задают будущий формат обмена, а не работающие маршруты.

## Public user API `/api/v1` — canonical draft

Symfony Core — единственный владелец пользовательских команд и статусов. Эти маршруты стабильны для фронтенда в рамках проектного draft; точные payloads, schemas и ошибки зафиксировать в OpenAPI до реализации. Python FastAPI — internal AI/Data API, не второй user API.

| Method/route | Sync/async | Семантика |
|---|---|---|
| `GET /api/v1/companies/{company_id}/context` | `200` sync | Разрешённая вызывающему версия контекста и unknowns. |
| `GET /api/v1/companies/{company_id}/assessments` | `200` sync | Сохранённые оценки, cursor pagination; не запускает анализ. |
| `GET /api/v1/companies/{company_id}/assessments/{assessment_id}` | `200` sync | Опубликованная revision, типизированные claims, evidence snapshots/refs, coverage и версии входов. |
| `POST /api/v1/companies/{company_id}/assessments` | `202` async | Запросить анализ/пересмотр: `scope`, `requested_period`, `as_of`, expected context/source policy versions. Возврат после commit с `job_id`. |
| `GET /api/v1/jobs/{job_id}` | `200` sync | Сохранённый status/progress/error. Статусы `queued/running/completed/partial/failed/cancelled/superseded`. |
| `POST /api/v1/companies/{company_id}/clarifications` | `201` sync | Создать/ответить/пропустить уточнение; applied answer с provenance создаёт новую context revision. |
| `PUT /api/v1/companies/{company_id}/decisions/{decision_id}` | `200` sync | Записать выбор пользователя/владельца/срок с expected revision; не меняет analysis status автоматически. |

Любое `GET` возвращает сохранённое состояние и не запускает модель. Простое изменение подтверждается новой revision после commit. Analysis `202` значит надёжно принятое задание, не готовый результат. `company_id` из URL — цель проверки прав; tenant и membership приходят из доверенного auth/session context. `tenant_id` в body не определяет scope.

### Public command headers, revisions, errors

- `Idempotency-Key`: одно намерение пользователя, scoped по tenant + principal + operation. Тот же ключ и payload возвращают прежнюю job/response; другое тело под ключом даёт `409 Conflict`.
- `expected_revision`: optimistic concurrency для записи/decision; `context_version`/`source_policy_version` фиксируют snapshot анализа. Несовпадение записи — `409`; устаревший async candidate становится `superseded`.
- `request_id`/`trace_id` — корреляция попыток, не дедупликация.
- `400` некорректное тело, `401` нет сессии, `403`/политикой унифицированный `404` нет права, `404` не найдено внутри scope, `409` idempotency/revision conflict, `429` quota, `5xx` серверная ошибка. Не раскрывать наличие чужого company ID. Автоповтор только для безопасной команды с тем же ключом.

## Internal Symfony ↔ Python analysis job — draft

На стр. 16 PDF требует OpenAPI + JSON Schema с `schema_version`, `message_id`, `job_id`, `company_id`, `trace_id`, `input_versions`, `lease_generation`, `deadline`, `result_ref`. Ниже — предложенные командой внутренние маршруты; PDF их имена не задаёт. Core выдаёт lease через API, Python не читает таблицу jobs.

- Claim: Core атомарно резервирует due job, возвращает минимальный scoped snapshot, versions, generation/deadline/budget.
- Heartbeat: renew только актуальный lease/generation; отменённая/утёкшая lease отвергается.
- Result: идемпотентный candidate с input versions, claims/evidence refs, coverage, partial/unknown, model/prompt/pipeline versions и usage. Core проверяет tenant scope, отмену, lease generation, версии, schema и refs перед публикацией.

| Метод и маршрут | Назначение и ответы (draft) |
|---|---|
| `POST /internal/v1/analysis-jobs/claim` | Core выдаёт due job с lease/generation; `200` — job выдана, `204` — очередь пуста, `403` — нет service scope. |
| `POST /internal/v1/analysis-jobs/{job_id}/heartbeat` | Продлить только текущий lease; `200` — обновлён, `409` — lease истёк/закрыт либо generation не совпадает, `403` — нет scope. |
| `POST /internal/v1/analysis-jobs/{job_id}/result` | Передать candidate; `200` — принят или повторён прежний ack. Сначала проверяются service identity и tenant/job scope по доверенной идентичности и принадлежащей ей job (`403` при отказе); только затем ищется прежний ack по `message_id` и payload. Совпадающий повтор возвращает прежний ack даже после закрытия lease. Если ack не найден, проверяются состояние lease и generation: тот же `message_id` с другим payload или новый message от worker со старым/чужим `lease_generation` даёт `409`. |

Пример message body — [`analysis-job.json`](examples/analysis-job.json); он черновой и не валидируется runtime. Public `job_id` и внутренние `message_id`/`trace_id` — разные ID: message dedupe не заменяет user `Idempotency-Key`.

## Владение, outbox, Messenger и SSE

Core владеет пользовательскими/API-записями, jobs/idempotency/outbox/inbox, опубликованными оценками, снимками evidence, read models и журналом уведомлений. AI владеет ревизиями исходных документов, извлечениями, evidence-фрагментами и индексом. Source Policy принадлежит Core. Допустимы отдельные схемы и роли PostgreSQL в одном кластере; межсервисных SQL joins/FKs/записи нет. Object refs доступны только сервисной идентичности; Core сохраняет snapshot/reference evidence для опубликованной revision.

Команда и outbox пишутся в одной транзакции. PHP Messenger/Doctrine transport принимает короткие project/outbox/notification jobs; это не runtime долгого Python analysis. У каждой job category один retry owner. Для Python jobs Core владеет attempts/run_after/lease/fencing. Full jitter не совпадает с Messenger built-in jitter; Symfony 7.4 Messenger docs указывают `RecoverableMessageHandlingException` как исключение, обходящее `max_retries`. Три попытки суммарно — `max_retries=2`; для требуемого full jitter нужен кастомный `RetryStrategyInterface` [Symfony Messenger docs](https://symfony.com/doc/7.4/messenger.html).

SSE публикует Mercure или согласованный bank hub после обновления read projection. Hub внешний к PHP-FPM. Он передаёт минимум: event ID/cursor, type, object ID, revision; готовое состояние UI читает REST. Private topics, topic-scoped короткий доступ, snapshot/replay, проверка отзыва. Polling — резерв. Kafka — transport adapter только по платформенному контракту, не замена Core job lifecycle.
