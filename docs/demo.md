# Демо для проверки

## Продуктовый путь с Core

1. Из корня выполните `docker compose up --build --wait`.
2. Откройте http://localhost:4173 и войдите `demo@alpha.test` / `DemoAlpha2026!` (локальный seed only).
3. Проверьте список компании, карточку, профиль/context, метрики и dashboard. Измените факт в контексте и сохраните: Core хранит новую версию; при устаревшем `expectedVersion` отвечает конфликтом.
4. Откройте «Новости». Демо seed не создаёт источники, поэтому список новостей может быть пуст. UI только читает Core news данные и не инициирует сбор.
5. Чтобы проверить Core→Python: оператор добавляет разрешённый URL-командой `bin/add-news-source.php`, затем `bin/run-news.php COMPANY_ID`. Core передаёт snapshot во внутренний Python endpoint в режиме baseline; созданные run/document/event/assessment читаются через UI/API. Не добавляйте источник, который нельзя опрашивать.

Можно вместо Core нажать «Посмотреть демо». Это локальные browser fixtures/localStorage, не те же данные и не та же сессия, что Core.

## Python offline demo

Для самостоятельного синтетического пути выполните из корня:

```sh
docker compose --profile tools run --build --rm intelligence demo-data
```

Он проходит Python collector/corpus/events/baseline на локальной учебной фикстуре без внешней сети и model calls. Это не настоящая банковская новость и не Core job queue. Результат содержит кандидаты и coverage, не автоматически опубликованные Core assessments.

## Границы

Успешный UI login подтверждает только локальную cookie-сессию. Демо не проверяет банковскую SSO, промышленную изоляцию tenants, AlfaGen, качество на реальных клиентах или полноценную Core job queue/lease worker. Подробные команды — [development](development.md) и [infra](../infra/README.md).
