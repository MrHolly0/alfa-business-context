# Python AI/Data: адаптеры провайдеров реализованы; MVP service планируется

В `intelligence.llm` реализованы Python 3.12+ OpenAI-compatible адаптеры OpenRouter и двух допустимых адресов хакатонного шлюза, проверка шести моделей, локальный SQLite учёт бюджета и offline-first CLI. Это ещё не FastAPI worker, агентный pipeline, claim/result API или банковская интеграция. CLI по умолчанию показывает синтетический offline fixture; сеть вызывается только явной командой `run`.

Из `apps/intelligence` установите Python 3.12+ и запустите offline demo без ключей и сетевых запросов:

```sh
python -m intelligence.llm
```

Пакет использует только стандартную библиотеку. Для CLI из корня репозитория укажите путь пакета:

```sh
PYTHONPATH=apps/intelligence python -m intelligence.llm
```

Настройте свой JSON на основе [`examples/providers.template.json`](examples/providers.template.json): шаблон содержит тарифы `0` только для Gemma `:free` на OpenRouter, а тарифы хакатонного профиля оставляет `null`. Задайте фактические тарифы хакатона до его live вызова; при выборе платной OpenRouter модели тоже укажите её фактические тарифы. Выберите один из шести разрешённых точных API model ID и экспортируйте имя ключа из `api_key_env` в environment своего shell. Поддерживаемые model ID зафиксированы и проверяются при загрузке профиля; фактическая доступность модели, тарифы и usage API шлюза требуют подтверждения. JSON профиль с `null` тарифами не проходит validation. Команда `validate` читает конфиг, но не ключ; `budget` показывает локальную SQLite запись; только `run` отправляет запрос. Укажите UTF-8 prompt через `--input file.txt` или stdin. Подробно: [`docs/llm-providers.md`](../../docs/llm-providers.md).

Для smoke и unit tests (только offline):

```sh
PYTHONPATH=apps/intelligence python -m unittest discover -s apps/intelligence/tests -v
```

## Целевой сервис (план)

Полный целевой Python/FastAPI сервис включает пулы ingest, monitoring, research и experiments по v3 pp.12–19. Текущий продуктовый прототип использует Node Worker/JavaScript; Python provider adapter не означает, что service runtime уже создан.

## Граница и владение

Python AI/Data владеет AI schema: полученными документами, document revisions, parser/extraction versions, claims, evidence spans, промежуточными аналитическими артефактами и поисковым индексом. Оригиналы/артефакты могут лежать в согласованном object storage. Symfony Core владеет жизненным циклом jobs, решениями по tenant/membership, Source Policy, опубликованными оценками и действиями пользователей. Допустим один кластер PostgreSQL с отдельными схемами и ролями БД; Python не читает и не меняет таблицы jobs и бизнес-данных Core. Между сервисами нет SQL joins/FKs: только непрозрачные ID, версионируемый JSON API и ссылки, доступные авторизованным сервисам. Для evidence, которое надо показывать после публикации, Core сохраняет ограниченный снимок/ссылку с точной версией и locator; внешний raw object остаётся у AI и читается через авторизованный service endpoint.

## Внутренний job contract

Symfony Core создаёт/владеет job и выдаёт её Python по внутреннему claim API. Python не выбирает tenant из payload документа и не использует прямой доступ к Core schema. Вход: `schema_version`, `message_id`, `job_id`, `company_id`, `trace_id`, context/source policy/event/document versions, allowed source/evidence refs, `lease_generation`, deadline, budget, purpose/scope. Сервисная идентичность и tenant scope подтверждаются Core; mTLS/scoped service tokens выбираются с платформой.

Набросок route names — командный draft, не contract из PDF: `POST /internal/v1/analysis-jobs/claim`, `POST /internal/v1/analysis-jobs/{job_id}/heartbeat`, `POST /internal/v1/analysis-jobs/{job_id}/result`. JSON payload пример — [`analysis-job.json`](../../contracts/examples/analysis-job.json). Фиксировать body/errors/response в OpenAPI + JSON Schema до параллельной реализации. Python не парсит PHP-serialized Messenger envelope и не принимает произвольный SQL/URL.

Core выдаёт lease generation, deadline и reproducible input versions; worker сохраняет этапы и периодически heartbeat. Отмена и потеря lease проверяются между шагами. Результат передаётся идемпотентно с `job_id`, generation, `input_versions`, `result_ref` и usage. Core проверяет scope/schema/versions/cancellation/uniqueness и публикует revision. Истёкший worker не может опубликовать результат; его артефакты могут остаться непубличными для диагностики по retention policy.

