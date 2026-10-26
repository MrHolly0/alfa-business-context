# Подключение AI/Data-модуля

Это руководство описывает работающий Python-модуль и текущую интеграцию с Symfony/PHP Core. Core уже обслуживает web API и ограниченный прямой новостной вызов Python, но полноценный Core-owned job lifecycle (enqueue/claim/heartbeat/result/failure, lease fencing, отмена и UI progress) ещё не реализован. Банковские источники и эксплуатационная среда не подключены. Для разработки также полезно [как работает AI/Data](ai-process.md), [контракты Core](../contracts/intelligence/README.md) и [сбор источников](../apps/intelligence/docs/collection.md).

## Границы владения

Symfony Core сейчас отвечает за локальных пользователей, company memberships, доступ, версии контекста, метрики, news sources/runs/events/assessments и UI API. В прямом `run-news.php` он строит разрешённый снимок и вызывает Python API. Общая очередь analysis jobs, отмена и выдача заданий worker пока не реализованы.

Python выполняет сбор только по этому снимку, хранит версии документов и evidence, формирует события и общий пакет воздействия, ищет допустимые основания, запускает персональный анализ/исследование и возвращает кандидата с версиями и объяснениями. Python не создаёт аккаунты или разрешения, не читает PHP-таблицы и не публикует напрямую пользовательскую ситуацию.

Frontend обращается к PHP по cookie-сессии. Внутренний bearer Python не является пользовательской сессией и хранится вне браузера. Текущий прямой news runner формирует ограниченную задачу, а UI читает сохранённые news records; UI не управляет Python job и не показывает общий job progress.

## Быстрый запуск без ключей

Из корня репозитория можно проверить CLI и тесты в Docker без модельного вызова:

```sh
docker compose --env-file /dev/null run --build --rm intelligence demo
docker compose --env-file /dev/null run --build --rm intelligence evaluate --mode baseline
docker compose --env-file /dev/null run --build --rm intelligence-test
```

На хосте сначала установите окружение по [инструкции разработки](development.md), затем из `apps/intelligence`:

```sh
.venv/bin/python -m intelligence demo
.venv/bin/python -m pytest -q -p no:cacheprovider tests
```

Для локального сквозного AI/Data-примера используйте `demo-data`:

```sh
.venv/bin/python -m intelligence demo-data --state-dir /tmp/alfa-data-demo
```

Он читает RSS и учебную bank-role заметку из `examples/feeds/`, проходит Collector → Corpus → events → две компании/графа → baseline candidates и выводит JSON с количеством событий, компаний, кандидатов и refs конфигураций. HTTP/DNS и ключи модели не используются. Если `--state-dir` опущен, хранилище временное: ссылки на оригиналы в результате перестанут работать после завершения процесса. Для сохранения raw bytes передайте приватный state path.

`demo`, `evaluate --mode baseline` и `run-task --mode baseline` не требуют ключа модели. Для локального анализа DataTask из корня репозитория подготовьте JSON со снимком `ai-data-task-v1` и без внешних источников, затем выполните:

```sh
cd apps/intelligence
.venv/bin/python -m intelligence run-task --mode baseline \
  --input /path/to/data-task.json --state-dir /tmp/ai-data-state \
  --output /tmp/data-result.json
```

Этот вызов исполняет задачу локально и минует Core job lifecycle; он удобен для проверки контракта, но не является production-очередью. В baseline фактические модельные вызовы не происходят, а вывод может иметь статус `partial`, `unknown` или `no_change`.

Контрактный Core stand запускается только явно в fixture mode на loopback. Пример ниже использует пустой демонстрационный снимок и не обращается к новостным сайтам:

```sh
cd apps/intelligence
.venv/bin/python -m intelligence core-stand --fixture-mode \
  --state-dir /tmp/alfa-core-stand --seed examples/core-stand-ai-job.json
```

