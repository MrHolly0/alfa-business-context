# Компоненты

| Каталог | Что внутри | Состояние |
|---|---|---|
| `apps/web/src` | React-интерфейс | Статическая сборка и учебные данные |
| `apps/web/public` | HTML, стили, изображения | Копируются в сборку |
| `apps/core` | Описание PHP-ядра | Реализация принадлежит Backend |
| `apps/intelligence/intelligence/corpus.py` | Документы, версии, фрагменты и поиск | SQLite, фильтры по источникам/дате/tenant |
| `apps/intelligence/intelligence/pipeline.py` | План, поиск, анализ, проверка результата | Baseline и явный вызов модели |
| `apps/intelligence/intelligence/llm` | Адаптеры и учёт расходов | Хакатонный шлюз, бесплатный и отдельный платный профиль OpenRouter |
| `apps/intelligence/intelligence/analysts.py` | Версии настроек и выбор по роли/отрасли | Публикация после ручной проверки, история и откат |
| `apps/intelligence/intelligence/tournament.py` | Отдельное сравнение вариантов | Слепой судья, отложенные случаи, кеш и ручная оценка |
| `apps/intelligence/intelligence/experiments.py` | Генератор, судья, критик и отдельный пул расходов | Ограниченные вызовы через общий адаптер |
| `apps/intelligence/intelligence/worker.py` | Получение заданий и доставка кандидата | Проверяется с подставным Core |
| `contracts/intelligence` | JSON Schema, OpenAPI и примеры | Реальный формат Python-стороны |
| `infra` | Команды Docker | Web, Python CLI/API/worker и отдельные тесты |

Node backend отсутствует. Пользовательские контроллеры создаёт Backend на PHP. Подробная карта Python — в [README приложения](../apps/intelligence/README.md).
