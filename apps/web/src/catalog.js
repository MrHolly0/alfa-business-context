// Presentation queries. Scores order a work queue; they are not probabilities.
export const domains = [
  {
    id: "sales",
    label: "Спрос и клиенты",
    icon: "people",
    topics: ["packaging", "demand", "software"],
  },
  {
    id: "procurement",
    label: "Закупки и затраты",
    icon: "package",
    topics: ["raw", "energy"],
  },
  { id: "finance", label: "Финансирование", icon: "card", topics: ["finance"] },
  {
    id: "operations",
    label: "Поставки и работа",
    icon: "clock",
    topics: ["logistics"],
  },
  { id: "growth", label: "Возможности", icon: "trend", topics: [] },
  { id: "context", label: "Общий контекст", icon: "file", topics: [] },
];
export const views = [
  ["all", "Все ситуации"],
  ["attention", "На проверку"],
  ["working", "В работе"],
  ["watch", "Наблюдения"],
  ["deferred", "Отложено"],
  ["archive", "Архив"],
];
export function domainOf(item) {
  if (item.type === "opportunity") return "growth";
  return (
    domains.find((d) => d.topics.includes(item.id))?.id ||
    domains.find((d) =>
      d.topics.some((t) => (item.topics || item.applies || []).includes(t)),
    )?.id ||
    "context"
  );
}
export function inDomain(item, domain) {
  if (!domain || domain === "all") return true;
  // Each case appears once in its primary domain. Events can concern several domains.
  if (item.metric) return domainOf(item) === domain;
  const d = domains.find((d) => d.id === domain);
  return domain === "context"
    ? domainOf(item) === "context"
    : d?.topics.some((t) => item.applies?.includes(t));
}
export function bucket(item, state) {
  const task = state.actions?.[item.id];
  if (state.hidden?.includes(item.id) || task?.status === "done")
    return "archive";
  if (task?.status === "review") return "working";
  if (task?.status === "deferred") return "deferred";
  if (state.watch?.includes(item.id) || task?.status === "watch")
    return "watch";
  return "attention";
}
// A situation has one work state. The legacy watch array is kept in sync for
// existing exports and assistant context; it is no longer an independent flag.
export function setWorkStatus(state, id, status, version, at = new Date().toISOString()) {
  if (!["todo", "review", "watch", "deferred", "done"].includes(status)) return;
  state.actions ||= {};
  state.actions[id] = { ...state.actions[id], status, at, basisVersion: version };
  state.watch = (state.watch || []).filter((x) => x !== id);
  if (status === "watch") state.watch.push(id);
  if (status !== "done") state.hidden = (state.hidden || []).filter((x) => x !== id);
}
const unique = (items) => [...new Map(items.map((i) => [i.id, i])).values()];
export function paginate(items, page = 0, size = 10) {
  const total = items.length,
    pages = Math.max(1, Math.ceil(total / size));
  const current = Math.max(
    0,
    Math.min(pages - 1, Number.isFinite(+page) ? Math.floor(+page) : 0),
  );
  return {
    items: items.slice(current * size, (current + 1) * size),
    total,
    page: current,
    pages,
    size,
  };
}
const includes = (value, query) =>
  String(value || "")
    .toLocaleLowerCase("ru")
    .includes(query);
export function caseCatalog(items, state, options = {}) {
  const {
    view = "all",
    domain = "all",
    query = "",
    type = "all",
    sort = "priority",
    page = 0,
    size = 10,
  } = options;
  const all = unique(items);
  const counts = Object.fromEntries(
    views.map(([key]) => [
      key,
      key === "all"
        ? all.length
        : all.filter((i) => bucket(i, state) === key).length,
    ]),
  );
  const selected = all.filter(
    (i) =>
      (view === "all" || bucket(i, state) === view) &&
      (type === "all" || type === i.type) &&
      includes(
        [i.title, i.metric, i.impact, i.next].join(" "),
        query.trim().toLocaleLowerCase("ru"),
      ),
  );
  const facets = Object.fromEntries(
    domains.map((d) => [
      d.id,
      selected.filter((i) => inDomain(i, d.id)).length,
    ]),
  );
  let filtered = selected.filter((i) => inDomain(i, domain));
  filtered.sort((a, b) => {
    if (sort === "due") {
      const ad = state.actions?.[a.id]?.due || "9999",
        bd = state.actions?.[b.id]?.due || "9999";
      if (ad !== bd) return ad.localeCompare(bd);
    }
    if (sort === "updated") {
      const at = state.assessments?.[a.id]?.checkedAt || "",
        bt = state.assessments?.[b.id]?.checkedAt || "";
      if (at !== bt) return bt.localeCompare(at);
    }
    return b.score - a.score || a.id.localeCompare(b.id);
  });
  return {
    ...paginate(filtered, page, size),
    counts,
    facets,
    matching: selected.length,
  };
}
export const eventSignature = (event) =>
  JSON.stringify(
    event.sources.map((s) => [s.id, s.text, s.kind, s.publishedAt]).sort(),
  );
export const unread = (event, seen = {}) =>
  seen[event.id] !== eventSignature(event);
export function eventCatalog(events, options = {}) {
  const {
    domain = "all",
    query = "",
    kind = "all",
    cutoff = "",
    onlyNew = false,
    seen = {},
    relevantTopics = null,
    page = 0,
    size = 10,
  } = options;
  const q = query.trim().toLocaleLowerCase("ru");
  const selected = unique(events).filter(
    (e) =>
      (!cutoff || e.date >= cutoff) &&
      (!relevantTopics || e.applies.some((t) => relevantTopics.includes(t))) &&
      (!onlyNew || unread(e, seen)) &&
      (kind === "all" ||
        (kind === "forecast" ? /Прогноз/.test(e.kind) : e.kind === kind)) &&
      includes(
        [
          e.title,
          e.text,
          ...e.sources.map((s) => [s.publisher, s.author].join(" ")),
        ].join(" "),
        q,
      ),
  );
  const facets = Object.fromEntries(
    domains
      .filter((d) => d.id !== "growth")
      .map((d) => [d.id, selected.filter((e) => inDomain(e, d.id)).length]),
  );
  const filtered = selected
    .filter((e) => inDomain(e, domain))
    .sort((a, b) => b.date.localeCompare(a.date) || a.id.localeCompare(b.id));
  return {
    ...paginate(filtered, page, size),
    facets,
    matching: selected.length,
  };
}