Для периодической фикстуры добавьте `--schedule examples/core-stand-ai-task.json --schedule-id demo-collect --interval 3600`; минимальный интервал — 60 секунд. Stand сам преобразует due-запись в обычный `ai-job-v1`, а worker работает с ним через те же claim/heartbeat/result. Этот расписатель проверяет контракт и не заменяет Symfony scheduler.

Чтобы запустить внутренний API в baseline, сначала задайте локальные тестовые `INTELLIGENCE_SERVICE_TOKEN` и `INTELLIGENCE_TENANTS`, затем:

```sh
.venv/bin/python -m intelligence serve --mode baseline --host 127.0.0.1 --port 8081 \
  --state-dir /tmp/ai-api-state
```

Приватные API требуют `Authorization: Bearer <token>` и tenant из `INTELLIGENCE_TENANTS`. В контейнере для стенда можно использовать `docker compose --env-file /dev/null --profile analysis up --build intelligence-api`, передав эти две переменные в окружение процесса Compose. Без конфигурации токена и tenant API намеренно закрыт. Docker Compose задаёт loopback-публикацию порта; реальную конфигурацию окружения обслуживает оператор, не frontend.

## Два API-режима

`POST /internal/v1/analyze` принимает либо legacy `AnalysisRequest` (`analysis-request-v1`), либо `DataTask` (`ai-data-task-v1`) и возвращает соответственно `CandidateAssessment` или `DataResult`. Это синхронная внутренняя точка для короткой проверки/интеграции; вход с одинаковыми tenant и request ID идемпотентен, другое тело под тем же ID конфликтует.

Для регулярной и долгой работы предпочтителен Core job worker. Существующие маршруты сохраняются:

| Метод и маршрут | Действие |
|---|---|
| `POST /internal/v1/analysis-jobs/claim` | Worker забирает одно задание или получает пустой ответ. |
| `POST /internal/v1/analysis-jobs/{job_id}/heartbeat` | Продлевает текущий lease для той же `lease_generation`. |
| `POST /internal/v1/analysis-jobs/{job_id}/result` | Передаёт неизменённое сообщение-кандидат; Core отвечает тем же `message_id`. |
| `POST /internal/v1/analysis-jobs/{job_id}/failure` | Передаёт ограниченный машинный код ошибки. Core выбирает повтор или terminal status. |

Worker поддерживает старый `analysis-job-v1` с `analysis` и новый `ai-job-v1` с `task`. Новый payload содержит идентификаторы job/tenant/company/trace, `lease_generation`, `deadline`, `lease_until`, `budget`, `scope` и `input_versions`. Для `ai-job-v1` последнее включает `context_version`, `source_policy_version` и пустой `event_versions`; события создаются/версируются внутри DataWorkflow. `scope.source_ids` должен совпадать с эффективными ID из политики.

Результат нового задания имеет envelope `ai-result-v1`, а `.candidate` — `DataResult` (`ai-data-result-v1`). В нём могут быть `collection`, `events`, `candidates`, `revisions`, `configurations`, `period_report`, `coverage` и `trace`. Статус кандидата `ready` или `completed` не равен опубликованной ситуации: публикация остаётся командой Core.

Core проверяет ACK, tenant/company, актуальные context/source-policy versions, срок и generation до записи. При изменении профиля компании или политики старый результат отклоняется. Один и тот же `input_signature` для tenant/company/event публикуется не более одного раза. Для уже опубликованных результатов Core включает соответствующие `published_assessments` (signature и assessment revision) в следующий снимок задания. Локальный кеш Python не служит доказательством того, что Core получил публикацию.

После отмены Core не продлевает lease или возвращает отказ heartbeat; Worker прекращает дальнейшие этапы и не отправляет кандидат. Уже отправленный провайдеру запрос может завершиться и тарифицироваться. Человеческие статусы, уведомления и немедленное отображение прогресса принадлежат Core/UI.

