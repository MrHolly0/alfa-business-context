# Модели и расходы

Один Python-адаптер используется анализом, worker и отдельной командой `llm`. Вызовы идут только в шесть разрешённых моделей из [инструкции по ключам](https://docs.google.com/document/d/1ly8GsI_-oPnCQWvtZ4nGevnnDP2MGYIfpn2zOUFxZ_4). Автоматических переключений между провайдерами или моделями нет.

## Бесплатная разработка

Шаблон выбирает `google/gemma-4-26b-a4b-it:free` на OpenRouter, нулевые тарифы и локальный дневной лимит USD 0. Это отдельный [бесплатный вариант модели](https://openrouter.ai/google/gemma-4-26b-a4b-it:free). У него могут быть ограничения частоты и доступности; при отказе запрос завершится ошибкой, платная модель не подставится. Верхний CLI анализа дополнительно отвергает платный OpenRouter model ID.

В корневом `.env`:

```dotenv
OPENROUTER_API_KEY=ваш_ключ
```

Не помещайте ключ в JSON, prompt, аргументы команды или Git. Docker читает `.env`; хостовый CLI сам его не загружает — используйте `uv run --env-file ../../.env` из каталога Python-приложения.

Живой анализ учебной новости:

```sh
docker compose run --build --rm intelligence analyze \
  --input /app/examples/analysis-request.json --mode model \
  --config /config/providers.json --profile openrouter
```

`demo`, тесты и обычный `evaluate` провайдер не вызывают. `evaluate --mode model` вызывает модель для каждого сценария; результат показывает проверки сценариев, а не доказанную точность на реальных данных.

## Хакатонный шлюз

Основной адрес — `https://changellenge.up.railway.app/v1`, резервный — `https://api.alfahack-gateway26.ru/v1`. Используется один явно выбранный адрес, без скрытого повтора на резервный. Командный лимит — 500 ₽ в сутки, сброс в 03:00 МСК.

Скопируйте `apps/intelligence/examples/providers.template.json` в `apps/intelligence/providers.local.json`, заполните `input_per_million` и `output_per_million` профиля `hackathon` в RUB. Пока тарифы `null`, запрос не выполняется: неизвестная цена не считается нулевой. В `.env` добавьте:

```dotenv
INTELLIGENCE_CONFIG_FILE=./apps/intelligence/providers.local.json
HACKATHON_API_KEY=ваш_ключ
```

Для запуска выберите `--profile hackathon --mode model`. Идентификаторы: `google/gemma-4-26b-a4b-it`, `z-ai/glm-5.3-flash`, `qwen/qwen3.6-35b-a3b`, `deepseek/deepseek-v4.1-flash`, `qwen/qwen3-vl-30b-a3b-instruct`, `moonshotai/kimi-k2.6`. Допуск ID не доказывает фактическую доступность в аккаунте.

## Как считаем

До вызова резервируется стоимость входного и выходного лимита. После ответа записывается `provider_reported` либо тарифная оценка `rate_estimate` с фактическими token counts. При неопределённом исходе резерв сохраняется. Output cap передаётся провайдеру; оценка input по UTF-8 байтам с надбавкой — ограничитель планирования, не токенизатор шлюза.

У запроса есть пределы вызовов, шагов и токенов. Разрешена одна починка JSON в пределах тех же лимитов; transport не повторяет запрос сам. Использованные токены, стоимость, версии и trace возвращаются вместе с кандидатом. Неизвестный usage не заменяется нулём.

Расходы USD и RUB разделены. Флаг `--compare-hackathon` у `analyze`/`evaluate` считает те же token counts по явно заданным тарифам хакатона; модели должны совпадать. Поле `projected_hackathon_cost_rub` — оценка, а не реальное списание шлюза и не конвертация USD. Для бесплатной модели фактическая стоимость может быть 0, расчётная хакатонная — больше 0.

Все процессы, использующие один SQLite-файл и `budget_scope`, видят общий локальный расход. Другой компьютер или приложение с тем же внешним ключом в этот счётчик не входит. Docker и хост имеют разные журналы; внешний общий остаток сверяется с оператором шлюза.

Посмотреть счётчик или отдельно проверить адаптер:

```sh
docker compose run --rm intelligence llm budget --config /config/providers.json --profile openrouter
docker compose run --rm intelligence llm validate --config /config/providers.json --profile openrouter
```

Команда `llm run` — низкоуровневый одиночный запрос для диагностики. Она обходит бизнес-анализ и выбирает модель из своего явного конфига. Для штатного тестирования используйте `analyze`.
