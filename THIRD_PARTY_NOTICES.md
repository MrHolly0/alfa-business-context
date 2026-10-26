# Сторонние компоненты и материалы

Версии Web закреплены в `apps/web/package-lock.json`, Python — в `apps/intelligence/uv.lock` и производных `requirements*.lock`, PHP — в `apps/core/composer.lock`. Сверяйте транзитивные зависимости и условия лицензии при обновлении.

## Прямые зависимости

| Компонент | Версия | Использование | Лицензия по lock/package metadata |
|---|---:|---|---|
| React / react-dom | 18.3.1 | Web UI | MIT |
| html-react-parser | 6.1.9 | Разбор отображаемой разметки | MIT |
| esbuild | 0.28.2 | Сборка Web | MIT |
| @alfalab/core-components | 50.37.0 | Компоненты Альфы | MIT в package-lock; условия дистрибутива сверить перед выпуском |
| FastAPI | 0.162.0 | Python internal HTTP API | MIT |
| Pydantic | 2.14.1 | Проверка JSON и схем | MIT |
| Uvicorn | 0.54.0 | Python HTTP server | BSD-3-Clause |
| HTTPX | 0.28.1 | Вызовы Core API из Python worker | BSD-3-Clause |
| Psycopg / psycopg-binary | 3.3.6 | PostgreSQL driver | Сверять лицензию дистрибутива при изменении версии |
| psycopg-pool | 3.3.3 | Ограниченный PostgreSQL connection pool | Сверять лицензию дистрибутива при изменении версии |
| pypdf | 6.21.2 | Извлечение текста PDF | BSD-3-Clause |
| defusedxml | 0.7.1 | Разбор RSS/XML | PSFL |
| pytest | 9.1.1 | Python tests | MIT |

Web собирается на Node.js 22.15 и в runtime обслуживается nginx. Symfony Core запускается в контейнере `php:8.4-apache`; `apps/core/composer.json` требует PHP `>=8.2`, а `composer.lock` фиксирует Symfony FrameworkBundle `v7.4.21`. В `composer.lock` записано 29 runtime-пакетов; у всех 29 license metadata — MIT. Ключевые зафиксированные Symfony packages: `framework-bundle`, `http-foundation`, `http-kernel`, `dependency-injection`, `routing`, `runtime`, `string`, `yaml` — `v7.4.21`; остальные транзитивные версии указаны в [composer.lock](apps/core/composer.lock). Apache/PHP image tag здесь закреплён на ветке 8.4, а не на patch release.

Основная рабочая persistence граница Python — PostgreSQL через Psycopg; Core имеет отдельную PostgreSQL database/roles. SQLite остаётся в Python fixture/local state и legacy import путях, а также в PHP test fixtures. Это не утверждение, что production persistence обоих сервисов SQLite.

## Локальное распознавание PDF

Образ Python включает системные `poppler-utils`, `tesseract-ocr`, `tesseract-ocr-rus` и `tesseract-ocr-eng`. Они используются отдельными локальными процессами без сети. Tesseract распространяется по Apache-2.0; полные условия Poppler и языковых данных находятся в `/usr/share/doc/<package>/copyright` внутри образа. При распространении образа сохраняйте эти документы и учитывайте условия его системных компонентов.

## Сохранённые материалы

- Lucide / исходные Feather-иконки: ISC/MIT, текст в `apps/web/public/assets/Lucide-LICENSE.txt`.
- Inter: SIL Open Font License 1.1, текст в `apps/web/public/assets/Inter-LICENSE.txt`.
- Растровые иллюстрации перенесены из исходного архива. Их происхождение и разрешение на дальнейшее распространение не установлены; не заявляйте авторство или лицензию без подтверждения.

## Данные и модели

CSV и девять аналитических PDF из кейса в репозиторий не добавлены. Учебные Python-примеры и сценарии явно помечены как синтетические. Материалы с ролью `bank` в этих примерах не являются реальной позицией Альфа-Банка. Для реальных материалов сохраняйте источник и разрешённые условия использования.

Runtime поддерживает шесть моделей кейса: Gemma 4 26B A4B, GLM-5.3-Flash, Qwen3.6 35B A3B, DeepSeek-V4.1-Flash, Qwen3-VL-30B-A3B-Instruct, Kimi-K2.6. Точные ID и адреса задаются [адаптером](apps/intelligence/intelligence/llm/config.py) по инструкции организаторов. OpenAI-compatible протокол не означает использование модели OpenAI. Тестовые профили OpenRouter включают бесплатную Gemma `:free` и отдельные платные маршруты Gemma, Qwen и GLM с ограничением цены и расходов; автоматического перехода между ними нет. Подключение к SSO и банковским системам не реализовано.