PHP должен хранить job lifecycle у себя: `queued → leased/running → completed|partial|unknown|failed|cancelled|expired`. Python workflow stage list предназначен для внутренней диагностики и не является API прогресса для пользователя. Используйте Core event/websocket/poll endpoint для UI; текущий stand показывает это состояние только через fixture `/fixture/state`.

Внутренние этапы Python можно проверить через `GET /internal/v1/executions/{request_id}?tenant_id=...`. Это диагностический журнал workflow, а не замена состоянию job в Core. API разрешает запрос только сервису и allowlisted tenant.

### Порядок подключения в Core

1. Сначала проверяйте сессию, membership и права в Symfony; не передавайте их cookie Python.
2. Соберите один версионированный `DataTask` из сохранённых контекста, эффективной source policy и опубликованных assessment proofs.
3. Зафиксируйте `context_version`, `source_policy_version`, `published_assessments`, tenant/company и лимиты в durable Core job.
4. Верните frontend ID job и Core-owned статус; UI опрашивает/получает обновление у Core, не запускает собственный crawler.
5. Python worker выполняет job с heartbeat и передаёт `ai-result-v1`; Core проверяет актуальность snapshot повторно при приёмке результата.
6. Core сохраняет candidate и revision в своей транзакции/outbox, только если политика, контекст, lease и публикационные правила ещё актуальны.

Для повторной задачи Core сравнивает актуальные `context_version`, `source_policy_version` и подпись входов. Обновление источников, профиля либо исключений создаёт новую версию snapshot и инвалидирует незавершённую работу; явная `reassess` задача получает свежие Core publication proofs. Не используйте повтор HTTP `analyze` как замену durable job и не считайте локальный Python DB источником бизнес-статуса.

Для `collect` без анализа результат может содержать отчёт получения и свежие документы, а статус остаться `unknown`, если событий для анализа нет. Это не ошибка модели и не сообщение об отсутствии новостей в мире: оно означает, что по разрешённому снимку нет готового аналитического кандидата.

### Что показать в UI

`ready` — кандидат прошёл структурные проверки; это не гарантия фактической истинности. `partial` означает ограниченное покрытие/неполные основания. `unknown` означает, что вывода недостаточно. `no_change` требует показать прежнюю актуальную публикацию и не создавать новую ситуацию. `publication_revision_required` обрабатывает Core; его нельзя выводить из наличия `candidates`.

В карточке не смешивайте статус исполнения (`queued`, `running`, `failed`) с качеством результата (`partial`, `unknown`, `no_change`). Версии snapshot, дата материала и время получения должны быть доступны при просмотре основания; пользовательское скрытие и исключение источника из анализа также должны иметь разные controls и права.

Показывайте дату `published_at`, отдельное время получения `fetched_at`, издателя/автора при наличии, source role, ссылку на материал, точную evidence цитату и неизвестные условия. Не объединяйте внешнее сообщение, банковскую аналитику и модельную гипотезу в один безымянный список фактов. Отдельно показывайте coverage limitations и текущие версии контекста/политики.

Ошибки `401`, `403`, `409`, `413`, `422` и timeout должны переводиться Core в понятные job/user состояния без показа содержимого внутренних исключений. Повтор Core result delivery использует тот же message ID и payload; новый анализ создаётся отдельной командой Core с новой generation/актуальными snapshot, а не повтором frontend POST.

## Источники и пользовательские исключения

Core формирует `SourcePolicySnapshot` с `policy_id`, tenant и revision, выдаёт grants и передаёт снимок в `DataTask.source_policy`. Python не выбирает издания и не включает подписку. Шаблон [sources.template.json](../apps/intelligence/examples/sources.template.json) содержит placeholders и не является перечнем разрешённых источников.

Добавление по ссылке начинается с разового `SourceProbeRequest` на `POST /internal/v1/sources/probe`. Core выдаёт точный HTTPS URL, tenant, срок grant, разрешённые hostname и типы. Например, поля запроса:

