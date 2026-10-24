# Компоненты

| Каталог | Что внутри | Состояние |
|---|---|---|
| `apps/web/src` | React-интерфейс | Статическая сборка и учебные данные |
| `apps/web/public` | HTML, стили, изображения | Копируются в сборку |
| `apps/core` | Описание PHP-ядра | Реализация принадлежит Backend |
| `apps/intelligence/intelligence/workflow.py` | Сквозное исполнение DataTask | Сбор, общий пакет, отбор компаний, пересмотр, период и watch evaluation |
| `apps/intelligence/intelligence/collection.py` | RSS/API/HTML/PDF и probe | Источники только из Core snapshot, исключения, polling/backoff, сырые оригиналы |
| `apps/intelligence/intelligence/events.py` | События и общие пакеты | Ревизии, группы перепечаток, цитаты, классификация и кеш |
| `apps/intelligence/intelligence/multi_agent.py` | Исследователь, аналитик, критик | Общий бюджет, ограниченный repair и поиск после критики |
| `apps/intelligence/intelligence/research.py` | Отбор компаний и зависимости | Подтверждённые/предполагаемые пути до трёх переходов |
| `apps/intelligence/intelligence/client_data.py` | Подготовка CSV контекста | Предложения Core, без LLM и записи бизнес-профиля |
| `apps/intelligence/intelligence/core_stand.py` | Контрактный стенд Core | Только fixture mode; не промышленный Backend |
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
