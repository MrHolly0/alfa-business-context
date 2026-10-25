# Подключение Symfony к Core и Python Intelligence

Эта инструкция описывает границы интеграции для backend-команды. Symfony остается владельцем пользовательского запроса, авторизации, канонического профиля и бизнес-очереди. Python Intelligence исполняет ограниченную задачу по неизменяемому снимку. Контрактные JSON-примеры лежат в `apps/intelligence/examples/symfony/`.

## Поток данных

1. **Сохранение компании.** Symfony проверяет право пользователя менять компанию, сохраняет исходное описание и увеличивает `context_version`. Сохраняйте tenant/company scope, время `as_of`, классификацию и происхождение ввода. При первом заполнении вызовите `POST /internal/v1/company-context/from-description` с `DescriptionPrepareRequest` для быстрого детерминированного предложения либо поставьте `prepare_description` как обычный `ai-data-task-v1`, если разрешен настроенный генератор. Прямой HTTP endpoint не вызывает модель и не записывает канонический профиль; результат workflow-задачи является предложением в обычном result cache.
2. **Проверка полей.** Покажите поля, точные цитаты/offsets, происхождение и отдельно assumptions/questions. Core или пользователь решает, какие утверждения принять. Уникальная точная цитата позволяет локально проверить span; предложение модели само по себе не подтверждение. Symfony/Core записывает принятую версию `CompanyContext v2` как канонический профиль и увеличивает версию. Python не становится владельцем master-данных; его snapshots и кеш не заменяют canonical store.
3. **Onboard.** Core создает `onboard` job со снимком принятого профиля, разрешенной `source_policy` и бюджетом. После принятия результата Symfony сохраняет ref и ревизию принятой конфигурации аналитика и проверяет `follow_up_intents` в одной транзакции: tenant, company, `context_version`, fingerprint профиля, ожидаемую ревизию конфигурации, родительский request и идемпотентность. Intent только просит Core рассмотреть оптимизацию; он не выделяет бюджет и не ставит job сам.
4. **Обычная оптимизация.** Для автоматического запуска Core отдельно проверяет `AutoOptimizationPolicy` allocation: конечные лимиты вызовов/токенов/шагов и положительный резерв RUB или USD. Текущий default без policy — `skipped_budget`. Частота ограничена cooldown 3600 секунд и максимумом 2 jobs на компанию за UTC-сутки; отказ по этому ограничению — `skipped_frequency`. Повторный intent с тем же idempotency key не расходует новую allocation. Только Core превращает допустимый intent в обыкновенный `operation=optimize` job, с бюджетом policy и точной `expected_config_revisions`.
5. **Collect и analyze.** Для поиска/сбора Symfony/Core передает выданный Core `SourcePolicySnapshot`; `source_ids` должны быть разрешены этим snapshot. Выполните `collect`, затем `analyze`/`reassess` на полученных данных и принятом профиле. Результаты относятся только к доступному разрешенному корпусу и его временной границе. Отсутствие найденного материала означает «в этом корпусе не найдено», а не отсутствие риска. Источники из произвольного задания не дают права на обход политики.
6. **Частичный кандидат.** Если доказательств недостаточно, `DataResult.status` и `CandidateAssessment.status` могут быть `partial` или `unknown`; передайте пользователю ограничения и уточнения. Это не причинный вывод и не подтверждение, что компания затронута или не затронута. Для публикации Symfony повторно проверяет ACL, версии профиля/policy/config, scope и нужные доказательства, затем сохраняет собственную publication revision и `input_signature`.
7. **Publication proof.** После успешной записи/публикации Core создает proof `{company_id,event_id,input_signature,assessment_revision}`. Добавляйте его как `published_assessments` в следующий `DataTask`. Python сможет вернуть `no_change` при совпадении подписи. Доставка результата/запись outbox сами по себе не являются доказательством публикации.

## Job endpoints и envelope

Исполняющий Worker использует базовый origin `CORE_BASE_URL` и bearer `CORE_SERVICE_TOKEN`, заданные оператором. URL не выбирается из job. Worker обращается к:

| Действие | Запрос |
|---|---|
| Claim | `POST /internal/v1/analysis-jobs/claim` с `{}`; ответ содержит `job` или `null` |
| Продлить lease | `POST /internal/v1/analysis-jobs/{job_id}/heartbeat` с `{"lease_generation": n}` |
| Передать результат | `POST /internal/v1/analysis-jobs/{job_id}/result` с полным `ai-result-v1` |
| Передать отказ | `POST /internal/v1/analysis-jobs/{job_id}/failure` с `{"lease_generation": n, "code": "..."}` |

