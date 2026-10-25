# PostgreSQL и хранилище Intelligence

Python-модуль Intelligence хранит рабочее состояние в PostgreSQL. Локальные SQLite-файлы нужны для контролируемого однократного импорта старых данных и для тестового Core Stand; они не являются рабочей базой приложения.

## Сервис и роли

Compose запускает PostgreSQL `17.11-bookworm` с базой `business_context_ai`. Роль `intelligence_owner` владеет схемами и таблицами, не может входить в систему и не имеет `SUPERUSER` или `BYPASSRLS`. Миграционная роль `intelligence_migrator` может входить и получает членство в роли владельца. Рабочая роль `intelligence_app` может входить, но не имеет привилегий владельца, `SUPERUSER` или `BYPASSRLS`.

Для тестов и основного приложения созданы отдельные базы: `business_context_ai_test` и `business_context_ai`. Compose публикует тестовый PostgreSQL на `127.0.0.1:55483` по умолчанию. Основной и тестовый клиенты получают разные DSN и не разделяют таблицы.

Пароли в локальной конфигурации Compose заданы только как значения для первого запуска. Для уже созданного volume изменение переменных не меняет пароли внутри PostgreSQL автоматически. Не добавляйте реальные пароли в репозиторий; настройте их в окружении развертывания. Файл `.env` документация не создает и не меняет.

## Подключение приложения и миграции

Приложение подключается через `INTELLIGENCE_DATABASE_URL`. Миграции и импорт выполняются отдельно через `INTELLIGENCE_MIGRATION_DATABASE_URL`; рабочему приложению эту роль не передают.

В Compose миграции запускает `intelligence-migrate`, перед стартом сервисов анализа и worker. Та же команда доступна локально в окружении проекта:

```sh
cd apps/intelligence
INTELLIGENCE_MIGRATION_DATABASE_URL='postgresql://…' \
  .venv/bin/python -m intelligence db migrate
```

Для проверки доступности приложения используйте `.venv/bin/python -m intelligence db check` с `INTELLIGENCE_DATABASE_URL`.

Команда сверяет checksum применённых файлов миграций. Изменение уже применённого файла считается ошибкой; новую структуру добавляют новой миграцией.

Тестовый контур запускается отдельно:

```sh
docker compose --profile test up -d postgres
docker compose --profile test run --build --rm intelligence-test-migrate
docker compose --profile test run --build --rm intelligence-test
```

`intelligence-test-migrate` применяет миграции только в `business_context_ai_test`, а `intelligence-test` использует рабочую роль приложения для той же тестовой базы. В наборе тестов каждой функции назначается отдельный namespace.

## Схемы, изоляция и типы

Компоненты используют отдельные схемы PostgreSQL: `ai_corpus`, `ai_collection`, `ai_analysts`, `ai_company_analysts`, `ai_events`, `ai_tournaments`, `ai_budget`, `ai_artifacts` и `ai_workflow`.

В каждой таблице есть `store_key`. Политики Row-Level Security фильтруют строки по значению `intelligence.store_key` в транзакции. Ключ входит в первичные и уникальные ограничения и во внешние ключи; ссылки между компонентами проверяются в своих схемах. Код storage задаёт ключ и `search_path` для каждого соединения. Для таблиц используются отдельные соединения с короткими транзакциями; пул ограничен параметром `INTELLIGENCE_DB_POOL_MAX` и по умолчанию равен четырём соединениям на процесс.

Для большинства компонентов namespace определяется корнем `INTELLIGENCE_STATE_DIR` и логическим путём хранения. Полный путь не используется как открытый ключ: он преобразуется в hash и добавляется к `INTELLIGENCE_STORAGE_NAMESPACE` (в Compose — `main`). Компонент бюджета использует общий namespace `main:shared-budget`, независимо от логических путей, чтобы лимиты и расходы были общими для установки.

Денежные значения читаются и записываются как точные `NUMERIC`/`Decimal`; преобразование в `float` не применяется. Временные значения хранятся как `TIMESTAMPTZ`, а строки API сериализуются в UTC с суффиксом `Z`. JSON-поля представлены текстом с проверками типа JSON на стороне PostgreSQL.

Оригинальные загруженные файлы и сырые ответы источников остаются в файловом хранилище. В базе находятся их ссылки и контрольные суммы. Compose для данных Intelligence предоставляет volume `intelligence-state`; PostgreSQL хранит свои данные в отдельном volume `intelligence-postgres`.

## Импорт прежней SQLite-базы

