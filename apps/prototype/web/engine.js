// Pure, auditable assessment engine. Seed examples and live imports remain distinguishable.
export const TODAY = "2026-10-22";
export const stable = (value) =>
  JSON.stringify(value, (_, v) =>
    v && !Array.isArray(v) && typeof v === "object"
      ? Object.fromEntries(
          Object.entries(v).sort(([a], [b]) => a.localeCompare(b)),
        )
      : v,
  );
export function hash(value) {
  let h = 2166136261;
  for (const c of typeof value === "string" ? value : stable(value)) {
    h ^= c.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(16);
}
export const clone = (x) => JSON.parse(JSON.stringify(x));
export const activeSources = (s) =>
  s.sources.filter((x) => !x.excluded && x.available && x.status === "ready");
export function topics(text) {
  const t = text.toLowerCase(),
    r = [];
  if (/ставк|кредит|инфляц|финанс|денежно/.test(t)) r.push("finance");
  if (/сырь|картон|бумаг|себестоим|закупоч|затрат|тариф/.test(t)) r.push("raw");
  if (/потреблен|рознич|спрос|пищев|доход|рынк. труда/.test(t))
    r.push("demand", "packaging", "software");
  if (/логист|доставк|перевоз|маршрут|импорт|порт\b/.test(t))
    r.push("logistics");
  if (/энерг|топлив|нефт/.test(t)) r.push("energy");
  return [...new Set(r)];
}
export function classify(text, declared = "") {
  if (
    [
      "Наблюдение",
      "Прогноз автора",
      "Мнение",
      "Прогноз банка",
      "Официальное сообщение",
    ].includes(declared)
  )
    return declared;
  if (/прогноз|ожида|возможно|предполага|по мнению|может|допуска/i.test(text))
    return "Прогноз автора";
  return "Не классифицирован";
}
const R = {
  packaging: {
    id: "packaging",
    title: "Спрос на упаковку",
    metric: "Заказы",
    icon: "package",
    type: "risk",
    horizon: "Ближайшие 1–3 месяца",
    topics: ["packaging"],
    sectors: ["packaging"],
    why: "Пищевые предприятия могут быть покупателями вашей упаковки.",
    chain: [
      "Потребление и выпуск покупателей",
      "Потребность в упаковке",
      "Объём ваших заказов",
    ],
    impact:
      "Замедление у покупателей может передаться в заказы на упаковку. Состав покупателей пока не подтверждён.",
    alt: "Покупатели могут формировать запас или менять поставщиков без снижения объёма закупок.",
    missing:
      "Сопоставьте фактические заказы пищевых предприятий и долю этого сегмента.",
    next: "Сверить планы трёх крупнейших покупателей",
    priority: 90,
  },
  raw: {
    id: "raw",
    title: "Условия следующей закупки",
    metric: "Себестоимость",
    icon: "trend",
    type: "risk",
    horizon: "При следующей закупке",
    topics: ["raw", "energy"],
    sectors: ["packaging", "restaurant"],
    why: "Материалы и продукты входят в затраты вашей компании.",
    chain: ["Затраты поставщика", "Цена следующей поставки", "Ваша маржа"],
    impact:
      "Рост рыночных цен может увеличить себестоимость после пересмотра договора.",
    alt: "Фиксация цены, запасы и возможность пересмотра отпускных цен могут отложить эффект.",
    missing: "Проверьте фиксацию цены и запас по основным позициям.",
    next: "Сопоставить цены и сроки следующей поставки",
    priority: 82,
  },
  finance: {
    id: "finance",
    title: "Стоимость финансирования",
    metric: "Денежный поток",
    icon: "card",
    type: "risk",
    horizon: "При пересмотре ставки или новом займе",
    topics: ["finance"],
    sectors: ["packaging", "restaurant", "software", "other"],
    why: "Рыночная ставка влияет на будущие условия заимствования, а не автоматически на каждый действующий договор.",
    chain: ["Рыночные ставки", "Условия вашего договора", "Процентные расходы"],
    impact:
      "Оценка зависит от типа ставки, срока рефинансирования и планов заимствования.",
    alt: "При фиксированной ставке до конца договора прямого изменения процентов может не быть.",
    missing: "Уточните наличие долга, тип ставки и планы нового займа.",
    next: "Проверить ближайший пересмотр кредитных условий",
    priority: 72,
  },
  demand: {
    id: "demand",
    title: "Изменение потребительского спроса",
    metric: "Выручка",
    icon: "people",
    type: "risk",
    horizon: "Ближайшие 1–3 месяца",
    topics: ["demand", "packaging"],
    sectors: ["restaurant"],
    why: "Доходы и потребление могут влиять на средний чек и посещаемость.",
    chain: [
      "Потребительские расходы",
      "Частота визитов и средний чек",
      "Выручка заведения",
    ],
    impact:
      "Охлаждение потребления может отражаться на посещаемости и структуре заказов.",
    alt: "Локальный трафик, сезонность и новая аудитория могут компенсировать общий тренд.",
    missing:
      "Сравните посещаемость и средний чек отдельно, по одинаковым дням недели.",
    next: "Проверить средний чек и посещаемость за четыре недели",
    priority: 94,
  },
  software: {
    id: "software",
    title: "Бюджеты ваших заказчиков",
    metric: "Продажи",
    icon: "context",
    type: "risk",
    horizon: "При продлении контрактов",
    topics: ["software", "demand"],
    sectors: ["software"],
    why: "Снижение потребительского спроса может косвенно менять ИТ-бюджеты клиентов из торговли.",
    chain: [
      "Продажи клиентов из торговли",
      "Приоритеты их ИТ-бюджетов",
      "Продление ваших контрактов",
    ],
    impact:
      "Клиенты могут переносить новые проекты, сохраняя расходы на поддержку и автоматизацию.",
    alt: "Спрос на автоматизацию может, наоборот, вырасти при стремлении клиентов сократить издержки.",
    missing:
      "Уточните, есть ли среди клиентов розничные компании и когда продлеваются договоры.",
    next: "Сверить планы продления и новые проекты клиентов",
    priority: 92,
  },
  logistics: {
    id: "logistics",
    title: "Устойчивость поставок",
    metric: "Сроки",
    icon: "clock",
    type: "risk",
    horizon: "До следующей поставки",
    topics: ["logistics"],
    sectors: ["packaging", "restaurant", "other"],
    why: "Задержка у поставщика может отразиться на вашей работе через запас и сроки.",
    chain: ["Маршрут поставщика", "Срок поступления", "Непрерывность работы"],
    impact:
      "Возможна задержка материалов, если поставщик использует затронутый маршрут.",
    alt: "Другой маршрут или достаточный запас могут снять срочность.",
    missing: "Маршрут и дата конкретной поставки пока не подтверждены.",
    next: "Уточнить маршрут и срок критичной поставки",
    priority: 68,
  },
  opportunity: {
    id: "opportunity",
    title: "Проверить спрос на замену поставщика",
    metric: "Новые продажи",
    icon: "arrows",
    type: "opportunity",
    horizon: "Ближайшие 2–3 месяца",
    topics: ["packaging", "demand"],
    sectors: ["packaging"],
    why: "Пересмотр затрат у покупателей может открыть возможность для нового предложения.",
    chain: [
      "Пересмотр закупок у покупателя",
      "Поиск более выгодных условий",
      "Тестовая поставка вашей компании",
    ],
    impact:
      "Предложение с подходящими сроками и небольшими партиями может быть интересно новым покупателям.",
    alt: "Снижение общего спроса может ограничить эффект, даже если ваше предложение выгодно.",
    missing:
      "Это поисковая гипотеза. Подтверждённых запросов и расчёта свободной мощности нет.",
    next: "Проверить интерес одного потенциального покупателя",
    priority: 55,
  },
};
export const recipes = Object.values(R);
export function sectorFor(company) {
  const a = company.facts.activity.toLowerCase();
  if (/программ|разработ|информац/.test(a)) return "software";
  if (/ресторан|кафе|питан|общепит/.test(a)) return "restaurant";
  if (/упаков|картон|бумаг|тар[аыу]/.test(a)) return "packaging";
  return "other";
}
export function relevant(state, id) {
  const r = R[id];
  return activeSources(state).filter((s) =>
    s.applies?.some((t) => r.topics.includes(t)),
  );
}
export function assess(state, id, scenario = null) {
  const r = R[id];
  if (!r) return null;
  const c = state.company,
    f = { ...c.facts, ...(scenario || {}) },
    sec = sectorFor({ ...c, facts: f }),
    src = r.sectors.includes(sec) ? relevant(state, id) : [],
    observations = src.filter((s) =>
      ["Наблюдение", "Официальное сообщение"].includes(s.kind),
    );
  let next = r.next,
    impact = r.impact,
    missing = r.missing,
    status = "Нужно уточнить",
    score = r.priority,
    why = r.why;
  if (!r.sectors.includes(sec)) {
    status = "Связь не подтверждена";
    impact =
      "Текущая деятельность компании не подтверждает эту отраслевую связь.";
    missing =
      "Для возврата к оценке уточните отрасль, покупателей или зависимости компании.";
    score = 5;
  } else if (id === "packaging") {
    if (f.food === "no") {
      status = "Связь исключена";
      impact =
        "По вашему уточнению пищевых предприятий среди покупателей нет. Этот канал влияния исключён.";
      score = 3;
      next = "Проверить другие группы покупателей";
      missing =
        "Проверьте другие группы покупателей, если они чувствительны к потреблению.";
    }
    if (f.food === "yes") {
      status = "Проверить заказы";
      impact =
        "Вы подтвердили покупателей из пищевой отрасли. Возможное влияние на заказы стоит проверить по их закупочным планам.";
      missing = "Принадлежность к отрасли ещё не подтверждает падение заказов.";
      score += 5;
    }
  } else if (id === "raw") {
    if (f.fixed === "yes") {
      status = "Влияние отложено";
      impact =
        "Цена по вашему уточнению зафиксирована. Рыночный рост не меняет текущую закупочную цену до пересмотра договора.";
      score -= 35;
      missing = "Проверьте дату окончания фиксации и условия следующей партии.";
    }
    if (f.fixed === "no") {
      status = "Проверить закупку";
      impact =
        "Цена не зафиксирована. При следующей закупке возможен рост затрат; размер эффекта зависит от состава закупок.";
      score += 5;
    }
  } else if (id === "finance") {
    if (f.debt === "no" && f.plannedBorrowing === "no") {
      status = "Низкая срочность";
      impact =
        "Вы указали отсутствие долга и планов заимствования. Прямой канал процентных расходов сейчас не подтверждён.";
      score = 15;
      next = "Вернуться к оценке при планировании займа";
      missing =
        "Пересмотрите условия, если появится долг или план нового заимствования.";
    } else if (f.debt === "no") {
      status = "Проверить планы";
      impact =
        "Вы указали отсутствие действующего долга. Влияние ставки зависит от будущего заимствования, а не от прежнего типа ставки.";
      score = f.plannedBorrowing === "yes" ? 80 : 35;
      next = "Проверить условия планируемого займа";
      missing =
        "Уточните потребность, срок и предполагаемые условия нового финансирования.";
    } else if (f.rateType === "fixed" && f.plannedBorrowing === "no") {
      status = "Влияние отложено";
      impact =
        "Для фиксированной ставки прямое изменение процентов до пересмотра условий не ожидается. Учитывайте дату рефинансирования.";
      score = 28;
      missing =
        "Уточните ближайшую дату пересмотра условий и потребность в новом финансировании.";
    } else if (f.rateType === "floating") {
      status = "Проверить договор";
      impact =
        "Вы указали плавающую ставку. Проверьте привязку, дату пересмотра и сценарий процентных платежей.";
      score = 96;
      missing =
        "Проверьте формулу привязки, дату пересмотра и чувствительность платежей к ставке.";
    }
  }
  if (id === "software" && f.retailClients === "no") {
    status = "Связь не подтверждена";
    impact =
      "Вы указали, что клиентов из розничной торговли нет. Косвенный канал через потребление для этих заказчиков не подтверждён.";
    missing = "Проверьте другие отрасли клиентов и сроки продления договоров.";
    score = 15;
  }
  if (id === "software" && f.retailClients === "yes") {
    status = "Проверить продления";
    impact =
      "Вы подтвердили клиентов из торговли. Проверьте планы новых проектов отдельно от поддержки и автоматизации.";
    missing =
      "Отраслевой прогноз не доказывает сокращение ИТ-бюджетов конкретного клиента.";
  }
  if (id === "demand" && f.visits === "down") {
    status = "Проверить динамику";
    impact =
      "Вы указали снижение посещаемости за четыре недели. Отделите эффект трафика от среднего чека и локальной сезонности.";
    missing =
      "Уточните сопоставимый период и структуру заказов; причина снижения пока не установлена.";
  }
  if (id === "demand" && f.visits === "up") {
    status = "Сравнить со своим трендом";
    impact =
      "Вы указали рост посещаемости. Общий прогноз охлаждения потребления пока не подтверждается вашим трафиком.";
    score = 35;
    missing = "Проверьте средний чек, промо и сопоставимость периода.";
  }
  if (id === "opportunity" && f.freeCapacity === "no") {
    status = "Ограничение мощности";
    impact =
      "Вы указали отсутствие свободных мощностей. Привлечение новых заказов требует проверки загрузки и экономики расширения.";
    score = 10;
    missing =
      "Сначала проверьте производственный резерв и маржу текущих заказов.";
  }
  if (id === "raw" && f.stock === "more") {
    score = Math.max(5, score - 10);
    missing +=
      " Вы указали запас более месяца: проверьте, какие позиции он покрывает.";
  }
  if (id === "logistics" && f.importShare === "no") {
    score = 20;
    impact =
      "Вы указали отсутствие зависимости от импорта. Проверьте внутренние маршруты отдельно: иностранная задержка сама по себе не подтверждает ваш риск.";
  }
  if (!src.length && r.sectors.includes(sec)) {
    status = "Недостаточно оснований";
    impact =
      "Все подходящие материалы исключены или ещё не загружены. Текущего основания для вывода нет.";
    score = 0;
    next = "Добавить или восстановить основания";
  }
  const bank = src.filter((s) => s.bank),
    groups = [
      ...new Set(src.map((s) => s.independence || s.publisher || s.id)),
    ];
  const caution = src.some(
    (s) =>
      !s.synthetic &&
      !s.bank &&
      !["Наблюдение", "Официальное сообщение"].includes(s.kind),
  );
  const dependencies = {
    packaging: ["food"],
    raw: ["fixed", "stock"],
    finance: ["debt", "rateType", "refinance", "plannedBorrowing"],
    demand: ["visits", "checkTrend"],
    software: ["retailClients", "contractReview"],
    logistics: ["importShare", "stock"],
    opportunity: ["food", "freeCapacity"],
  };
  const context = Object.fromEntries(
    ["activity", "region", ...(dependencies[id] || [])].map((k) => [k, f[k]]),
  );
  const basis = {
    recipe: id,
    context,
    sector: sec,
    sources: src
      .map((s) => ({
        id: s.id,
        hash: hash([s.text, s.kind, s.author, s.publishedAt, s.eventAt]),
        excerpt: s.text.slice(0, 2000),
        group: s.independence || s.publisher,
      }))
      .sort((a, b) => a.id.localeCompare(b.id)),
  };
  return {
    ...r,
    next,
    impact,
    missing,
    status,
    score,
    why,
    sector: sec,
    sources: src,
    bank,
    groups: groups.length,
    observations: observations.length,
    confidence: !src.length
      ? "Нет оснований"
      : observations.length
        ? "Сценарная оценка"
        : "Только прогнозы и мнения",
    caution,
    signature: hash(basis),
    snapshot: basis,
  };
}
export function list(state) {
  return recipes
    .filter((r) => r.sectors.includes(sectorFor(state.company)))
    .map((r) => assess(state, r.id))
    .sort((a, b) => b.score - a.score);
}
export function revise(
  state,
  reason = "Ручная проверка",
  ids,
  now = new Date().toISOString(),
) {
  const changes = [];
  state.assessments ||= {};
  state.checks ||= [];
  ids ||= [
    ...new Set([
      ...recipes
        .filter((r) => r.sectors.includes(sectorFor(state.company)))
        .map((r) => r.id),
      ...Object.keys(state.assessments),
    ]),
  ];
  for (const id of ids) {
    const a = assess(state, id);
    if (!a) continue;
    const old = state.assessments[id];
    if (!old || old.signature !== a.signature) {
      const version = (old?.version || 0) + 1;
      const entry = {
        id: "v-" + id + "-" + version + "-" + hash(now),
        caseId: id,
        version,
        at: now,
        reason,
        signature: a.signature,
        title: a.title,
        status: a.status,
        impact: a.impact,
        snapshot: clone(a.snapshot),
        previousStatus: old?.status || null,
      };
      state.history.unshift(entry);
      state.assessments[id] = { ...entry, checkedAt: now };
      changes.push(entry);
    } else state.assessments[id].checkedAt = now;
  }
  state.history = state.history.slice(0, 200);
  state.checks.unshift({ at: now, reason, changes: changes.length });
  state.checks = state.checks.slice(0, 100);
  const notifyChanges = changes.filter(
    (c) => (state.notify?.[c.caseId] || "important") === "important",
  );
  if (notifyChanges.length && reason !== "Первый расчёт")
    state.notifications.unshift({
      id: "n-" + hash(now + reason),
      at: now,
      title: "Пересмотрены оценки: " + notifyChanges.length,
      text: reason,
      read: false,
      caseId: notifyChanges[0].caseId,
    });
  state.notifications = state.notifications.slice(0, 100);
  return changes;
}
export function initial(seed) {
  const all = {
    schema: 6,
    active: seed.companies[0].id,
    companies: {},
    sources: clone(seed.sources),
    auto: true,
    pet: true,
    role: "owner",
    lastFeed: null,
  };
  for (const c of seed.companies) {
    const s = {
      company: clone(c),
      assessments: {},
      history: [],
      checks: [],
      watch: [],
      hidden: [],
      actions: {},
      scenarios: {},
      notifications: [],
      research: [],
      messages: [],
      feedback: [],
    };
    s.sources = all.sources;
    revise(s, "Первый расчёт", undefined, "2026-10-21T12:00:00Z");
    delete s.sources;
    all.companies[c.id] = s;
  }
  return all;
}
export function events(state) {
  const out = new Map();
  for (const s of activeSources(state)) {
    const key =
      s.eventId || hash(s.title.toLowerCase().replace(/[^a-zа-яё0-9]/g, ""));
    if (!out.has(key))
      out.set(key, {
        id: key,
        title: s.title,
        date: s.eventAt || s.publishedAt,
        dateBasis: s.eventAt ? "Дата события" : "Дата публикации",
        sources: [],
        applies: s.applies || [],
        synthetic: s.synthetic,
        kind: s.kind,
        text: s.text,
      });
    const e = out.get(key);
    e.sources.push(s);
    e.applies = [...new Set([...e.applies, ...(s.applies || [])])];
  }
  return [...out.values()].sort((a, b) => b.date.localeCompare(a.date));
}
export function fingerprintSource(s) {
  return hash([s.url, s.title, s.text, s.publishedAt]);
}
export function mergeSources(current, incoming) {
  const seen = new Set(current.map(fingerprintSource)),
    urls = new Map(current.filter((s) => s.url).map((s) => [s.url, s]));
  let added = 0,
    updated = 0;
  for (const src of incoming) {
    if (seen.has(fingerprintSource(src))) continue;
    const old = src.url && urls.get(src.url);
    if (old) {
      const excluded = old.excluded;
      Object.assign(old, src, { id: old.id, excluded });
      updated++;
    } else {
      current.push({ ...src, id: src.id || "s-" + fingerprintSource(src) });
      added++;
    }
    seen.add(fingerprintSource(src));
  }
  return { added, updated };
}
export function validateFacts(f) {
  if (!String(f.activity || "").trim() || String(f.activity).length > 500)
    throw Error("Укажите деятельность до 500 знаков.");
  if (!String(f.region || "").trim()) throw Error("Укажите регион.");
  for (const k of [
    "food",
    "fixed",
    "debt",
    "plannedBorrowing",
    "retailClients",
    "freeCapacity",
    "importShare",
  ])
    if (!["yes", "no", "unknown"].includes(f[k]))
      throw Error("Проверьте выбранные значения.");
  if (!["fixed", "floating", "unknown"].includes(f.rateType))
    throw Error("Укажите тип ставки.");
  if (f.refinance && !/^\d{4}-\d{2}-\d{2}$/.test(f.refinance))
    throw Error("Укажите дату в формате ГГГГ-ММ-ДД.");
  if (f.refinance && !validDate(f.refinance))
    throw Error("Дата не существует.");
  return f;
}
export function updateFacts(
  state,
  patch,
  reason,
  now = new Date().toISOString(),
) {
  const next = validateFacts({ ...state.company.facts, ...patch });
  const keys = Object.keys(patch).filter(
    (k) => state.company.facts[k] !== next[k],
  );
  if (!keys.length) return [];
  for (const k of keys) {
    state.company.factHistory.unshift({
      key: k,
      previous: state.company.facts[k],
      value: next[k],
      at: now,
      origin: "Уточнение пользователя",
    });
    state.company.confirmed[k] = { at: now, origin: "Уточнение пользователя" };
  }
  state.company.facts = next;
  return revise(state, reason, undefined, now);
}
export function validDate(d) {
  const x = new Date(d + "T12:00:00Z");
  return (
    /^\d{4}-\d{2}-\d{2}$/.test(d) &&
    !isNaN(x) &&
    x.toISOString().slice(0, 10) === d
  );
}
export const addDays = (d, n) =>
  new Date(new Date(d + "T12:00:00Z").getTime() + n * 864e5)
    .toISOString()
    .slice(0, 10);
export function daily(company) {
  const rows = [],
    sec = sectorFor(company),
    shift = sec === "software" ? 0.6 : sec === "restaurant" ? 1.2 : 1;
  for (let d = "2026-04-01", i = 0; d <= TODAY; d = addDays(d, 1), i++) {
    const week = new Date(d + "T12:00:00Z").getUTCDay(),
      season = Math.sin(i / 6) * 2,
      price = 100 + i * 0.06 + Math.sin(i / 11) * 1.4,
      market = 108 - i * 0.055 + Math.cos(i / 8) * 2;
    const orders = Math.max(
      1,
      Math.round((market + season + (week === 0 ? -9 : 0)) * shift),
    );
    const revenue = Math.round(orders * (98 + Math.sin(i / 17)));
    const cost = Math.round(revenue * (0.67 + i * 0.0002) + price * 12 * shift);
    rows.push({
      date: d,
      orders,
      revenue,
      cost,
      price: Math.round(price * 10) / 10,
      market: Math.round(market * 10) / 10,
    });
  }
  return rows;
}
const sum = (r, k) => r.reduce((v, x) => v + x[k], 0);
export function pearson(x, y) {
  if (x.length !== y.length || x.length < 3) return null;
  const n = x.length,
    mx = x.reduce((a, b) => a + b, 0) / n,
    my = y.reduce((a, b) => a + b, 0) / n;
  let c = 0,
    a = 0,
    b = 0;
  for (let i = 0; i < n; i++) {
    c += (x[i] - mx) * (y[i] - my);
    a += (x[i] - mx) ** 2;
    b += (y[i] - my) ** 2;
  }
  return a && b ? c / Math.sqrt(a * b) : null;
}
export function research(state, input) {
  const { start, end, question = "" } = input;
  if (!validDate(start) || !validDate(end) || start > end)
    throw Error("Начало периода должно быть раньше окончания.");
  if (start < "2026-04-01" || end > TODAY)
    throw Error("Для расчётов доступны дни с 1 апреля по 22 октября 2026.");
  const data = daily(state.company),
    rows = data.filter((r) => r.date >= start && r.date <= end),
    n = rows.length,
    prevEnd = addDays(start, -1),
    prevStart = addDays(start, -n),
    baseline = data.filter((r) => r.date >= prevStart && r.date <= prevEnd),
    complete = baseline.length === n;
  const orders = sum(rows, "orders"),
    prev = complete ? sum(baseline, "orders") : null,
    delta = prev ? 100 * (orders / prev - 1) : null,
    revenue = sum(rows, "revenue"),
    cost = sum(rows, "cost"),
    margin = revenue ? (100 * (revenue - cost)) / revenue : null;
  const materialCorrelation = pearson(
    rows.map((r) => r.price),
    rows.map((r) => (100 * r.cost) / r.revenue),
  );
  const candidates = [];
  for (const lag of [0, 3, 7, 14])
    if (rows.length - lag >= 14) {
      const a = rows.slice(0, rows.length - lag).map((r) => r.market),
        b = rows.slice(lag).map((r) => r.orders);
      const r = pearson(a, b);
      if (r !== null) candidates.push({ lag, r, n: a.length });
    }
  candidates.sort((a, b) => Math.abs(b.r) - Math.abs(a.r));
  const ev = events(state).filter((e) => e.date >= start && e.date <= end),
    supported =
      !question ||
      /измен|заказ|марж|закономер|связ|сырь|затрат|спрос|событ|период|динамик|цен|выруч|паралл|риск/.test(
        question.toLowerCase(),
      );
  return {
    id: "r-" + Date.now() + "-" + hash(input),
    ...input,
    createdAt: new Date().toISOString(),
    rows,
    baseline: complete ? baseline : [],
    complete,
    delta,
    orders,
    margin,
    materialCorrelation: n >= 14 ? materialCorrelation : null,
    bestLag: candidates[0] || null,
    events: ev.map((e) => ({
      id: e.id,
      title: e.title,
      date: e.date,
      sources: e.sources.map((s) => ({
        id: s.id,
        title: s.title,
        textHash: hash(s.text),
      })),
    })),
    context: clone(state.company),
    sourceSnapshot: activeSources(state).map((s) => ({
      id: s.id,
      hash: hash(s.text),
    })),
    supported,
    coverage: {
      days: n,
      events: ev.length,
      sources: new Set(ev.flatMap((e) => e.sources.map((s) => s.id))).size,
    },
    limits:
      "Показатели компании синтетические и не взяты из банковских операций. Корреляции на них демонстрируют метод, а не закономерность вашего бизнеса. Перебор лагов требует проверки на отдельном периоде. Совпадение с публикацией не доказывает причинную связь.",
  };
}
export function digest(state, period = "day") {
  const now = new Date(),
    cutoff = now.getTime() - (period === "week" ? 7 : 1) * 864e5;
  const changes = state.history.filter(
    (h) => new Date(h.at).getTime() >= cutoff && h.reason !== "Первый расчёт",
  );
  const cards = list(state).filter((a) => !state.hidden.includes(a.id));
  return {
    at: now.toISOString(),
    period,
    changes,
    items: cards.slice(0, 3),
    actions: Object.entries(state.actions).filter(
      ([, a]) => a.status !== "done",
    ),
    events: events(state).filter(
      (e) => new Date(e.date + "T12:00:00Z").getTime() >= cutoff,
    ),
    checked: state.checks[0]?.at,
  };
}

export function contextSources(state, limit = 35) {
  const active = activeSources(state);
  return [
    ...active.filter((s) => s.bank),
    ...active
      .filter((s) => !s.bank)
      .sort((a, b) => b.publishedAt.localeCompare(a.publishedAt)),
  ].slice(0, limit);
}