Для `ai-job-v1` обязательны идентификаторы `message_id`, `job_id`, `tenant_scope`, `company_id`, `trace_id`, `deadline`, `lease_until`; `lease_generation`; `input_versions` с `context_version`, `source_policy_version` и `event_versions`; `scope.source_ids`; `budget`; и полный `task`. Worker проверяет tenant allowlist, совпадение scope и версий, разрешенные источники, lease/deadline и лимиты бюджета до исполнения. Результат содержит те же job/scope/version поля и `candidate` как полный `DataResult`.

Symfony enqueue hook — бизнес-интеграция, а не Python API. Python не предлагает endpoint для создания реальной Core job. Фактическая job queue, leases, publication и cancellation должны жить за Symfony/Core API. `CoreStand` работает только в явном fixture mode: он полезен для контрактных проверок и не является production Core или Symfony backend.

## Версии, конкуренция и восстановление

- **Lease и generation:** heartbeat продлевает только текущую аренду. После истечения lease Core может выдать задачу с новой `lease_generation`; результат старого поколения отклоняется. Worker прекращает работу при потере lease и сверяет версии до и после выполнения.
- **CAS и digest:** job привязан к версиям контекста, политики источников и конфигурации. Снимок, ожидаемая ревизия и содержимое защищены от подмены/устаревания. Передавайте новые версии новым job, не исправляйте payload уже выданной аренды.
- **Idempotency:** `request_id` + тот же payload могут вернуть сохраненный результат; переиспользование ключа с другим digest дает conflict. Result с тем же digest для job может быть подтвержден как duplicate. Другое содержимое результата для уже принятой job — конфликт.
- **409:** stale context/policy/config, generation mismatch или конфликт ключа означают, что надо получить актуальный снимок и создать новый job. Не отправляйте тот же устаревший результат снова. `401/403` требуют исправления credentials/scope; это не транспортная ошибка.
- **Retry/quarantine:** Worker хранит точный envelope в локальном durable outbox и повторяет только ошибки доставки/неопределенный ACK с ограниченными попытками и backoff. Сверьте статус в Core при неизвестном ACK. После stale/authorization/conflict outbox quarantined; оператор исправляет причину или создает новую job. Не запускайте оплачиваемую работу повторно только из-за сетевого таймаута.
- **Cancel:** стандартный `CoreClient` не имеет публичного cancel вызова. В stand есть только fixture-admin cancel; это не пользовательский API. Symfony должен дать ACL-защищенный cancel Core hook, который помечает job отмененной, инвалидирует lease и не принимает поздний результат.

## Что подключить в продукте

**Backend Symfony/Core:** сохранение профиля и его версий; ACL для пользователя и tenant; endpoint/очередь постановки job; trusted service auth и tenant allowlist; immutable snapshots и budgets; claim/heartbeat/result/failure API; scheduler и отдельная AutoOptimizationPolicy allocation; cooldown, UTC daily cap, идемпотентность и причины пропуска; транзакционная обработка accepted result/follow-up; cancel; обработка CAS/409; durable publication record и выдача publication proof; мониторинг и quarantine.

**Frontend Alfa:** форма описания и подтверждение конкретных полей; раздельный показ подтвержденных фактов и предположений; статус/прогресс job; причина `skipped_budget`/`skipped_frequency`; ограничения и вопросы для `partial` результата; действия пользователя по публикации/отклонению. Не показывайте отсутствие найденных документов как отсутствие риска.

В этом репозитории есть Python API, Worker/CoreClient и fixture stand; реальный Symfony Core connection и его endpoint/auth/queue lifecycle не поставляются. Наличие тестового stand подтверждает контрактную форму, но не фактическое подключение или production поведение. Генератор Alfa не считается настроенным или доступным только потому, что Python поддерживает provider route; его модель, credentials и лимиты отдельно подтверждаются владельцем deployment. Учебные fixtures и тестовые оценки не являются независимой валидацией качества на реальных компаниях.

## Локальные контракты и проверка