```json
{
  "grant_id": "probe-123",
  "tenant_id": "tenant-from-core",
  "candidate_url": "https://approved.example/news",
  "expires_at": "2030-01-01T00:10:00Z",
  "allowed_hosts": ["approved.example"],
  "allowed_types": ["application/rss+xml", "application/atom+xml", "text/html", "application/pdf", "application/json"],
  "timeout_seconds": 10,
  "max_bytes": 2000000
}
```

`approved.example` здесь — документационный placeholder, не реальный источник для запуска. Передавать URL можно только после проверки Core, разрешения владельца источника и точного allowlist; probe не является web search, не сохраняет подписку и не разрешает произвольный обход ссылок. Он возвращает найденные metadata и `missing_fields`; дату публикации система не подставляет сама. После пользовательского подтверждения Core сохраняет источник и выдаёт новый policy snapshot.

Collector поддерживает RSS/Atom, HTML, PDF и ограниченный JSON adapter с dot-path mapping. Дата HTML/PDF должна прийти из metadata/grant; без неё материал остаётся `pending_metadata`. Feed/API item URL должен укладываться в разрешённый hostname. Запросы ограничены HTTPS, проверкой публичного DNS с IP pinning и TLS, не более двух разрешённых redirect, 20 секундами и 2 МБ. Нет cookies, proxy-наследования, произвольных заголовков или общего crawler. Сбор вызывается через Core scheduler/job, без открытого браузера.

Для внутренних экранов поиска есть `POST /internal/v1/search`: Core передаёт tenant, разрешённые source IDs, `as_of`, запрос и при необходимости snapshot policy/document refs. Это поиск по локальному корпусу, не web search. Сам поиск новостей/настройка изданий и типов источников — UI/Core-команды; Python не добавляет элементы в список изданий и не сохраняет выбранную пользователем ссылку.

Классификацию документа (`public`, `synthetic`, `confidential`) выдаёт Core в каждой immutable source grant. Collector переносит её как provenance; он не повышает уровень доверия по тексту страницы, просьбе пользователя или `DataTask.synthetic`. Если Core не разрешил классификацию, применяется `confidential` и внешний model-call блокируется.

Общее исключение источника или издателя, а также явное исключение документа для анализа входит в версию общей политики. Такой источник/материал не должен попасть в новую retrieval выдачу даже из старого corpus cache. Личное скрытие публикации — только предпочтение интерфейса и не исключает материал из анализа компании. Запрос на исключение из анализа требует права, новой версии политики и пересчёта затронутых оценок.

Точные байты ответа и их SHA256 сохраняются в приватном tenant-scoped raw store коллектора; operational receipt содержит URL, издателя/автора/даты и corpus revision ref. Python передаёт author/event_time/raw provenance в CorpusStore. Ссылка на оригинал разрешается только с актуальным tenant/source policy и revision. Запись `source_role=bank` сама по себе не подтверждает доступ к банковским данным.

Подготовка обезличенного CSV кейса доступна как `POST /internal/v1/company-context/prepare` (`CompanyContextPrepareRequest`). Core передаёт аутентифицированный `tenant_id`, `filename`, `origin`, `rows` (1–500), необязательные `roles_by_synthetic_u`, `revenue_unit` (`RUB` или `thousand_RUB`) и точный фильтр `affected_company_ids`. Ответ — `CompanyContextPrepareResult` с предложениями `CompanyContextProposal`, не запись профиля. `SYNTHETIC_U` остаётся анонимизированным ID; GROUP_ID не создаёт связь поставки; платежи выводятся только как гипотеза наблюдения. Без подтверждённой единицы выручки квалификация по порогу остаётся `unknown`. Модуль чисто Python и не вызывает LLM; подробности — [подготовка CSV](client-data.md). Материалы организаторов загружаются по [инструкции](judging.md).

