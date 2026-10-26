# Symfony Core: пользователи и база кейса

Symfony Core stores users, company data, metrics, source policies, collected documents, baseline assessments, feedback, and discussions. Bank identity and model inference are not connected yet.

## Локальный запуск

Нужны PHP 8.2+, PDO SQLite и Composer. Из `apps/core`:

```sh
composer install
php bin/schema.php
APP_SECRET='локальный-случайный-секрет' php -S 127.0.0.1:8090 -t public public/router.php
```

Для PostgreSQL задайте `DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DB` при запуске всех команд и сервера. Создавайте секрет и учётные данные самостоятельно, не добавляйте их в Git. SQLite-файл по умолчанию `var/core.sqlite`; путь меняется через `CORE_SQLITE_PATH`.

## Импорт и доступ

```sh
php bin/import-case.php case-v1 companies.csv [person-links.csv] [payments.csv]
php bin/create-user.php user@example.test 'Имя Пользователя' < password.txt
php bin/grant-company.php USER_ID COMPANY_ID viewer
```

Импорт одной версии выполняется в транзакции. Он требует все 24 заголовка компании, проверяет ссылки в остальных файлах и сохраняет каждый ряд без изменения, с hash и номером строки. Для платежей фактическое имя файла надо проверить: в PDF оно совпадает с именем файла физических лиц. Сумма, количество и выручка остаются исходными текстовыми значениями: единица/период не подтверждены.

Список company ID для администратора можно получить запросом к базе. Публичного маршрута для выдачи прав нет. `create-user.php` получает пароль из stdin, чтобы не помещать его в аргументы команды.

## Проверка

```sh
composer test
```

Проверяются импорт полей, хеширование пароля, выдача доступа и откат повреждённого импорта. HTTP-сценарий входа и изоляции организаций также проверен локально.

## API

- `POST /api/auth/login` `{ "email": "...", "password": "..." }` — вход, защищённая cookie-сессия на 30 минут без активности.
- `GET /api/auth/me`, `POST /api/auth/logout`.
- `GET /api/companies` — только организации пользователя.
- `GET /api/companies/{id}` — исходные признаки доступной организации без raw JSON.
- `GET/PUT /api/me/onboarding` — текущий шаг обучения; `PUT` `{ "nextStep": 0..9 }`.


## Реализованные возможности ядра

После `php bin/schema.php` доступны:

- `GET/PUT /api/companies/{id}/context` — подтверждённые пользователем факты с номером версии. Для записи нужны `facts` и `expectedVersion`.
- `GET/POST /api/companies/{id}/metrics`, `PATCH /api/companies/{id}/metrics/{metricId}` — каталог метрик и история определений.
- `POST /api/companies/{id}/metrics/{metricId}/observations` — ручное значение с периодом, единицей и источником. Без значения API возвращает `no_data`.
- `GET /api/companies/{id}/dashboard?from=YYYY-MM-DD&to=YYYY-MM-DD` — метрики и события за период. Сводка имеет статус `no_analysis`, пока ML не подключён.
- `POST /api/companies/{id}/events`, `GET /api/companies/{id}/metrics/{metricId}/events`, `PUT /api/companies/{id}/metrics/{metricId}/events/{eventId}` — ручные события и подтверждённые связи с метриками.
- `POST /api/companies/{id}/feedback`, `GET/POST /api/companies/{id}/discussions` — отзывы и обсуждения.

Роли `editor` и `admin` могут менять контекст, метрики, наблюдения и события. `viewer` может читать и оставлять отзывы или сообщения. Все данные фильтруются по доступу к компании. Конфликт версий возвращает HTTP 409. Прогресс обучения принимается последовательно и хранится по пользователю.

Для локального демо выполните `php bin/seed-demo.php`. Вход: `demo@alpha.test` / `DemoAlpha2026!`. Тестовая компания и пользователь не являются реальными данными банка. Форма входа сайта пока не подключена к этому API.

## Новости: источник → документ → оценка

Core хранит разрешённые источники и версию политики, а Python Intelligence выполняет сбор и baseline-анализ. Для локального запуска нужны зависимости из `apps/intelligence` (`python3.12 -m venv .venv` и `pip install -e .`). В каталоге `apps/core/config/case-bank-materials.json` перечислены девять названий из приложения 2 PDF и ссылка на общую папку; отдельные файлы и их метаданные из неё ещё не загружены.

Источник регистрирует оператор, указав точный HTTPS URL и разрешённые домены статей:

```sh
php bin/add-news-source.php COMPANY_ID https://example.org/feed.xml rss external public 'Название' 'Издатель' 2026-10-23T00:00:00+00:00 example.org
php bin/run-news.php COMPANY_ID
```

Для HTML/PDF обязательна дата публикации в metadata источника или документа. Неуказанная дата не заменяется временем загрузки. `run-news.php` собирает версионированный снимок контекста и источников, запускает существующий Python workflow, сверяет версии перед записью, сохраняет документы, ревизии, события и аналитические кандидаты. Повторный сбор не создаёт дублей. Python сохраняет оригинальные байты отдельно; Core отдаёт только доступные пользователю метаданные и основания.

`php bin/run-due-news.php` выбирает компании, которые не проверялись последний час. Эту команду нужно вызывать внешним планировщиком; постоянный процесс расписания в Core пока не запущен. Для офлайн-проверки без сети:

```sh
sh tests/news-smoke.sh
```

API: `GET /api/companies/{id}/news/sources`, `/news/documents?from=&to=`, `/news/runs`, `/news/runs/{runId}`, `/news/events/{eventId}/assessment`. На дашборде события из документов показывают статус и краткое описание оценки. Автоматическое совпадение темы пользовательской метрики с текстом материала сохраняется только со статусом `proposed` и неизвестным направлением влияния; подтверждение доступно отдельной командой. Baseline не вызывает LLM и не выдаёт причинную оценку. Реальный модельный режим, права на материалы и загрузка файлов из общей папки требуют отдельной настройки и проверки.
