# Модель данных и владение

Это целевая модель из product v3, а не схема текущей базы. Сегодня Python сохраняет локальные corpus/workflow/source polling данные в SQLite и raw blobs в private state; production Core/PostgreSQL и PHP user API в Compose не подключены. Core и AI могут использовать один PostgreSQL-кластер с раздельными schemas/roles, но данные принадлежат одному приложению. Межсервисных SQL joins/FKs и записи в чужие таблицы нет. Идентификаторы между сервисами — opaque strings; каждый JSON/API contract несёт версии и scope. Core остаётся источником истины по доступу, user actions, jobs и опубликованным оценкам.

## Core schema — Symfony владеет

| Сущность | Основные поля | Назначение |
|---|---|---|
| `tenant` / `membership` | `tenant_id`, `principal_id`, `role`, `permissions`, `valid_from/to` | Scope организации и членство из согласованного auth adapter. Не принимать tenant из запроса как полномочие. |
| `company` | `tenant_id`, `company_id`, `display_name`, allowed `external_ref` | Компания в tenant. Составные уникальности защищают от случайных cross-tenant ссылок. |
| `company_context_revision` | `tenant_id`, `company_id`, `revision`, `created_at/by`, `reason`, `schema_version` | Immutable snapshot контекста компании. |
| `context_fact` | `fact_id`, `revision`, `key`, `value`, `unit`, `valid_from/to`, `status`, `provenance_ref` | Факт с происхождением; `confirmed`, `reported`, `unknown`, `assumption` различаются. Пусто не значит «нет». |
| `dependency_edge` | `edge_id`, `revision`, `from_ref`, `relation`, `to_ref`, `status`, `valid_from/to`, `evidence_ref` | Зависимости компании и их provenance/status. Платёж и GROUP_ID сами по себе не подтверждают товарную зависимость. |
| `source_policy_revision` | `tenant_id`, `company_id?`, `revision`, `allowed_sources`, `exclusions`, `created_at` | правила Core разрешённого поиска, исключений и свежести. Python получает только отфильтрованный policy/context snapshot. |
| `situation` | `tenant_id`, `company_id`, `situation_id`, `event_refs[]`, `created_at` | Долгоживущий бизнес-вопрос; связывает assessment revisions и историю. |
| `assessment_revision` | `tenant_id`, `company_id`, `situation_id`, `revision`, `analysis_status`, `summary`, `horizon`, `coverage`, `context_version`, `source_policy_version`, `event_versions`, `pipeline_version`, `model_id`, `prompt_version`, `created_at` | Опубликованный Core результат и воспроизводимые версии входов. Кандидат Python не становится публикацией автоматически. |
| `assessment_claim` | `assessment_revision`, `claim_id`, `text`, `kind`, `evidence_refs[]`, `assumptions[]`, `origin?` | Единый enum `external_fact`, `bank_view`, `model_hypothesis`, `recommendation`, `unknown`. Ненайденная позиция банка — `unknown` с `origin: bank`, не `bank_view`. |
| `evidence_snapshot` / `evidence_ref` | `evidence_ref_id`, `assessment_revision`, `ai_document_ref`, `document_revision`, `locator`, `quote_snapshot/hash`, `source_role`, `relation` | Core materializes/retains минимальный fragment snapshot/reference для опубликованной оценки. Сырой документ и полное извлечение принадлежат AI; ссылки выдаются через service authorization. |
| `user_action` / `decision_record` | `tenant_id`, `company_id`, `situation_id`, `principal_id`, `status`, `owner`, `due_at`, `note`, `basis_revision` | Выбор человека, вопрос и условия наблюдения. Отдельно от `analysis_status`. |
| `job` | `job_id`, `tenant_id`, `company_id`, `kind`, `idempotency_key`, `payload_hash`, `input_versions`, `status`, `attempt`, `run_after`, `lease_until`, `lease_generation`, `heartbeat_at`, `deadline`, `result_ref` | Единственная система Core для выполнения и повторов долгих Python Analysis Jobs. |
| `job_step` | `job_id`, `step_id`, `status`, `artifact_ref`, `usage`, `error_class`, `started_at`, `finished_at` | Состояние этапов/артефактов; длинный вызов модели не держит DB lock. |
| `outbox` / `inbox` | `event_id`, `tenant_id`, `event_type`, `aggregate_revision`, `payload_ref`, `created_at` / `consumer` | Outbox атомарен с Core write; inbox дедуплицирует внешнее/повторное сообщение при применении результата. |
| `read_projection` / `notification_journal` | `tenant_id`, `company_id`, `object_revision`, `projection_version`, `event_id/cursor`, `created_at` | Версионированные UI views и SSE replay journal. Источник можно пересобрать из состояния Core. |
| `audit_record` | `tenant_id`, `actor`, `action`, `object_ref`, `request_id`, `before/after_revision`, `at` | Кто и каким правом изменил объект; не полный Event Sourcing. |