Импорт запускается вручную и только после остановки процесса, который может изменять SQLite. Команда требует флаг `--confirm-quiesced`; его наличие подтверждает оператор. Источник копируется вместе с WAL/SHM во временный read-only snapshot, его hash проверяется до и после импорта. Исходный SQLite-файл не открывается на запись. Импортёр проверяет целостность и внешние ключи SQLite, сверяет таблицы и колонки с целевой схемой, требует пустой namespace, сохраняет идентификаторы и ссылки и добавляет manifest импорта в той же транзакции PostgreSQL.

Сначала выполните проверочный проход `--dry-run`, затем импорт с теми же параметрами без `--dry-run`:

```sh
docker compose --profile tools run --rm intelligence-import import-sqlite \
  --source /legacy/corpus.sqlite3 \
  --component corpus \
  --logical-path /app/.state/corpus.sqlite3 \
  --confirm-quiesced --dry-run

docker compose --profile tools run --rm intelligence-import import-sqlite \
  --source /legacy/corpus.sqlite3 \
  --component corpus \
  --logical-path /app/.state/corpus.sqlite3 \
  --confirm-quiesced
```

Этот Compose-вариант подходит, если исходный файл уже находится в `intelligence-state` volume, смонтированном read-only по адресу `/legacy`. Для источника на хосте используйте локальный CLI ниже и передайте его абсолютный путь:

```sh
cd apps/intelligence
INTELLIGENCE_MIGRATION_DATABASE_URL='postgresql://…' \
  .venv/bin/python -m intelligence db import-sqlite \
  --source /path/to/legacy-corpus.sqlite \
  --component corpus \
  --logical-path /path/to/logical/corpus.sqlite \
  --confirm-quiesced --dry-run

INTELLIGENCE_MIGRATION_DATABASE_URL='postgresql://…' \
  .venv/bin/python -m intelligence db import-sqlite \
  --source /path/to/legacy-corpus.sqlite \
  --component corpus \
  --logical-path /path/to/logical/corpus.sqlite \
  --confirm-quiesced
```

`--dry-run` проверяет read-only source, schema compatibility и scope, но не вставляет строки и потому не исполняет row-level target constraints на реальных данных. Ошибки target constraints могут проявиться только при фактической транзакции импорта; та транзакция атомарно откатывает строки и manifest при отказе.

Вместо `corpus` укажите целевой компонент из списка схем. Повтор с тем же source hash проверяет manifest и количества строк и завершает работу как идемпотентный. Другой источник или непустой целевой namespace отклоняется; объединение SQLite и PostgreSQL не выполняется. Поле `raw_ref` переносится как ссылка: сами raw-файлы импортёр не копирует. Снимок источника передавать нельзя, если исходный процесс ещё работает или пишет в БД.

SQLite внутри Core Stand используется тестами протокола Core. Это отдельный стенд и не переключает хранилище Intelligence с PostgreSQL.


## Восстановление и конкурентность

HTTP/LLM выполняются вне транзакций. Изменения бюджета, версии конфигурации, записи событий и идемпотентности сериализуются короткими транзакционными advisory locks; уникальные ограничения и условные обновления остаются окончательными проверками. Резерв и списание берут блокировку одного budget scope. Фактическая стоимость, если она оказалась выше оценки, записывается полностью; последующие резервы учитывают её.

Выполнение workflow защищено PostgreSQL lease на 120 секунд с продлением каждые 20 секунд и токеном владельца. Это координация аналитических артефактов, не очередь бизнес-заданий. Истёкший или прерванный запуск не повторяет неизвестный оплаченный этап автоматически. После остановки прежнего исполнителя и проверки расходов оператор может разрешить возобновление:

```sh
docker compose --profile tools run --rm intelligence db recover-execution \
  --state-dir /app/.state --tenant TENANT --request REQUEST --confirm-stopped
```

Активный lease восстановить нельзя. Отдельные резервы API/outbox и неопределённые модельные расходы сохраняют собственные процедуры восстановления, описанные в документах соответствующих модулей. Для явного повторения DB-only операций предусмотрен `Database.transaction`: по умолчанию три попытки для serialization failure/deadlock, с backoff и jitter. Допускается от одной до пяти попыток; сетевые и модельные вызовы этот helper не повторяет.

После импорта прежнего budget-файла приложение проверяет manifest. Пока существующий legacy ledger не импортирован в текущую учётную область, конструктор отказывает с `legacy_budget_import_required`. Не меняйте namespace для обхода этого ограничения. Файловые пути с суффиксом `.sqlite3` в конструкторах сохранены как логические идентификаторы для совместимости; новые SQLite-файлы они не создают.

Тестовый набор требует обе test DSN и отказывает, если имя базы не заканчивается на `_test`. В API/worker передают только app DSN. Проверенные сценарии и оставшиеся границы — [результаты миграции](postgresql-checks.md).
