# Статус web-приложения

React UI собирается Node.js и работает в nginx-контейнере со статическими файлами. UI показывает локальные демоданные и хранит действия в `localStorage`. PHP Core пока не подключён: `/api/` возвращает `503`. Python AI/Data запускается отдельно и не управляет браузерным демо.

Сквозной офлайн-путь Python запускается командой `demo-data`; он проходит collector, corpus, события и baseline-анализ по локальным фикстурам. `run-task`, внутренний API и worker также находятся в Python; worker требует настроенный Core endpoint. Это не production PHP интеграция. Исторические сведения о перенесённом Node Worker сохранены в [архивном аудите](reference/web-audit.md). Команды — в [infra README](../infra/README.md), схема AI/Data — в [руководстве интеграции](ai-integration.md).
