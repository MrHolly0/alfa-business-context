# Явная маршрутизация моделей

`ModelRouter(base_config, ledger_path, routes=...)` создаёт фиксированные сервисы для ролей `researcher`, `analyst`, `critic`, `extractor`, `generator` и `judge`. `services()` возвращает словарь этих сервисов, который можно передать в pipeline как `role_services`. `route_for(role, complexity="complex")` выбирает только явно заданный маршрут `<role>:complex`; если его нет, выбрасывает ошибку. Значение `standard` или отсутствие complexity выбирает обычный маршрут роли. Неудачный вызов не переключает модель и не запускает скрытый повтор.

Маршруты могут менять только `model`, `input_per_million`, `output_per_million`, `max_output_tokens` и `reasoning_effort`. Последний параметр принимает `none`, `low`, `medium` или `high`, задаётся только явно и доступен только для OpenRouter; при отсутствии параметра транспорт ничего не добавляет к запросу. В OpenRouter он передаётся как `reasoning: {"effort": ...}`. `max_tokens` по-прежнему задаёт общий предел генерации, поэтому рассуждение может занять часть этого лимита. Профиль, URL API, имя переменной ключа, budget scope, валюта, дневной и lifetime caps и максимум стоимости одного запроса всегда берутся из base-профиля. Все созданные сервисы используют один путь ledger; расходы ролей сходятся в тот же атомарный счёт.

`examples/model-routing.template.json` — пример верхних тарифных оценок на миллион токенов: Gemma $0.08/$0.40, Qwen $0.15/$1.20, GLM $0.20/$0.60 и DeepSeek Flash $0.40/$1.50. Пример явно ставит Qwen analyst/generator в `none`, а GLM critic в `low`; это настройки генерации, не подтверждение качества. Это консервативные конфигурационные caps для предварительной оценки, а не измеренные расходы или гарантия тарифа поставщика. Проверьте действующий каталог провайдера и ваш согласованный cap перед применением. Если ставка/модель не укладывается в `max_request_cost` или общий бюджет, `LLMService` отклонит запрос до отправки.

Пример подключения из процесса, которому уже передан проверенный base profile:

```python
import json
from pathlib import Path
from intelligence.llm.routing import ModelRouter

routes = json.loads(Path("examples/model-routing.template.json").read_text())["routes"]
router = ModelRouter(base_config, ledger_path=".state/llm-budget.sqlite3", routes=routes)  # логический ID; журнал хранится в PostgreSQL
role_services = router.services()
complex_analyst = router.route_for("analyst", complexity="complex")
```

Переменная API-ключа остаётся в base-конфигурации и считывается общим transport только при явном вызове модели. Routing-файл не содержит секретов и не включает сетевые вызовы при загрузке.
