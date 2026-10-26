# Ближайшие задачи

## Уже работает в текущей ветке

- Compose runtime для React/nginx, Symfony Core/PHP, Python Intelligence API и PostgreSQL с отдельными Core/AI базами и application/migration ролями.
- Локальная Core cookie-сессия, seeded demo user/company, company memberships, versioned context CAS, метрики/наблюдения/dashboard, ручные события, feedback и discussions.
- Web `/api/` reverse proxy, Core login/logout, выбор доступной компании, контекстная форма, dashboard, news read views и синхронный запуск проверки из UI для разрешённых ролей.
- Core news-source/run хранение и ограниченный синхронный путь Web/Core `POST /api/companies/{id}/news/runs` → PHP runner → Python `/internal/v1/analyze`; baseline default, exact configured tenant `core-local`; опциональный Core PHP scheduler.
- Базовые Core/web/Python test images/targets в Compose; CI должна выполнять каждый из трёх тестовых контуров.

## До полноценного анализа как продукта

| Приоритет | Задача | Результат |
|---|---|---|
| P0 | Создать durable Core Analysis Jobs: постановка, idempotency, transactional outbox, claim/heartbeat/result/failure, attempt limits, backoff, lease_until и fencing generation. | Python worker исполняет только job, выданные Core; duplicate delivery не дублирует публикацию, устаревший worker не записывает результат. |
| P1 | Добавить в UI ACL-защищённое управление источниками и подписками. | Core остаётся владельцем source policy; интерфейс заменяет ручную настройку оператора. |
| P0 | Отображать job status/progress/partial/error и published assessment/evidence refs в UI. | Сквозной пользовательский путь Core → AI worker → Core → UI. |
| P1 | Согласовать tenant/membership mapping, identity/SSO, secrets rotation, audit/retention и deployment security с владельцами платформы. | Банковский пилот после отдельной проверки; локальный demo auth не переносится как SSO. |
| P1 | Проверить новую новость на первоисточник/перепечатки/исправление и адресный fan-out по company dependencies. | Контролируемое обновление assessment и объяснимое отсутствие/неполнота основания. |
| P1 | Провести независимую оценку качества, false influence links, пропусков, стоимости и полезности уточнений. | Измеримые критерии до расширения модели/корпуса. |

Автоматический `follow_up_intents`/optimize путь и полный worker lifecycle остаются контрактными/fixture сценариями до появления соответствующего Core implementation. Kafka, Redis, Qdrant, SSE и AlfaGen не включать без требования, доступа и подтверждённого выигрыша. Реальные клиентские CSV/PDF в репозитории не предполагаются.
