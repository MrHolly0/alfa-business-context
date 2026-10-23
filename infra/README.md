# Локальный запуск

Из корня репозитория:

```sh
docker compose up --build --wait
```

Откройте <http://localhost:4173>. Для остановки используйте `Ctrl+C`; контейнер
не содержит базу данных или отдельные сервисы — Compose запускает только
интерактивный прототип.

Чтобы подключить OpenAI-совместимый шлюз локально, скопируйте `.env.example` в
`.env` в корне репозитория и заполните `LLM_BASE_URL`, `LLM_API_KEY`,
`LLM_MODEL` и разрешённый `LLM_CASE_MODEL`. Compose передаст переменные в
контейнер. Не храните ключи в Git. Без этих значений приложение работает в
режиме правил.

Вне Docker установите Node.js 22.15.0 (`.nvmrc`), затем выполните:

```sh
npm ci --prefix apps/prototype
npm test
npm run build
npm run validate
npm run dev
```

`npm run dev` сначала собирает артефакт и затем запускает preview на порту
4173. Нативный Node.js не загружает `.env` автоматически; для LLM задайте
переменные в окружении процесса либо используйте `node --env-file=.env
apps/prototype/scripts/dev.mjs` после сборки.


## Целевой MVP и текущий запуск

`docker compose up --build --wait` по-прежнему запускает только существующий React/Node прототип из `apps/prototype`. Этот npm/Compose путь не переходит на Symfony; `composer` для него не требуется.

Целевой MVP по новой редакции продуктового документа (стр. 11–19, 31, 52) предлагает React/Core UI и Symfony 7.4 / PHP 8.4 как Core modular monolith. HTTP API, PHP CLI workers и scheduler поставляются из одного Symfony-артефакта; Python/FastAPI отдельно выполняет ingest и анализ через внутренний Core API. PostgreSQL использует раздельные схемы и роли Core/AI, документы идут в объектное хранилище. SSE обслуживает Mercure либо согласованный канал банка. Node остаётся только технологией текущего демо: собственный Node BFF не является целевым Core и нужен лишь при контракте оболочки.

Symfony/PHP, Composer-пакеты, Python-сервис, PostgreSQL jobs, object storage и Mercure пока не подключены этим Compose. Допуск PHP, платформенные версии, SSO и канал обновлений должны быть согласованы с банком; Kafka и Qdrant не обязательны для первой демонстрации.
