# Обмен с Python

Здесь лежат схемы реализованной Python-стороны. Backend реализует свою часть; настоящий PHP Core пока не подключён. Прежние примеры `*-draft-1` в соседней папке сохранены как проектные материалы, для интеграции используйте версии `*-v1` отсюда.

## Внутренний API Python

[OpenAPI](openapi.json) описывает `GET /health` и четыре POST-маршрута:

| Маршрут | Вход | Результат |
|---|---|---|
| `/internal/v1/documents` | [Документ](document.schema.json), PDF в base64 | ID документа/ревизии, число фрагментов, предупреждения; RSS возвращает `items` с результатами отдельных записей |
| `/internal/v1/search` | [Поисковый запрос](search.schema.json) | Разрешённые фрагменты с цитатой, hash, датой и локатором |
| `/internal/v1/rank` | [Профиль и список событий](rank-request.schema.json) | [Предварительный порядок](rank-result.schema.json) с объяснениями лексических совпадений |
| `/internal/v1/analyze` | [Запрос](analysis-request.schema.json) | [Кандидат оценки](candidate-assessment.schema.json) |

Приватные маршруты требуют `Authorization: Bearer <INTELLIGENCE_SERVICE_TOKEN>`. Tenant должен входить в настроенный на сервисе `INTELLIGENCE_TENANTS`. Токен означает доверенного вызывающего Core; Python не принимает пользовательскую сессию и не определяет membership. Core обязан выдать только разрешённый профиль и список источников. Источник с ролью `bank` импортируется доверенным Core; сам текст не может назначить себе такую роль.

`analyze`: одинаковые tenant/request_id и нормализованное тело возвращают сохранённый результат; другое тело — `409`. Одновременный повтор или неопределённый прерванный запрос — `409 request_in_progress`, без второго вызова модели. `401` — нет сервисного доступа, `403` — tenant запрещён, `413` — большой запрос, `422` — ошибка входа. `ready` означает прошедшую структурную проверку оценку, а не доказанную истинность. `partial`/`unknown` должны показываться пользователю явно.

Примеры входов находятся в `apps/intelligence/examples`: `analysis-request.json`, `documents.json`, `rank-request.json`. Все они синтетические. [Пример задания](analysis-job.example.json) и [кандидата](candidate-assessment.example.json) показывают те же форматы; временные lease в примере заменяйте текущими. Кандидат не является опубликованной revision: публикация остаётся за Core.

## Маршруты, которые реализует Core

| Метод | Ответ |
|---|---|
| `POST /internal/v1/analysis-jobs/claim` | `204` без задания; `200 {"job": <analysis-job-v1>}` с [заданием](analysis-job.schema.json) |
| `POST /internal/v1/analysis-jobs/{job_id}/heartbeat` | Вход `{"lease_generation": 1}`; `200 {"accepted": true, "lease_generation": 1, "lease_until": "...Z"}` |
| `POST /internal/v1/analysis-jobs/{job_id}/result` | [Результат](analysis-result.schema.json); `200 {"accepted": true, "message_id": "тот же ID"}` |
| `POST /internal/v1/analysis-jobs/{job_id}/failure` | Вход `{"lease_generation": 1, "code": "invalid_job"}` либо `analysis_failed`; Core решает, повторять ли задание |

Worker передаёт `Authorization: Bearer <CORE_SERVICE_TOKEN>`. Адрес берётся только из `CORE_BASE_URL`, редиректы запрещены. `input_versions` включает context/source policy и ровно одно событие с ревизией. `scope.source_ids` и `document_revision_refs` совпадают с inline `analysis`. Список ревизий ограничивает фоновый корпус; доверенная inline-новость добавляется отдельно. Отсутствующий context snapshot нельзя заменять чтением таблиц PHP.

Для платного OpenRouter в тестовом задании требуется также `budget.cost_reservation_usd`; RUB-резерв не переводится в доллары. Worker проверяет достаточность USD-резерва по верхним тарифам профиля. В хакатонном профиле остаётся `cost_reservation_rub`.

Перед выполнением проверяются generation, tenant/company, версии и лимиты. Heartbeat продлевает lease во время вызова модели; `lease_until` не позже `deadline`. `409` или неподтверждённый heartbeat останавливает дальнейшие шаги. Уже отправленный запрос модели прервать гарантированно нельзя, его расход учитывается, но кандидат не публикуется.

Core сначала проверяет сервисную идентичность и scope, затем дедупликацию. Одинаковые `message_id` и payload должны возвращать прежний ACK даже после закрытия lease. Иное тело под ID, устаревшая generation, отмена или конфликт версий — `409`; нет scope — `403`. Это необходимо для доставки исходящего сообщения после потери ACK. Worker хранит точный payload до подтверждения; terminal конфликт переносится в локальный архив ошибок, без повторного вызова модели.

Неопределённый прерванный анализ не перезапускается сам. Оператор останавливает процесс, проверяет журнал провайдера и локальное состояние (`intelligence inspect`), затем при необходимости явно разрешает повтор через `reset-request`/`reset-job --confirm-stopped`. Это может снова вызвать модель; предпочтительный повтор задания выдаёт Core с новой generation.

## Обновление схем

Из `apps/intelligence`: `uv run python scripts/export_contracts.py`. Проверка актуальности: та же команда с `--check`. JSON Schema ответа модели экспортируется отдельно в [model-answer.schema.json](model-answer.schema.json) и передаётся модели в prompt; это не гарантия провайдера, Python проверяет ответ самостоятельно.

`analyst-config.schema.json` описывает разрешённые настройки поверх фиксированных системных правил. Версия выбранной конфигурации приходит в `candidate.versions.analyst_config`; Backend может сохранять её вместе с остальными версиями, без исполнения инструкций из настроек.

Повтор доставки результата использует прежние `message_id` и payload. Python применяет ограниченный backoff и изолирует постоянные ошибки; это не заменяет транзакционную дедупликацию результата и проверку generation/версий на стороне Core. Локальные retry-поля не входят в wire-контракт.