## Операции, настройки и публикация

`DataTask.operation` принимает `collect`, `analyze`, `reassess`, `period`, `onboard`, `optimize`, `rollback_config` или `prepare_description`; `trigger` различает ручной, scheduled и повторный запуск. `period` требует диапазон и режим `retrospective` либо `as_known`. `businesses` несёт разрешённые контекстные snapshots (до 20), а `published_assessments` — Core proofs (до 600). `as_known` ограничивает документы временем, когда они уже были известны.

При первой компании DataWorkflow создаёт отдельную безопасную analyst-конфигурацию на базе активной настройки или baseline. Генератор заполняет только ограниченные предпочтения; права, источники, факты компании и модельный бюджет остаются отдельными. Core получает `configurations`/ref в результате; таблицы Python не заменяют бизнес-профиль Symfony.

Групповой турнир конфигураций запускается отдельно через `python -m intelligence arena`. Проверить список, сгенерировать варианты, выполнить offline-прогон и посмотреть ручной слепой пакет:

```sh
python -m intelligence arena list
python -m intelligence arena generate
python -m intelligence arena run --run-id review-1
python -m intelligence arena review-pack --run-id review-1 --output /tmp/review-pack.json
```

Для публикации нужен отчёт подходящего несинтетического прогона, fresh holdout/shadow, ручная оценка и `promote` с `expected_revision`; пример полного цикла и rollback есть в [tournament.md](../apps/intelligence/docs/tournament.md). Для ручной настройки есть `arena export/register`; изменения неизменяемы и версионируются. Команда `arena rollback` создаёт новую ревизию, возвращающую стабильный вариант. Новый бизнес не ждёт турнира.

Запись в реестре Python не автоматически обновляет уже сохранённые PHP-ситуации. Core решает, когда создать новую задачу, принять candidate и показать новую business revision. Повтор без значимых изменений и с Core publication proof возвращает `no_change` и не должен плодить публикации.

## Модели и границы готовности

`data_classification=confidential` не отправляет профиль или основание на внешнюю модель: workflow использует локальный baseline. Для `public` или `synthetic` допустим внешний вызов только при соответствующей классификации каждого документа от Core, настроенном провайдере, бюджете, разрешённых тарифах и успешных проверках worker; каждый вызов проходит через общий лимит и ledger. Не включайте `--mode model` в обычный запуск ради заполнения поля.

В репозитории нет предоставленных реальных банковских PDF/CSV для проверки, подключённого PHP Core или разрешённых production источников/лицензий. Python API, worker и контрактный fixture stand тестируются; стенд не является Symfony, а демо-материалы синтетические. Не выполнена независимая оценка качества на размеченных 200–300 бизнес-кейсах. Learned ranking, embeddings и поиск по всей сети не заявляются как реализованная production функция без согласования модели и данных. См. [контрактный стенд](../apps/intelligence/docs/core-stand.md), [runtime](../apps/intelligence/docs/runtime.md), [проверки/ограничения](../apps/intelligence/docs/checks.md) и [матрицу готовности](ai-scope-and-readiness.md).

## Поля, которые важно показать в интерфейсе

- `selections`: почему компания отобрана, какие известные связи найдены; `no_signal_found` не означает отсутствия риска. При отсутствии связи кандидат остаётся `partial` с вопросом.
- `invalidations`: основание опубликованной оценки больше не доступно в текущем полном или явно выбранном снимке событий; Core проверяет пересмотр/отзыв актуальности, сохраняя историю. Это предложение действия, не удаление ситуации Python.
- `watch_results`: проверка подтверждённых Core условий по переданным наблюдениям, датам и версиям. `unknown` при отсутствии данных не заменяется `not_met`.
- `period_report`: описательные изменения числовых наблюдений и хронология внешних публикаций; `retrospective` использует сегодняшнее знание, `as_known` — фактическое время получения сервером. Позиция банка не попадает в хронологию внешних событий.
- `coverage.model_attempts`: реальные попытки, ошибки и usage; расходы общего разбора входят в общий бюджет задания, а не дублируются для каждой компании.

