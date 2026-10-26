# Web-интерфейс

React-интерфейс собирается Node.js и запускается как статический сайт в непривилегированном nginx. В Compose nginx обслуживает UI и проксирует same-origin `/api/*` в Symfony Core.

## Запуск и вход

Полный продуктовый Compose stack запускается из корня: `docker compose up --build --wait`. Откройте http://localhost:4173 и войдите в Core локальной учётной записью из seeded database (`demo@alpha.test` / `DemoAlpha2026!`). Кнопка «Посмотреть демо» явно включает старый браузерный прототип: фикстуры и изменения этого режима остаются в localStorage и не становятся Core данными.

Web использует cookie Core с `credentials: same-origin`; токенов модели или внутренних Python service tokens в браузере нет. При старте приложение проверяет `/api/auth/me`, затем загружает доступные компании и их профиль/метрики/news read views. Context updates отправляются с `expectedVersion`; конфликт перезагружает актуальный Core state. Новости отображаются read-only: UI сам не запускает collection/analysis.

## Разработка

```sh
npm ci --prefix apps/web
npm --prefix apps/web test
npm run build
npm run validate
```

Root scripts: `npm run build`, `npm run validate`, `npm test`. Docker web runtime — nginx; Node применяется при сборке и тестировании, отдельный Node server не нужен. API endpoints переписываются proxy в `nginx.conf`; см. также [Core API](../core/README.md), [web status](../../docs/web-status.md) и [Docker](../../infra/README.md).