## AI schema — Python владеет

| Сущность | Основные поля | Назначение |
|---|---|---|
| `source_registry_ref` | `source_id`, `owner`, `kind`, `access_method`, `license/poll policy ref`, `status` | Полученные материалы привязаны к Core Source Policy; право выдачи из Core policy, не AI registry. |
| `document` | `document_id`, `source_id`, `canonical_uri`, `publisher`, `author`, `published_at`, `event_time`, `fetched_at`, `content_hash` | Идентичность источника, принадлежащего AI. |
| `document_revision` | `document_id`, `revision`, `raw_object_ref`, `normalized_text_ref`, `language`, `parse_status`, `parser_version`, `page_coverage` | Immutable оригинал/извлечение, OCR/parser status и coverage. |
| `extracted_claim` | `claim_id`, `document_revision`, `claim_type`, `text`, `author`, `valid_time`, `stance`, `status` | Извлечённое сообщение/наблюдение/мнение/прогноз/гипотеза. Банковская позиция не сливается с внешним фактом или модельным выводом. |
| `evidence_span` | `evidence_id`, `document_revision`, `claim_id?`, `locator`, `quote/hash` | Фрагмент с точной page/paragraph/offset. Полный источник подчиняется лицензии/retention. |
| `event_artifact` | `event_id`, `revision`, `document_refs[]`, `event_time`, `summary`, `cluster_version`, `status` | Производный event cluster/analysis package, не опубликованный user assessment. Перепечатки не считаются независимыми подтверждениями. |
| `search_index` | `document_revision`, `evidence_id`, filters, index_version | Производная копия, принадлежащая AI. ACL/source/time filters применяются до выдачи. Rebuild возможен из разрешённых оригиналов. |

Core хранит только устойчивые opaque AI refs и материализованный минимальный evidence snapshot для опубликованной revision. Сетевые ссылки/объектные refs требуют service identity и повторного Core permission check. Python не создаёт и не меняет Core `assessment_revision` напрямую.

## Версии, сообщения и инварианты

Публичная команда должна создавать Core job с `context_version`, `source_policy_version`, `event_versions`, deadline/budget и `lease_generation`. Реализованный Python контракт `ai-data-task-v1` передаёт tenant/company, `context_version`, immutable `SourcePolicySnapshot`, operation, time mode, budget/scope и опубликованные assessment proofs; `ai-data-result-v1` возвращает кандидатов и revisions. Python читает переданный snapshot, а не Core DB. Stand позволяет локально проверять claim/heartbeat/result в fixture-only режиме; это не production Core. При интеграции Core проверяет current membership, source policy, revisions, cancel flag и generation перед публикацией. Устаревший candidate должен стать `superseded`; новый вход при необходимости coalesces в следующую job.

`Idempotency-Key` уникален по scope tenant + principal + operation. Тот же ключ с тем же `payload_hash` возвращает прежнюю job; другой payload — `409 Conflict`. `request_id`/`trace_id` не дедуплицируют действие. At-least-once delivery допустима; Core outbox и inbox/idempotent consumers гарантируют однократное применение эффекта внутри Core. PHP Messenger обрабатывает короткие проекции/outbox/notifications; Python retries долгой job планирует Core `run_after`.

## Источники и данные кейса

Политика и право на источник принадлежат Core Source Policy; документы/extractions/index — AI. В брифе CSV поля описаны как обезличенные, но фактические файлы, имя payment link и единицы `REVENUE_COMPANY` требуют сверки до импорта. Не выводить реальную личность, кредитный договор, денежный эффект или товарную зависимость из обезличенного ID, GROUP_ID либо перевода. Наличие seed в legacy README не доказывает доступ к исходному CSV.