Критику не передаётся реальный профиль компании. Для явно синтетического задания с синтетическими основаниями допускается синтетический контекст; в остальных случаях семантическая проверка применимости ограничена. Передача закрытого профиля внешнему провайдеру требует отдельного решения о данных и направлении передачи.


## Подключить профиль и жизненный цикл настройки

1. Backend сохраняет проверенный `company-context-v2` и повышает `context_version` при изменении фактов. Все направления сохраняются; unknown и противоречия не превращаются в подтверждённые сведения.
2. Создаёт обычный Core job с `operation=onboard`. Результат сразу содержит работоспособный `config_ref` и `revision`.
3. При необходимости ставит `operation=optimize` с картой `expected_config_revisions`, теми же tenant/company scope и явным model budget. Первый анализ ждать не должен. По ошибке показывает сохранённую базовую настройку.
4. Ставит `collect` для получения разрешённых источников, `analyze` для существующего корпуса, `reassess` после уточнения/новых данных, `period` для интервала. Передаёт `published_assessments` из своих подтверждённых публикаций.
5. При приёме проверяет все `versions.company_contexts`, source policy и актуальную конфигурацию, затем применяет revisions идемпотентно. Для отката стратегии ставит `rollback_config` с ожидаемой ревизией; профиль не откатывается.

### Подготовка описания и автоматический follow-up

Для прямой анкеты используйте `POST /internal/v1/company-context/from-description` или CLI `prepare-description`: оба пути возвращают только детерминированное предложение. Если нужны разрешённые модельные предложения, Core может отправить обычную задачу `operation=prepare_description`; ответ всё равно не записывает профиль. Core проверяет предложения и создаёт собственную версию только после принятия.

После создания baseline Python может приложить `follow_up_intents` к результату `onboard`, `analyze`, `collect` или `reassess`. Core сначала принимает родительский результат, проверяет свежесть context/fingerprint/config revision и идемпотентность, затем применяет отдельную конечную `AutoOptimizationPolicy`. Без выделенной политики intent фиксируется как `skipped_budget`; с разрешённой квотой Core создаёт обычный `operation=optimize` job без внешних новостей и без отдельной очереди. Повтор не обходит лимит и не повторяет автоматически неуспешную платную генерацию. Автоматический PHP hook при сохранении компании пока не реализован.

См. полную схему потока, ограничения и команды в [документации персонализации](personalization.md), типы intent/policy в [контракте](../contracts/intelligence/README.md).

Frontend передаёт изменения и действия в Core. Показывает состояние фоновой задачи отдельно от `ready/partial/unknown`, источники и цитаты отдельно от гипотез и банковской позиции; уточнения — как поля, способные изменить вывод. Web уже подключён к Core для cookie-auth, компаний, контекста, метрик, dashboard и чтения новостей. Управление Python jobs, job status/progress и публикация worker-кандидата в UI пока не подключены; `sectorFor()` относится к демонстрационным экранам и не ограничивает Python-контракт.

Проверяемый новый пример: `docker compose run --build --rm intelligence demo-business`. Он использует локальные RSS и две синтетические компании; никакая имитация не называется подключённым Symfony или настоящим банковским корпусом. Форматы и ошибки описаны в [контракте](../contracts/intelligence/README.md).

Пошаговые подключения и последовательные JSON-примеры для Symfony: [инструкция разработчику](symfony-integration.md). Частота auto-optimize задаётся Core policy (по умолчанию 3600 секунд и 2 задачи/company/UTC-день), независимо от allocation ID; пропуск — `skipped_frequency`. [Проверка цикла и исследования](research-and-tournament-checks.md).
