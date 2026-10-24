# Контракты Python AI/Data

Эти схемы описывают границу между PHP Core и внутренним Python-сервисом. Python-контракт реализован; production Symfony Core и пользовательский путь через него пока не подключены. Core владеет пользователями, правами, профилем компании, Source Policy, очередью, отменой и публикацией. Python получает проверенный snapshot, сохраняет AI/Data артефакты и возвращает кандидата; он не пишет бизнес-таблицы Core.

## Внутренний API

Маршруты закрыты `Authorization: Bearer <INTELLIGENCE_SERVICE_TOKEN>` и tenant allowlist `INTELLIGENCE_TENANTS`. Сервисный bearer не заменяет пользовательскую сессию: Backend сначала проверяет пользователя/организацию и формирует разрешённый scope.

| Метод и маршрут | Контракт |
|---|---|
| `POST /internal/v1/documents` | [`DocumentInput`](document.schema.json): доверенная ручная подача материала с датами, ролью источника и классификацией |
| `POST /internal/v1/search` | [`SearchInput`](search.schema.json): поиск по tenant/source/as-of и, если передана, Source Policy с фильтром исключений до выдачи |
| `POST /internal/v1/rank` | [`RankRequest`](rank-request.schema.json) → [`RankResult`](rank-result.schema.json): объяснимый предварительный порядок событий |
| `POST /internal/v1/analyze` | Legacy [`AnalysisRequest`](analysis-request.schema.json) → [`CandidateAssessment`](candidate-assessment.schema.json), либо [`DataTask`](ai-data-task.schema.json) → [`DataResult`](ai-data-result.schema.json) |
| `POST /internal/v1/company-context/prepare` | [`CompanyContextPrepareRequest`](company-context-prepare.schema.json) → [`CompanyContextPrepareResult`](company-context-proposal.schema.json); только предложение, не запись Core-профиля |
| `POST /internal/v1/sources/probe` | [`SourceProbeRequest`](source-probe-request.schema.json) → [`SourceProbeResult`](source-probe-result.schema.json); разовая URL-проверка по Core grant, без подписки |
| `POST /internal/v1/originals` | [`OriginalInput`](original-request.schema.json): разрешённое чтение оригинала по tenant, revision и текущей policy |
| `GET /internal/v1/executions/{request_id}?tenant_id=...` | Диагностические этапы локального workflow; не бизнес-статус job и не пользовательский progress API |

Ошибки приватных маршрутов: `401` — сервисная аутентификация отсутствует/неверна; `403` — tenant или policy scope запрещён; `409` — idempotency conflict/запрос уже исполняется; `413` — превышен размер тела; `422` — невалидный контракт. Тела ошибок не содержат внутренних stack traces.

`/internal/v1/search` и `rank` работают по уже разрешённому корпусу/входным событиям. Они не являются web search и не управляют списком пользовательских изданий. Для URL Core выдаёт одноразовый probe grant, проверяет права владельца, а после подтверждения сохраняет source grant в новой revision Source Policy.

## DataTask и результат

`ai-data-task-v1` содержит tenant/company/request IDs, `context_version`, `source_policy_version`, `as_of`, operation (`collect`, `analyze`, `reassess`, `period`, `onboard`), immutable `SourcePolicySnapshot`, до 20 бизнес-контекстов, `published_assessments` Core, бюджет и ограничения. `source_policy.tenant_id/revision` обязаны совпасть с task. Классификация источника берётся из Core-owned `SourceGrant`, а не из текста материала или task label.

`ai-data-result-v1` содержит collection report, event/package refs, selections/invalidations, candidates по компаниям, revisions, analyst configurations, period/watch результаты, coverage и trace. Кандидат не равен опубликованной ситуации. Core проверяет актуальность versions, lease/generation, cancellation и policy перед публикацией.

Core прикладывает `published_assessments` с точной input signature и assessment revision. Локальный кеш Python не доказывает, что Core принял прошлый ответ. При наличии точного proof workflow может вернуть `no_change`; без proof он возвращает candidate с `publication_revision_required=true`, чтобы Core не потерял первую публикацию после неопределённого ACK. Core отдельно дедуплицирует signature.

## Долгая работа: Core владеет очередью

Worker обращается к Core через четыре маршрута:

| Маршрут | Назначение |
|---|---|
| `POST /internal/v1/analysis-jobs/claim` | Получить job или пустой ответ |
| `POST /internal/v1/analysis-jobs/{job_id}/heartbeat` | Продлить lease той же generation |
| `POST /internal/v1/analysis-jobs/{job_id}/result` | Передать `ai-result-v1` и получить ACK с исходным `message_id` |
| `POST /internal/v1/analysis-jobs/{job_id}/failure` | Передать ограниченный машинный код отказа |

Существующий [`analysis-job-v1`](analysis-job.schema.json) поддержан для legacy `analysis`. Новый [`ai-job-v1`](ai-job.schema.json) несёт DataTask и scope. Worker проверяет tenant, deadline, budget, `input_versions` и `lease_generation`; потерянный lease запрещает дальнейшую отправку результата. Точный payload хранится до подтверждения Core, поэтому повтор ACK использует те же ID и содержимое.

Fixture stand (`python -m intelligence core-stand --fixture-mode`) исполняет эти маршруты на loopback и хранит тестовые jobs/schedules в SQLite. Он нужен для контрактных тестов worker, но не предоставляет production PHP-права, SSO или пользовательский API.

## Схемы данных и генерация

| Схемы | Назначение |
|---|---|
| [`SourcePolicySnapshot`](source-policy.schema.json), [`SourceProbeRequest`](source-probe-request.schema.json), [`SourceProbeResult`](source-probe-result.schema.json) | Core grants, исключения, probe preview |
| [`DocumentInput`](document.schema.json), [`SearchInput`](search.schema.json), [`OriginalInput`](original-request.schema.json) | Corpus ingest/search/original read |
| [`ImpactPackage`](impact-package.schema.json), [`PeriodResearchReport`](period-research.schema.json) | Переиспользуемый разбор события, временной ряд/coverage |
| [`WatchCondition`](watch-condition.schema.json), [`WatchEvaluation`](watch-evaluation.schema.json) | Условия наблюдения и оценка наблюдений |
| [`CompanyContextPrepareRequest`](company-context-prepare.schema.json), [`CompanyContextPrepareResult`](company-context-proposal.schema.json) | CSV нормализация обезличенных данных без CRUD Core |
| [`AnalystConfig`](analyst-config.schema.json) | Закрытые настраиваемые предпочтения поверх фиксированных правил |
| [`ModelAnswer`](model-answer.schema.json) | Узкий формат вывода модели, проверяемый Python |

Схемы и OpenAPI экспортируются из моделей Python. Из `apps/intelligence` выполните:

```sh
.venv/bin/python scripts/export_contracts.py --check
```

Чтобы пересоздать экспорт после согласованного изменения модели, запустите ту же команду без `--check`; получившийся diff должен содержать совместимую версию/API и тесты. Актуальный процесс подключения описан в [ai-integration.md](../../docs/ai-integration.md); готовность и внешние зависимости — в [матрице](../../docs/ai-scope-and-readiness.md) и [живом отчёте](../../docs/ai-live-checks.md).