- [01-description-request.json](../apps/intelligence/examples/symfony/01-description-request.json) и [02-description-proposal.json](../apps/intelligence/examples/symfony/02-description-proposal.json) — запрос и deterministic proposal.
- [03-onboard-task.json](../apps/intelligence/examples/symfony/03-onboard-task.json) — валидный `DataTask` со снимком профиля и source policy.
- [04-worker-job.json](../apps/intelligence/examples/symfony/04-worker-job.json) и [05-worker-result.json](../apps/intelligence/examples/symfony/05-worker-result.json) — Worker envelope. Времена deadline/lease в job статичны: при запуске их нужно выставлять заново.
- [06-auto-policy.json](../apps/intelligence/examples/symfony/06-auto-policy.json), [07-optimize-job.json](../apps/intelligence/examples/symfony/07-optimize-job.json) и [07-optimize-result.json](../apps/intelligence/examples/symfony/07-optimize-result.json) — allocation и optimize job/result, созданные fixture CoreStand после принятого onboard результата. Worker job включает точный task, `expected_config_revisions`, source scope и отдельный cost reservation.
- [08-collect-task.json](../apps/intelligence/examples/symfony/08-collect-task.json) и [09-collect-result.json](../apps/intelligence/examples/symfony/09-collect-result.json) — collect по синтетическому feed через in-process fixture fetcher.
- [10-analyze-task.json](../apps/intelligence/examples/symfony/10-analyze-task.json) и [11-analysis-result.json](../apps/intelligence/examples/symfony/11-analysis-result.json) — baseline анализ и partial candidate с ограничениями.
- [12-next-analysis-with-publication-proof.json](../apps/intelligence/examples/symfony/12-next-analysis-with-publication-proof.json) и [13-publication-proof-result.json](../apps/intelligence/examples/symfony/13-publication-proof-result.json) — следующий запрос с типизированным `PublishedAssessment` и ответ workflow после учета proof.
- `apps/intelligence/intelligence/workflow.py` — `DataTask`, `DataResult`, snapshots и publication proof.
- `apps/intelligence/intelligence/personalization.py` — `FollowUpIntent`, `AutoOptimizationPolicy`.
- `apps/intelligence/intelligence/core_client.py` — точные Worker/Core маршруты.
- `apps/intelligence/intelligence/core_stand.py` — исполняемый fixture контракт и fixture-only admin операции.

Файлы описывают одну синтетическую цепочку: `tenant-a` / `транссевер`, context version 1, source-policy revision 7, одна временная граница. В `03` профиль уже сохранен после проверки полей: отрасль, описание и роль приняты при review, а выделенные из текста activity/dependency сохраняют quote provenance. `04` — отправленная CoreStand onboard job; `05` — её фактически принятый result с configuration и `follow_up_intents`; `06` — использованная allocation; `07` — optimize job и accepted result, созданные тем же CoreStand из этого intent. `08`–`13` продолжают тот же tenant/company/context/policy. В `09` collect уже содержит локальный baseline candidate; `11` и `13` показывают `DataResult.status=partial` при `revisions[].status=no_change`. Читайте эти статусы раздельно: candidate остается неполным, а revision говорит, нужно ли Core публиковать новую оценку по этому input signature.

Для collect использовался in-process feed fixture без сети; результат baseline может быть partial. Optimize job и result получены локальным test generator fixture с пометкой test config; это не измеряет доступность/качество production модели. Генерация проверила DescriptionPrepareRequest/`prepare_description`, `DataTask`, `DataResult`, Worker `_check` на `ai-job-v1`, и CoreStand обработку фактического accepted `ai-result-v1`; collect/analyze/proof прогнаны через `DataWorkflow`. В fixture Worker использовал часы теста до истечения записанного lease; эти статичные deadline/lease не переносите в рабочую очередь. Runtime fixture не подтверждает действующее подключение реального Core. Перед production отдельно проверьте маршрутизацию, генерацию, права, job lifecycle и публикацию на Symfony Core; stand этого не подменяет.

## Что проверено, а что ещё подключить

| Контур | Проверено | Остаётся подключить и проверить |
|---|---|---|
| Python и стенд Core | Получение/lease/result, приём onboarding, постановка optimize с квотой и частотой, scoped collect/analyze, publication proof и устранение повторов | Это локальные контрактные сценарии; публикация стенда не является публикацией Symfony |
| Настоящий Symfony | В репозитории есть протокол и согласованные примеры; PHP-реализация не предоставлена | ACL и canonical store, транзакционная очередь, service auth, scheduler, cancel, актуальность версий, приём/доставка/публикация и путь до UI |
| Шлюз Альфы | Ранее metadata endpoints отвечали 200, ключ объявлял лимит 500; генеративный вызов не выполнялся | Подтверждённая модель, тарификация/верхний резерв, генерация и учёт реального расхода через этот шлюз |
| Другой модельный API | Настоящие вызовы OpenRouter/GLM, включая небольшой турнир и косвенное исследование | Это не проверка Альфы и не независимая оценка качества |

[Фактические результаты и расходы](research-and-tournament-checks.md). Статические JSON — запись согласованного синтетического прогона. При изменении runtime и входов пересоздавайте запросы, сроки и publication proof: старый proof не гарантирует `no_change` для нового кода.