## Pipeline и evidence

1. **Ingest:** читать только разрешённые API/RSS/webhook/импорты; фиксировать source, publisher, автор, `published_at`, `event_time`, `fetched_at`, canonical URI, hash и policy version. Ограничивать тип/объём/redirect/DNS/timeouts; текст источника недоверенный, не инструкция.
2. **Parse/version:** документное содержание и извлечение версионируются отдельно; сохранять parser version, язык, ошибки/пропуски страниц и coverage. Исправление или новое содержание не переписывает предыдущую ревизию.
3. **Dedup/event:** точные дубли по URL/hash, перепечатки связать с первоисточником, похожие темы автоматически не склеивать.
4. **Claims/retrieval:** claims отделяют external fact, bank position, model hypothesis, recommendation и unknown. Сохранять авторство/valid time/фрагмент/противоречия. Источники, bank materials и company context находить отдельными категориями. ACL/source policy/as_of применяются до отдачи evidence. Ненайденная банковская позиция — `unknown` с `origin: bank`, не вывод от имени банка.
5. **Impact:** общий event package переиспользуется, Core context/dependencies дают адресный fan-out. Каждое ребро графа имеет provenance/status/date; платёж или industry match сам по себе не доказывает причинную связь. Ограниченная глубина, alternatives и falsifiers.
6. **Candidate:** выход структурирован, с точными versioned evidence refs/snippets, типами claim, versions, assumptions, coverage, alternatives/falsifiers, horizon и usage. Недостаток/противоречие даёт `partial`/`unknown`; не заявлять уверенный вывод по отсутствию результата.

Canonical claim enum: `external_fact`, `bank_view`, `model_hypothesis`, `recommendation`, `unknown`. Банк-прогноз сохраняет тип/автора/дату, не становится установленным внешним фактом.

## Agent plan/tools/context/check/adapt

Агентная логика — детерминированная команда state machine, не готовая автономная система. Planner формирует JSON план из допустимых этапов и ограничивает шаги/глубину/time/token budget. Tools: чтение выданного профиля и документных фрагментов, lexical retrieval (Qdrant/vector только при отдельном разрешении), ограниченный dependency traversal, code calculations, validation. Модель не получает произвольные сеть/SQL/shell/секреты и не может менять контекст или выполнять действия от лица клиента.

Межшаговый context хранит `context_version`, `source_policy_version`, документные revisions, prior step artifacts, evidence refs, unknowns и budget. Check сверяет цитаты/числа с оригиналами, schema/IDs, временную применимость, альтернативы, противоречия, falsifier и покрытие bank materials. Adapt может выполнить ограниченный повторный retrieval/extraction или вернуть вопрос пользователю; один schema repair и общий retry budget задаются кодом. Самооценка модели не выдаётся как вероятность.

Разрешены только модели из брифа (Gemma 4 26B A4B, GLM-5.3-Flash, Qwen3.6 35B A3B, DeepSeek-V4.1-Flash, Qwen3-VL-30B-A3B-Instruct, Kimi-K2.6); точные API ID и allowlist зафиксированы в адаптере. Доступность в выданном аккаунте, тарифы и наличие usage API надо подтвердить у оператора шлюза. Общий лимит брифа — 500 ₽/сутки. Резервировать бюджет до вызова, записывать usage после, ограничивать in-flight/attempts, прекратить optional deep path при исчерпании. Таймаут не доказывает, что провайдер не выполнил или не тарифицировал запрос.

## Очереди и поиск

Core Analysis Jobs владеет `run_after`, attempt, lease, heartbeat, cancellation и fencing. Python workers — executors отдельных классов; retry получает новую попытку только от Core, без скрытой лестницы SDK retries. Нет Celery параллельно для тех же jobs. Messenger — короткие PHP-проекции/outbox/notifications, не Python очередь.

Для стенда v3 указывает lexical search. Qdrant/dense+sparse/RRF/reranker — условное расширение продукта/pilot: сначала подтвердить лицензии и допуск embeddings, затем сравнить с lexical baseline, ACL/temporal filters и стоимостью; не объявлять включённым в хакатонный стенд. Kafka транспортируется через adapter только при банковском контракте. Redis не хранит права/job truth. Metrics: freshness/gaps/parse coverage, recall важных новостей на holdout, точность evidence, false influence links, partial rate, p95 queue/steps, retries, token cost и model errors.
