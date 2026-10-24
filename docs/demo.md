# Демо для проверки

React UI и Python AI/Data — два отдельных пути. UI не запускает Python задачу и пока не подключён к PHP Core.

## React UI

1. Из корня репозитория запустите `npm run dev`, затем откройте http://localhost:4173.
2. Откройте очередь ситуаций, выберите учебную ситуацию и посмотрите основание, дату, источник и неизвестные условия.
3. Откройте каталог источников, найдите материал и проверьте действия исключения одного материала или издания. Переключите профиль компании и сравните учебные сценарии.
4. Ожидаемый результат: интерфейс показывает локальные фикстуры и сохраняет локальные действия в `localStorage`. Это не обращение к backend, не проверка прав и не анализ через Python.

## Python AI/Data

Из каталога `apps/intelligence` выполните:

```sh
.venv/bin/python -m intelligence demo-data \
  --state-dir /tmp/alfa-demo-fresh \
  --output /tmp/alfa-demo-result.json
```

Ожидаемый JSON содержит `status: partial`; в `coverage.demo_summary` — 2 события, 2 учебные компании и 4 кандидата, `network_access: false`, `baseline_only: true`. Это офлайн-синтетический путь через Collector → Corpus → events → DataTask. Он не использует реальные банковские PDF/CSV или внешний LLM.

## Ограничения и проверки

UI и Python демо не соединены; `/api/` в nginx отвечает `503`, PHP Core отсутствует в текущем Compose. Python fixture не доказывает точность на реальных клиентах или новостях. Все ограничения и команды тестов приведены в [infra README](../infra/README.md); для полного backend/frontend контракта см. [AI integration](ai-integration.md).
