import { createPulse } from "./pulse.js";
import { seed } from "./seed.js";
import * as E from "./engine.js";
import { icons } from "./icons.js";
import { createUI } from "./ui.js";
import * as C from "./catalog.js";
import { contextualProduct } from "./bank-products.js";
import { volumeSources } from "./volume-demo.js";
import { renderMarkup } from "./core-ui.jsx";
import { createCoreClient, CoreApiError } from "./core-client.js";
const $ = (s) => document.querySelector(s),
  esc = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const I = (n) =>
  `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[n] || icons.info}</svg>`;
const B = (label, action, kind = "secondary", icon = "", extra = "") =>
  `<button type="button" class="btn ${kind}" data-action="${esc(action)}" ${extra}>${icon ? I(icon) : ""}${label}</button>`;
const iconButton = (icon, action, label, extra = "") =>
  `<button type="button" class="iconbtn" data-action="${esc(action)}" aria-label="${esc(label)}" ${extra}>${I(icon)}</button>`;
const date = (d) =>
  d
    ? new Date(d.slice(0, 10) + "T12:00:00Z")
        .toLocaleDateString("ru-RU", {
          day: "numeric",
          month: "short",
          year: "numeric",
        })
        .replace(/\sг\.$/, "")
    : "Не указана";
const time = (d) =>
  d
    ? new Date(d).toLocaleString("ru-RU", {
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Europe/Moscow",
      }) + " МСК"
    : "Ещё не проверяли";
const num = (n, d = 0) =>
  n === null || !Number.isFinite(n)
    ? "—"
    : n.toLocaleString("ru-RU", {
        maximumFractionDigits: d,
        minimumFractionDigits: d,
      });
const trends = [
  ["unknown", "Не проверяли"],
  ["down", "Снижается"],
  ["stable", "Без заметного изменения"],
  ["up", "Растёт"],
];
const yesno = [
    ["unknown", "Неизвестно"],
    ["yes", "Да"],
    ["no", "Нет"],
  ],
  yn = (v) =>
    ({ yes: "Да", no: "Нет", unknown: "Не уточнено" })[v] || "Не уточнено";
const fieldLabels = {
  retailClients: "Есть клиенты из розничной торговли",
  contractReview: "Ближайшее продление договора с клиентом",
  visits: "Посещаемость за последние четыре недели",
  checkTrend: "Средний чек за последние четыре недели",
  freeCapacity: "Есть свободные мощности",
  activity: "Деятельность",
  region: "Регион",
  food: "Есть покупатели из пищевой отрасли",
  fixed: "Закупочная цена зафиксирована",
  stock: "Запас материалов",
  debt: "Есть действующий долг",
  rateType: "Тип ставки по долгу",
  refinance: "Дата пересмотра / рефинансирования",
  plannedBorrowing: "Планируется новое заимствование",
  importShare: "Зависимость от импортных поставок",
};
let root;
try {
  root = JSON.parse(localStorage.getItem("business-context-v6"));
} catch {}
if (root?.schema !== 6 || !root.companies?.[root.active]) {
  root = E.initial(seed);
  try {
    const old = JSON.parse(localStorage.getItem("business-context-v5"));
    if (old) {
      const c = root.companies[root.active];
      c.watch = (old.watch || []).filter((x) =>
        E.recipes.some((r) => r.id === x),
      );
      root.pet = old.pet !== false;
      for (const k of ["food", "fixed"])
        if (["yes", "no", "unknown"].includes(old.company?.[k]))
          c.company.facts[k] = old.company[k];
    }
  } catch {}
}
let demoSignedIn = false;
try {
  demoSignedIn = sessionStorage.getItem("business-context-demo-session-v2") === "1";
} catch {}
const coreClient = createCoreClient();
let coreMode = false,
  coreUser = null,
  coreCompanies = [],
  coreCompany = null,
  coreContext = null,
  coreDashboardData = null,
  coreMetrics = [],
  coreNewsSources = [],
  coreNewsDocuments = [],
  coreMetricEvents = [],
  coreMetricEventsId = "",
  coreMetricEventsLoading = false,
  coreCompanyGeneration = 0,
  coreBusy = false,
  coreError = "";
let state,
  routeKey = "",
  view = { page: "overview" },
  assistant = false,
  expanded = false,
  mobile = false,
  busy = null,
  error = "",
  draft = {},
  returnTo = "overview",
  detailReturn = "overview",
  query = "",
  filter = "all",
  onlyWatch = false,
  showAll = false,
  eventQuery = "",
  eventPeriod = "all",
  eventKind = "all",
  eventPage = 0,
  inventoryDomain = "all",
  inventoryType = "all",
  inventorySort = "priority",
  inventoryPage = 0,
  eventDomain = "all",
  eventOnlyNew = false,
  advancedNewsFilters = false,
  volumeSeen = {},
  sourceFilter = "all",
  sourceQuery = "",
  sourcePage = 0,
  sourceStage = "link",
  sourceImport = null,
  newsReturn = "events",
  digestPeriod = "day",
  lastResult = "",
  integration = { news: false, import: false, analyze: false, llm: false },
  chatBusy = false;
const UI = createUI({ I, esc, act: perform });
const listMemory = new Map();
function sync() {
  root.profile ||= { firstName: "Анна", lastName: "Волкова" };
  state = root.companies[root.active];
  state.sources = root.sources;
  state.company.facts = {
    retailClients: "unknown",
    contractReview: "",
    visits: "unknown",
    checkTrend: "unknown",
    freeCapacity: "unknown",
    ...state.company.facts,
  };
  state.company.confirmed ||= {};
  state.company.factHistory ||= [];
  state.scenarios ||= {};
  state.messages ||= [];
  state.actions ||= {};
  state.viewedVersions ||= {};
  state.seenEvents ||= {};
  state.dismissedOffers ||= {};
}
sync();
function persist() {
  try {
    const copy = {
      ...root,
      companies: Object.fromEntries(
        Object.entries(root.companies).map(([id, c]) => {
          const { sources, ...v } = c;
          return [id, v];
        }),
      ),
    };
    localStorage.setItem("business-context-v6", JSON.stringify(copy));
  } catch {
    toast("Память браузера заполнена. Экспортируйте важные результаты.");
  }
}
function toast(text, action = "", label = "Отменить") {
  const el = $("#toast");
  el.innerHTML = `<span>${esc(text)}</span>${action ? `<button data-action="${esc(action)}">${label}</button>` : ""}`;
  el.classList.add("visible");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("visible"), 6500);
}
function navigate(path) {
  UI.close();
  if (location.hash === "#" + path) {
    route();
    return;
  }
  location.hash = path;
}
function route() {
  if (routeKey && ["situations", "events", "sources"].includes(view.page))
    listMemory.set(routeKey, {
      page:
        view.page === "situations"
          ? inventoryPage
          : view.page === "events"
            ? eventPage
            : sourcePage,
      scroll: window.scrollY,
    });
  if (!coreMode && view.page === "news" && view.id) markEventSeen(view.id, view.scope === "volume");
  const key = decodeURI(location.hash.slice(1) || "home"),
    parts = key.split("/"),
    previous = view.page;
  if ((demoSignedIn || coreMode) && key === "login") {
    location.hash = "home";
    return;
  }
  if (parts[0] === "news" && previous === "events") newsReturn = routeKey;
  if (
    parts[0] === "detail" &&
    ["overview", "situations", "events", "news", "digest", "actions"].includes(previous)
  )
    detailReturn = routeKey || "overview";
  view = {
    page: parts[0],
    id: parts[1],
    tab: parts[2] || "analysis",
    scope: parts[2],
  };
  if (key !== routeKey) {
    if (view.page === "situations")
      inventoryPage = listMemory.get(key)?.page || 0;
    if (view.page === "events") {
      eventPage = listMemory.get(key)?.page || 0;
    }
    if (!coreMode && view.page === "detail") {
      const assessment = E.assess(state, view.id);
      if (assessment) {
        state.viewedVersions[view.id] = assessment.signature;
        persist();
      }
    }
    error = "";
    if (
      ["context", "source-new", "research-new", "feedback", "account"].includes(view.page)
    ) {
      returnTo =
        previous === "detail"
          ? routeKey
          : previous === "sources"
            ? "sources"
            : previous === "research"
              ? "research"
              : routeKey || "overview";
      if (!coreMode && view.page === "context")
        draft = {
          ...state.company.facts,
          ...(view.scope === "scenario" ? state.scenarios[view.id] || {} : {}),
        };
      if (view.page === "source-new") {
        sourceStage = "link";
        sourceImport = null;
        draft = {
          title: "",
          url: "",
          publisher: "",
          author: "",
          publishedAt: "",
          kind: "Не классифицирован",
          text: "",
        };
      }
      if (view.page === "research-new")
        draft = {
          question:
            "Что изменилось в заказах и марже? Есть ли возможные связи с событиями?",
          start: "2026-10-01",
          end: E.TODAY,
          period: "oct",
        };
      if (view.page === "feedback") draft = { type: "wrong", text: "" };
      if (view.page === "account") draft = { ...root.profile };
    }
    routeKey = key;
  }
  UI.close();
  mobile = false;
  render();
  window.scrollTo(0, listMemory.get(key)?.scroll || 0);
  requestAnimationFrame(() => {
    const h = $("h1");
    if (h) {
      h.tabIndex = -1;
      h.focus({ preventScroll: true });
    }
  });
}
function intro(title, description = "", action = "") {
  return `<div class="page-intro"><div><h1>${esc(title)}</h1>${description ? `<p>${esc(description)}</p>` : ""}</div>${action ? `<div class="page-actions">${action}</div>` : ""}</div>`;
}
function back(path = returnTo, label = "Назад") {
  return `<a class="back" href="#${esc(path)}">${I("left")}${esc(label)}</a>`;
}
function empty(title, text, action = "") {
  return `<div class="empty">${I("file")}<h2>${esc(title)}</h2><p>${esc(text)}</p>${action ? `<div class="actions">${action}</div>` : ""}</div>`;
}
const badge = (s, kind = "") => `<span class="badge ${kind}">${esc(s)}</span>`;
const notice = (text, kind = "neutral") =>
  `<div class="notice ${kind}">${I("info")}<div>${text}</div></div>`;
function selectField(name, label, options, value, extra = "") {
  const id = "field-" + name;
  return `<label class="field"><span>${esc(label)}</span><input type="hidden" id="${id}" name="${name}" value="${esc(value)}"><button type="button" class="select-trigger" data-ui="select" data-field="${id}" data-options="${esc(JSON.stringify(options))}" role="combobox" aria-haspopup="listbox" aria-expanded="false" aria-label="${esc(label)}" ${extra}><span>${esc(options.find((x) => x[0] === value)?.[1] || "Выберите")}</span>${I("down")}</button></label>`;
}
function textField(
  name,
  label,
  value = "",
  hint = "",
  type = "text",
  extra = "",
) {
  return `<label class="field"><span>${esc(label)}</span><input aria-label="${esc(label)}" name="${name}" type="${type}" value="${esc(value)}" ${extra}>${hint ? `<span class="hint">${esc(hint)}</span>` : ""}</label>`;
}
function dateField(name, label, value, min = "", max = "", scope = "") {
  const id = "field-" + (scope ? scope + "-" : "") + name;
  return `<label class="field"><span>${esc(label)}</span><input type="hidden" name="${name}" id="${id}" value="${esc(value)}"><button type="button" class="date-trigger" data-ui="calendar" data-field="${id}" ${min ? `data-min="${min}"` : ""} ${max ? `data-max="${max}"` : ""} aria-label="${esc(label)}" aria-haspopup="dialog" aria-expanded="false"><span>${value ? date(value) : "Выбрать дату"}</span>${I("calendar")}</button></label>`;
}
function textarea(name, label, value = "", hint = "", extra = "") {
  return `<label class="field"><span>${esc(label)}</span><textarea aria-label="${esc(label)}" name="${name}" ${extra}>${esc(value)}</textarea>${hint ? `<span class="hint">${esc(hint)}</span>` : ""}</label>`;
}
function formError() {
  return error
    ? `<div class="form-error" role="alert">${esc(error)}</div>`
    : "";
}
function applyPublisherBlocks() {
  for (const s of root.sources) {
    const p = (root.excludedPublishers || []).find(
      (p) => p.group === (s.independence || s.publisher),
    );
    if (p && !s.excluded) {
      s.excluded = true;
      s.excludedByPublisher = p.id;
    }
  }
}
function updateAll(reason) {
  let activeChanges = 0;
  for (const s of Object.values(root.companies)) {
    s.sources = root.sources;
    const changes = E.revise(s, reason);
    if (s.company.id === root.active)
      activeChanges = changes.filter((c) =>
        E.recipes
          .find((r) => r.id === c.caseId)
          ?.sectors.includes(E.sectorFor(s.company)),
      ).length;
  }
  persist();
  return activeChanges;
}
function refreshButton(id = "all") {
  const unavailable = !integration.news;
  return B(
    busy
      ? "Проверяем…"
      : '<span class="full-label">Обновить оценки</span><span class="short-label">Обновить</span>',
    "refresh:" + id,
    "quiet",
    busy ? "clock" : "refresh",
    busy || unavailable
      ? `disabled title="${unavailable ? "Загрузка новостей сейчас недоступна." : ""}" aria-label="${unavailable ? "Загрузка новостей сейчас недоступна" : "Обновить оценки"}"`
      : 'aria-label="Обновить оценки"',
  );
}
function jobBanner() {
  return busy
    ? `<div class="job" role="status"><span class="spinner"></span><div>${esc(busy.step)}<div class="hint">Можно перейти в другой раздел. Предыдущие оценки доступны.</div></div>${B("Отменить", "cancel-job", "text")}</div>`
    : "";
}
const pulse = createPulse({ E, C, esc, I, B, state:()=>state, root:()=>root, persist, navigate, render });
function nav(label, icon, path) {
  const active = view.page === path ||
    (path === "overview" && ["digest", "history", "deep"].includes(view.page)) ||
    (path === "situations" && ["detail", "actions", "feedback"].includes(view.page)) ||
    (path === "events" && view.page === "news") ||
    (path === "company" && view.page === "context") ||
    (path === "research" && ["research-new", "report"].includes(view.page));
  return `<a class="navitem ${active ? "active" : ""}" href="#${path}" ${view.page === path ? 'aria-current="page"' : ""}>${I(icon)}<span>${label}</span></a>`;
}
function coreCompanyLabel(company) {
  return company?.synthetic_u || company?.name || company?.id || "Компания";
}
function coreStatusLabel(status) {
  return ({ no_analysis: "Сводка пока не готова", partial: "Нужны уточнения", ready: "Готово", unknown: "Не установлено", observed: "Есть наблюдение", confirmed: "Подтверждено", proposed: "Предложено", blocked: "Проверка ограничена", error: "Не удалось проверить", published: "Опубликовано", failed: "Не удалось проверить", no_data: "Данных нет", not_evaluated: "Не проверено", unavailable: "Недоступно" })[status] || (status ? "Нужны уточнения" : "Не указано");
}
function coreEventKind(kind) {
  return ({ source_document: "Новость", manual: "Добавлено вручную", official: "Официальное сообщение", forecast: "Прогноз", opinion: "Мнение" })[kind] || "Материал";
}
function coreSourceRole(role) {
  return ({ external: "Внешний источник", bank: "Банк", internal: "Внутренний источник", synthetic: "Учебный источник" })[role] || "Тип источника не указан";
}
function coreClassification(value) {
  return ({ public: "Открытый", confidential: "Конфиденциальный", synthetic: "Учебный" })[value] || "Классификация не указана";
}
async function loadCoreCompanies(selectId = null) {
  const sessionUser = coreUser?.id;
  coreBusy = true;
  coreError = "";
  render();
  try {
    const result = await coreClient.companies();
    if (coreUser?.id !== sessionUser) return;
    coreCompanies = result.companies || [];
    const selected = coreCompanies.find((c) => c.id === selectId) || coreCompanies[0] || null;
    if (selected) await loadCoreCompany(selected.id, false);
    else {
      coreCompany = null;
      coreContext = coreDashboardData = null;
      coreMetrics = coreNewsSources = coreNewsDocuments = [];
    }
  } catch (e) {
    coreError = e.message;
  } finally {
    coreBusy = false;
    render();
  }
}
async function loadCoreCompany(id, repaint = true) {
  const generation = ++coreCompanyGeneration;
  if (repaint) {
    coreBusy = true; coreError = "";
    if (coreCompany?.id !== id) {
      coreCompany = null; coreContext = coreDashboardData = null;
      coreMetrics = coreNewsSources = coreNewsDocuments = [];
      coreMetricEvents = []; coreMetricEventsId = ""; coreMetricEventsLoading = false;
    }
    render();
  }
  try {
    const [companyResult, contextResult, metricsResult, dashboardResult, sourcesResult, documentsResult] = await Promise.all([
      coreClient.company(id), coreClient.context(id), coreClient.metrics(id), coreClient.dashboard(id),
      coreClient.newsSources(id), coreClient.newsDocuments(id),
    ]);
    if (generation !== coreCompanyGeneration) return;
    coreCompany = companyResult.company;
    coreContext = contextResult.context;
    coreMetrics = metricsResult.metrics || [];
    coreDashboardData = dashboardResult;
    coreNewsSources = sourcesResult.sources || [];
    coreNewsDocuments = documentsResult.documents || [];
    coreMetricEvents = []; coreMetricEventsId = ""; coreMetricEventsLoading = false;
    coreCompanies = coreCompanies.map((c) => c.id === id ? { ...c, ...coreCompany } : c);
    coreError = "";
  } catch (e) {
    if (generation !== coreCompanyGeneration) return;
    coreError = e.message;
    if (e.status === 401) {
      coreMode = false; coreUser = null; coreCompanies = []; coreCompany = null;
      try { sessionStorage.removeItem("business-context-demo-session-v2"); } catch {}
      error = "Сессия завершилась. Войдите снова.";
    }
  } finally {
    if (generation === coreCompanyGeneration && repaint) { coreBusy = false; render(); }
  }
}
async function loadCoreMetricEvents(metricId) {
  if (!coreCompany || coreMetricEventsId === metricId || coreMetricEventsLoading) return;
  const generation = coreCompanyGeneration;
  coreMetricEventsId = metricId; coreMetricEvents = []; coreMetricEventsLoading = true;
  try {
    const result = await coreClient.metricEvents(coreCompany.id, metricId);
    if (generation !== coreCompanyGeneration || coreMetricEventsId !== metricId) return;
    coreMetricEvents = await Promise.all((result.events || []).map(async (event) => {
      try {
        const response = await coreClient.newsAssessment(coreCompany.id, event.id);
        return { ...event, assessment: response.assessment || null };
      } catch (e) { if (e.status === 401) throw e; return event; }
    }));
  } catch (e) {
    if (generation === coreCompanyGeneration && coreMetricEventsId === metricId) {
      coreError = e.message;
      if (e.status === 401) {
        coreMode = false; coreUser = null; coreCompanies = []; coreCompany = null;
        coreContext = coreDashboardData = null; coreMetrics = coreNewsSources = coreNewsDocuments = [];
        error = "Сессия завершилась. Войдите снова.";
      }
    }
  } finally {
    if (generation === coreCompanyGeneration && coreMetricEventsId === metricId) {
      coreMetricEventsLoading = false; render();
    }
  }
}
function coreMetricForm() {
  const canWrite = coreCompany?.role !== "viewer";
  return `<div class="form-page">${intro("Новый показатель", "Определите показатель компании. Значения не создаются автоматически.")}<form id="core-metric-form" class="card">${formError()}${textField("title","Название","","","text",'required maxlength="160"')}${textarea("description","Описание","", "", 'required maxlength="2000"')}${textField("unit","Единица измерения","","","text",'maxlength="40"')}${selectField("direction","Направление",[["neutral","Без заданного направления"],["up","Рост предпочтителен"],["down","Снижение предпочтительно"]],"neutral")}<div class="actions"><button class="btn primary" type="submit" ${canWrite ? "" : "disabled"}>Создать показатель</button>${B("Отмена","nav:home","secondary")}</div>${canWrite ? "" : '<p class="hint">Роль viewer даёт доступ только для чтения.</p>'}</form></div>`;
}
async function startCoreSession(email, password) {
  coreBusy = true; error = ""; render();
  try {
    const result = await coreClient.login(email, password);
    coreCompanyGeneration++;
    coreCompany = coreContext = coreDashboardData = null;
    coreCompanies = []; coreMetrics = coreNewsSources = coreNewsDocuments = [];
    coreMetricEvents = []; coreMetricEventsId = "";
    coreUser = result.user;
    coreMode = true; demoSignedIn = false;
    try { sessionStorage.removeItem("business-context-demo-session-v2"); } catch {}
    await loadCoreCompanies();
    navigate("home");
  } catch (e) {
    error = e.message;
  } finally {
    coreBusy = false; render();
  }
}
function coreContextForm() {
  const groups = [
    ["activities", "Направления деятельности"], ["products", "Продукты"],
    ["markets", "Рынки"], ["suppliers", "Поставщики"],
    ["customers", "Клиенты"], ["risks", "Риски"], ["notes", "Примечания"],
  ];
  const facts = coreContext?.facts || {};
  const canWrite = coreCompany?.role !== "viewer";
  return `<div class="form-page">${intro("Контекст компании", "Сохраняйте подтверждённые сведения о компании. Каждая строка — отдельное утверждение.")}<form id="core-context-form" class="card">${formError()}${groups.map(([key,label]) => textarea(key,label,(facts[key] || []).join("\n"),"До 30 строк, максимум 300 знаков на строку; не добавляйте предположения как факты.",'maxlength="9000"')).join("")}${canWrite ? `<div class="actions"><button class="btn primary" type="submit" ${coreBusy ? "disabled" : ""}>Сохранить изменения</button>${B("Отмена","nav:company","secondary")}</div>` : '<p class="hint">Роль viewer даёт доступ только для чтения.</p>'}<p class="hint">Текущая версия: ${coreContext?.version ?? 0}. При конфликте версий данные будут перечитаны.</p></form></div>`;
}
function coreCompanyPage() {
  const facts = coreContext?.facts || {};
  const lines = Object.entries(facts).filter(([,items]) => Array.isArray(items) && items.length);
  return `${intro("Моя компания", "Сведения о компании и сохранённый контекст.", B("Уточнить контекст","nav:context","primary","pencil"))}<div class="company-identity"><span class="company-avatar">${I("building")}</span><div><h2>${esc(coreCompanyLabel(coreCompany))}</h2><p>${esc(coreCompany?.mainokved_name || "Основная деятельность не указана")} · ${esc(coreCompany?.region || "Регион не указан")}</p></div></div><section class="profile-groups"><div class="profile-group"><h2>Контекст компании · версия ${coreContext?.version ?? 0}</h2><dl class="profile-facts">${lines.map(([key,items]) => `<div><dt>${esc(({activities:"Направления деятельности",products:"Продукты",markets:"Рынки",suppliers:"Поставщики",customers:"Клиенты",risks:"Риски",notes:"Примечания"})[key] || key)}</dt><dd>${items.map((x) => esc(x)).join("; ")}<span class="fact-origin">Сохранено</span></dd></div>`).join("") || '<div><dt>Контекст</dt><dd>Пока не уточнён</dd></div>'}</dl></div></section><div class="actions">${B("Изменить контекст","nav:context","secondary","pencil")}</div>`;
}
function coreNewsPage() {
  return `${intro("Новости и источники", "Здесь показаны сохранённые материалы. Управление источниками и запуск сбора пока недоступны.")}<section class="card source-directory"><h2>Источники · ${coreNewsSources.length}</h2>${coreNewsSources.map((s) => `<article class="directory-row"><div class="row-grow"><h3>${esc(s.title || s.uri)}</h3><p class="source-meta">${esc(s.publisher || "Издатель не указан")} · ${esc(coreSourceRole(s.source_role))} · ${esc(coreClassification(s.classification))}</p><span class="source-kind">${s.enabled ? "Включён" : "Выключен"}</span></div></article>`).join("") || '<p>Источники не зарегистрированы.</p>'}</section><section class="card source-directory"><h2>Документы · ${coreNewsDocuments.length}</h2>${coreNewsDocuments.map((d) => `<article class="directory-row"><div class="row-grow"><h3>${esc(d.title || d.canonical_uri)}</h3><p class="source-meta">${esc(d.publisher || "Издатель не указан")} · ${esc(d.published_at || "Дата не указана")}</p><p>${esc(d.excerpt || "Текстовый фрагмент не сохранён.")}</p><span class="source-kind">${esc(coreClassification(d.classification))}</span></div></article>`).join("") || '<p>Документы не сохранены.</p>'}</section>`;
}
function coreUnsupportedPage() {
  return empty("Раздел пока недоступен", "Здесь не показываются демоданные. Доступны обзор, профиль компании и сохранённые новости.", B("К обзору","nav:home"));
}
function renderCore() {
  document.title = "Бизнес-пульс · Альфа-Бизнес";
  if (!coreUser) {
    renderMarkup($("#app"), loginPage()); renderAssistant(); return;
  }
  const userName = coreUser.displayName || coreUser.display_name || coreUser.email || "Пользователь";
  let page;
  if (coreBusy && !coreCompany) page = '<div class="card">Загружаем данные компании…</div>';
  else if (coreError && !coreCompany) page = `<div class="card form-error">${esc(coreError)}</div>`;
  else if (!coreCompany) page = `<div class="card">${coreError ? `<p class="form-error">${esc(coreError)}</p>` : ""}<h2>Нет доступных компаний</h2><p>Для этой учётной записи пока нет доступных организаций.</p></div>`;
  else switch (view.page) {
    case "home": case "overview": page = pulse.coreDashboard(coreCompany, coreDashboardData); break;
    case "metric": {
      loadCoreMetricEvents(view.id);
      page = pulse.coreMetricPage(coreMetrics.find((m) => m.id === view.id), coreMetricEventsId === view.id ? coreMetricEvents : [], coreCompany.role !== "viewer");
      break;
    }
    case "metric-new": page = coreMetricForm(); break;
    case "company": page = coreCompanyPage(); break;
    case "context": page = coreContextForm(); break;
    case "events": case "sources": page = coreNewsPage(); break;
    case "news": {
      const event = coreDashboardData?.events?.find((e) => e.id === view.id);
      page = event ? `${back("events","К событиям")}<article class="news-reader"><header><h1>${esc(event.title)}</h1><p>${esc(coreEventKind(event.kind))} · ${esc(event.published_on)}</p></header><div class="reader-body"><p>${esc(event.summary || "Текст события не предоставлен.")}</p></div><section class="reader-source"><p>Статус оценки: ${esc(coreStatusLabel(event.assessment_status))}</p><p>${esc(event.assessment_summary || "Опубликованной оценки пока нет.")}</p></section></article>` : back("events","К событиям") + '<div class="card">Событие не найдено среди сохранённых данных.</div>';
      break;
    }
    case "account": page = `<div class="form-page account-page">${intro("Мой профиль","Данные учётной записи.")}<section class="card account-identity"><div><h2>${esc(userName)}</h2><p>${esc(coreUser.email || "")}</p></div></section><div class="actions">${B("Выйти","core-logout","secondary")}</div></div>`; break;
    default: page = coreUnsupportedPage();
  }
  const companyTitle = coreCompany ? coreCompanyLabel(coreCompany) : "Нет доступных компаний";
  const navItem = (label, icon, path) => `<a class="navitem ${view.page === path ? "active" : ""}" href="#${path}">${I(icon)}<span>${label}</span></a>`;
  renderMarkup($("#app"), `${mobile ? '<button class="mobile-shade" data-action="mobile" aria-label="Закрыть навигацию"></button>' : ""}<aside class="sidebar ${mobile ? "open" : ""}"><a class="brand" href="#home"><span>Альфа Банк</span></a><nav class="banknav" aria-label="Меню банка"><div class="navlabel">Мои сервисы</div>${navItem("Бизнес-пульс","trend","home")}${navItem("Моя компания","building","company")}${navItem("Новости","file","events")}</nav></aside><main class="main"><div class="workspace"><header class="topbar"><div class="module-name"><a href="#home">Бизнес-пульс</a></div><div class="toptools"><button class="company-button" data-action="core-company-menu" aria-label="Выбрать компанию: ${esc(companyTitle)}" aria-haspopup="menu">${I("building")}<span>${esc(companyTitle)}</span>${I("down")}</button><a class="account-button" href="#account" aria-label="Профиль: ${esc(userName)}" title="${esc(userName)}"><span class="user-avatar">${esc(Array.from(userName)[0] || "П")}</span></a></div></header><div class="toolbar"><nav class="tabs module-tabs" aria-label="Бизнес-пульс"><a class="tab ${["home","overview","metric","metric-new"].includes(view.page)?"active":""}" href="#home">Обзор</a><a class="tab ${["company","context"].includes(view.page)?"active":""}" href="#company">Компания</a><a class="tab ${["events","news","sources"].includes(view.page)?"active":""}" href="#events">Новости</a></nav></div><div id="main-content" tabindex="-1">${coreBusy ? '<p class="hint">Обновляем сведения…</p>' : ""}${coreError && coreCompany ? `<p class="form-error">${esc(coreError)}</p>` : ""}${page}</div></div></main>`);
}
function render() {
  UI.close();
  if (coreMode) {
    renderCore();
    return;
  }
  sync();
  if (!demoSignedIn) {
    document.title = "Вход · Альфа-Бизнес";
    renderMarkup($("#app"), loginPage());
    renderAssistant();
    return;
  }
  document.title =
    (view.page === "detail"
      ? E.assess(state, view.id)?.title || "Бизнес-пульс"
      : view.page === "home" ? "Главная" : view.page === "account" ? "Мой профиль" : "Бизнес-пульс") + " · Альфа-Бизнес";
  renderMarkup(
    $("#app"),
    `${mobile ? '<button class="mobile-shade" data-action="mobile" aria-label="Закрыть навигацию"></button>' : ""}<aside class="sidebar ${mobile ? "open" : ""}">${pulse.sidebar()}</aside><main class="main"><div class="workspace"><header class="topbar"><div class="module-name">${iconButton("menu", "mobile", "Открыть меню").replace('class="iconbtn"', 'class="iconbtn mobilemenu"')}<a href="#${["home", "account"].includes(view.page) ? "home" : "overview"}">${["home", "account"].includes(view.page) ? "Альфа-Бизнес" : "Бизнес-пульс"}</a><span class="demo">Демо</span></div><div class="toptools"><button class="iconbtn" data-action="pulse-help" aria-label="Обучение разделу">?</button><button class="company-button" data-action="company-menu" aria-label="Выбрать компанию: ${esc(state.company.name)}" aria-haspopup="menu" aria-expanded="false">${I("building")}<span>${esc(state.company.name)}</span>${I("down")}</button><button class="iconbtn" data-action="notifications" aria-label="Обновления" aria-haspopup="dialog" aria-expanded="false">${I("bell")}${state.notifications.some((n) => !n.read) ? '<span class="dot"></span>' : ""}</button><button class="assistant-button ${assistant ? "active" : ""}" data-action="assistant" aria-haspopup="dialog" aria-controls="assistant-dialog" aria-expanded="${assistant}" aria-label="Помощник">${root.pet ? '<img src="/assets/01_calm_right.webp" alt="">' : I("chat")}<span>Помощник</span></button><a class="account-button ${view.page === "account" ? "active" : ""}" href="#account" aria-label="Мой профиль: ${esc(profileName())}" title="${esc(profileName())}" ${view.page === "account" ? 'aria-current="page"' : ""}><span class="user-avatar" aria-hidden="true">${esc(profileInitials())}</span></a></div></header>${
      view.page !== "account"
        ? `<div class="toolbar"><nav class="tabs module-tabs" aria-label="Бизнес-пульс">${[
            ["home", "Обзор", ["home", "overview", "metric", "digest", "history", "deep"]],
            ["situations/all", "Ситуации", ["situations", "detail", "actions", "feedback"]],
            ["events", "Новости", ["events", "news"]],
            ["research", "Анализ периода", ["research", "research-new", "report"]],
          ].map(([p, l, pages]) => `<a class="tab ${pages.includes(view.page) ? "active" : ""}" href="#${p}" ${pages.includes(view.page) ? 'aria-current="page"' : ""}>${l === "Анализ периода" ? '<span class="full-label">Анализ периода</span><span class="short-label">Анализ</span>' : l}</a>`).join("")}</nav><div class="toolbar-tools">${B("Управление", "module-menu", "text", "down", `data-icon-position="right" aria-label="Управление модулем" aria-haspopup="menu" aria-expanded="false" ${["company", "context", "sources", "source", "source-new", "settings", "about"].includes(view.page) ? 'data-active="true"' : ""}`)}</div></div>`
        : ""
    }<div id="main-content" tabindex="-1">${content()}</div></div></main>`,
  );
  renderAssistant();
  requestAnimationFrame(()=>{ if(demoSignedIn) pulse.help(view.page); });
}
function content() {
  switch (view.page) {
    case "home":
      return pulse.dashboard();
    case "metric":
      return pulse.metricPage(view.id);
    case "account":
      return accountPage();
    case "overview":
      return pulse.dashboard();
    case "situations":
      return inventory();
    case "detail":
      return detail();
    case "events":
      return eventList();
    case "news":
      return newsReader();
    case "company":
      return company();
    case "context":
      return contextForm();
    case "sources":
      return sources();
    case "source":
      return sourceDetail();
    case "source-new":
      return sourceForm();
    case "research":
      return researchList();
    case "research-new":
      return researchForm();
    case "report":
      return researchReport();
    case "digest":
      return digest();
    case "actions":
      return actionsPage();
    case "history":
      return historyPage();
    case "settings":
      return settings();
    case "about":
      return about();
    case "feedback":
      return feedbackForm();
    case "deep":
      return deepReport();
    default:
      return empty(
        "Страница не найдена",
        "Перейдите к главным ситуациям.",
        B("К главному", "nav:overview"),
      );
  }
}
function profileName() {
  return [root.profile.firstName, root.profile.lastName].filter(Boolean).join(" ");
}
function profileInitials() {
  return [root.profile.firstName, root.profile.lastName].filter(Boolean).map((s) => Array.from(s.trim())[0] || "").join("").toLocaleUpperCase("ru");
}
function loginPage() {
  return `<main class="login-layout" id="main-content"><section class="login-showcase" aria-label="О сервисе"><a class="brand" href="#login" aria-label="Альфа-Бизнес"><span class="alpha">А</span><span>Альфа-Бизнес</span></a><div><span class="login-eyebrow">Бизнес-пульс</span><h1>Всё важное для вашего бизнеса — в одном месте</h1><p>Ситуации, новости и следующие шаги с учётом выбранной компании.</p><div class="login-preview"><span>Ваш рабочий обзор</span><strong>От события к решению</strong><small>Одинаково удобно для производства, торговли, услуг и других направлений.</small></div></div><span class="login-footnote">Вход в сервис или отдельное демо</span></section><section class="login-panel"><div class="login-mobile-brand"><span class="alpha">А</span><strong>Альфа-Бизнес</strong></div><form id="demo-login-form" class="login-card"><span class="login-eyebrow">Вход в сервис</span><h2>Войдите в сервис</h2><p>Используйте свою учётную запись.</p>${formError()}${coreError ? `<p class="form-error" role="alert">${esc(coreError)}</p>` : ""}<label for="demo-login-email">Электронная почта</label><input id="demo-login-email" name="email" type="email" autocomplete="username" placeholder="name@company.ru" required><label for="demo-login-password">Пароль</label><input id="demo-login-password" name="password" type="password" autocomplete="current-password" placeholder="Введите пароль" required><button class="login-submit" type="submit" ${coreBusy ? "disabled" : ""}>${coreBusy ? "Входим…" : "Войти"} ${I("right")}</button><p class="login-disclaimer">Вход проверяется сервером; используется защищённая cookie-сессия.</p><div class="actions">${B("Посмотреть демо","demo-login","secondary")}</div></form></section></main>`;
}
function accountPage() {
  return back("home", "На главную банка") + `<div class="form-page account-page">${intro("Мой профиль", "Личные данные пользователя. Организация выбирается отдельно в шапке.")}<form id="account-form" class="card">${formError()}<div class="account-identity"><span class="user-avatar large" aria-hidden="true">${esc(profileInitials())}</span><div><h2>${esc(profileName())}</h2><p>Демонстрационный профиль</p></div></div><div class="form-grid">${textField("firstName","Имя",draft.firstName,"","text",'required maxlength="80" autocomplete="given-name"')}${textField("lastName","Фамилия",draft.lastName,"","text",'maxlength="80" autocomplete="family-name"')}</div><div class="actions"><button type="submit" class="btn primary">Сохранить</button>${B("Отмена","form-cancel","secondary")}</div><p class="account-limit">Имя используется в этом прототипе. Банковский вход и управление правами не подключены.</p></form><section class="account-organization"><h2>Текущая организация</h2><div class="account-org-row">${I("building")}<div><strong>${esc(state.company.name)}</strong><p>${esc(state.company.facts.activity)}</p></div></div><p>Смена организации меняет рабочие данные и оценки. Ваш профиль остаётся прежним.</p></section></div>`;
}

function briefImpact(a) {
  if (
    !a.sources.length ||
    [
      "Связь исключена",
      "Связь не подтверждена",
      "Низкая срочность",
      "Влияние отложено",
      "Ограничение мощности",
      "Сравнить со своим трендом",
    ].includes(a.status)
  )
    return a.impact;
  return (
    {
      packaging:
        "Изменения у пищевых производителей могут затронуть часть ваших заказов.",
      raw: "Рост цен поставщиков может увеличить себестоимость следующей партии.",
      finance:
        state.company.facts.rateType === "floating"
          ? "Плавающая ставка может повлиять на платежи. Проверьте дату пересмотра условий."
          : "Влияние ставки зависит от вашего кредитного договора и планов нового займа.",
      demand:
        "Охлаждение потребления может повлиять на посещаемость и средний чек.",
      software:
        "Клиенты из торговли могут переносить новые ИТ-проекты. Проверьте планы продления.",
      logistics:
        "Задержка у поставщика может затронуть ваши сроки, если маршруты совпадают.",
      opportunity:
        "Новые покупатели могут искать поставщиков с более подходящими условиями.",
    }[a.id] || a.impact
  );
}
function briefTitle(a) {
  return a.id === "raw" && state.company.facts.fixed !== "yes"
    ? "Операционные расходы"
    : a.title;
}
function disclosure(title, body, extra = "", open = false) {
  return `<details class="disclosure ${extra}" ${open ? "open" : ""}><summary><span>${esc(title)}</span>${I("down")}</summary><div class="disclosure-body">${body}</div></details>`;
}
function bankBrief(source) {
  if (source.id === "bank-rate")
    return "В отчёте банк ожидал паузу в снижении ключевой ставки. Влияние на компанию зависит от типа ставки по долгу и планов заимствования.";
  if (source.id === "bank-consumption")
    return "Авторы ожидали охлаждения потребления в IV квартале. Это прогноз по рынку, а не подтверждённое снижение ваших продаж.";
  return source.text.length > 210
    ? source.text.slice(0, 210) + "…"
    : source.text;
}

function overview() {
  const all = E.list(state),
    model = C.caseCatalog(all, state, { view: "attention", size: 3 });
  let items = model.items;
  if (root.role === "finance")
    items.sort(
      (a, b) =>
        (b.id === "finance") - (a.id === "finance") || b.score - a.score,
    );
  if (root.role === "supply")
    items.sort(
      (a, b) => (b.id === "raw") - (a.id === "raw") || b.score - a.score,
    );
  const counts = model.counts,
    last = state.checks[0]?.at;
  return (
    intro(
      "Главное для бизнеса",
      "Что изменилось и что стоит проверить в вашей компании.",
    ) +
    `<div class="overview-controls"><span class="freshness">${I("clock")}<span>Проверено: ${time(last)}</span></span><div class="actions flush">${refreshButton()}${B('<span class="full-label">Изменения за период</span><span class="short-label">За период</span>', "nav:research-new", "quiet", "calendar", 'aria-label="Изменения за период"')}</div></div>${jobBanner()}${lastResult ? `<div class="update-result" role="status">${I("info")}<span>${esc(lastResult)}</span></div>` : ""}
 <nav class="work-summary" aria-label="Состояние ситуаций">${[
   ["attention", "На проверку"],
   ["working", "В работе"],
   ["watch", "Наблюдения"],
   ["all", "Все ситуации"],
 ]
   .map(
     ([key, label]) =>
       `<a href="#situations/${key}" class="work-summary-item"><span>${label}</span><strong>${counts[key]}</strong><small>${{attention:"Ещё не проверяли",working:"Активные проверки",watch:"Следим за изменениями",all:"Все состояния"}[key]}</small>${I("right")}</a>`,
   )
   .join("")}</nav>
 <section class="overview-priorities"><div class="section-heading"><h2>На проверку</h2><a class="inline-link" href="#situations/attention"><span class="full-label">${"Все проверки · " + counts.attention}</span><span class="short-label">Все · ${counts.attention}</span> ${I("right")}</a></div>
 <div id="situation-list">${items.length ? items.map((a, i) => situation(a, i === 0)).join("") : empty("Проверки распределены", "Откройте ситуации в работе или наблюдения, чтобы продолжить.", B("Открыть ситуации", "nav:situations/all"))}</div></section>
 ${counts.attention > 3 ? `<p class="queue-explanation">Здесь — три первых вопроса. Остальные собраны по темам в <a class="link" href="#situations/attention">списке проверок</a>.</p>` : ""}
 <section class="overview-documents" aria-label="Сводка и обновления"><a href="#digest" class="document-shortcut">${I("file")}<span><strong>Сводка для сохранения</strong><small>Приоритеты и обновления за день или неделю</small></span>${I("right")}</a><a href="#history" class="document-shortcut">${I("clock")}<span><strong>Журнал обновлений</strong><small>Когда проверяли данные и что изменилось</small></span>${I("right")}</a></section>${state.deepAnalysis ? B("Дополнительные связи", "nav:deep", "text", "context") : ""}`
  );
}

function materialRange() {
  const dates = E.activeSources(state)
    .map((s) => s.publishedAt)
    .filter(Boolean)
    .sort();
  return dates.length ? date(dates.at(-1)) : "нет материалов";
}
function sectorLabel() {
  return {
    packaging: "Производство упаковки",
    software: "Разработка ПО",
    restaurant: "Общественное питание",
    other: "Профиль компании",
  }[E.sectorFor(state.company)];
}
function situation(a, first = false) {
  const more = iconButton("more", "more:" + a.id, "Действия: " + a.title, 'aria-haspopup="menu" aria-expanded="false"');
  if (!first)
    return `<article class="compact-row situation-card">${situationArt(a.id)}<div class="compact-content"><h2><a class="situation-link" href="#detail/${a.id}">${esc(briefTitle(a))}</a></h2><p>${esc(a.metric)} · ${esc(a.horizon)}</p></div><span class="card-chevron" aria-hidden="true">${I("right")}</span>${more}</article>`;
  return `<article class="lead-card has-art situation-card"><div class="lead-copy"><div class="lead-category"><span>${a.type === "opportunity" ? "Возможность" : "Риск"} · ${esc(a.metric)}</span></div><h2><a class="situation-link" href="#detail/${a.id}">${esc(briefTitle(a))}</a></h2><p class="lead-description">${esc(briefImpact(a))}</p><div class="lead-meta">${esc(a.horizon)}</div><span class="card-open" aria-hidden="true">Открыть разбор ${I("right")}</span></div>${situationArt(a.id, "lead-illustration")}<div class="lead-more">${more}</div></article>`;
}
function situationArt(id, extra = "") {
  const asset = [
    "packaging",
    "raw",
    "finance",
    "demand",
    "software",
    "logistics",
    "opportunity",
  ].includes(id)
    ? id
    : "packaging";
  return `<img class="situation-art ${extra}" src="/assets/situation-${asset}.webp" alt="" draggable="false" loading="lazy">`;
}
function quickPager(model, prefix) {
  if (model.pages <= 1) return "";
  return `<nav class="quick-pager" aria-label="Быстрое переключение страниц">${B("", prefix + "-page:" + (model.page - 1), "iconbtn", "left", `aria-label="Предыдущая страница вверху" ${model.page === 0 ? "disabled" : ""}`)}${B("", prefix + "-page:" + (model.page + 1), "iconbtn", "right", `aria-label="Следующая страница вверху" ${model.page === model.pages - 1 ? "disabled" : ""}`)}</nav>`;
}

function pageNavigation(model, prefix) {
  if (model.pages <= 1) return "";
  const pages = [
    ...new Set([
      0,
      model.page - 1,
      model.page,
      model.page + 1,
      model.pages - 1,
    ]),
  ]
    .filter((p) => p >= 0 && p < model.pages)
    .sort((a, b) => a - b);
  return `<nav class="page-navigation" aria-label="Страницы результатов">${B("", prefix + "-page:" + (model.page - 1), "iconbtn", "left", `aria-label="Предыдущая страница" ${model.page === 0 ? "disabled" : ""}`)}${pages.map((page, i) => `${i && page > pages[i - 1] + 1 ? '<span class="page-gap">…</span>' : ""}<button class="page-number ${page === model.page ? "active" : ""}" data-action="${prefix}-page:${page}" aria-label="Страница ${page + 1}" ${page === model.page ? 'aria-current="page"' : ""}>${page + 1}</button>`).join("")}${B("", prefix + "-page:" + (model.page + 1), "iconbtn", "right", `aria-label="Следующая страница" ${model.page === model.pages - 1 ? "disabled" : ""}`)}</nav>`;
}
function domainNavigation(model, prefix, selected, includeGrowth = true) {
  const visible = C.domains.filter(
    (d) => (includeGrowth || d.id !== "growth") && model.facets[d.id] > 0,
  );
  return `<aside class="topic-rail"><h2>По темам</h2><nav aria-label="Темы ${prefix === "case" ? "ситуаций" : "событий"}">${B(`<span>Все темы</span><span class="facet-count">${model.matching}</span>`, prefix + "-domain:all", `topic-option ${selected === "all" ? "active" : ""}`, "grid", `aria-pressed="${selected === "all"}"`)}${visible.map((d) => B(`<span>${esc(d.label)}</span><span class="facet-count">${model.facets[d.id]}</span>`, prefix + "-domain:" + d.id, `topic-option ${selected === d.id ? "active" : ""}`, d.icon, `aria-pressed="${selected === d.id}"`)).join("")}</nav></aside><div class="topic-mobile">${selectField(prefix + "Domain", "Тема", [["all", "Все темы · " + model.matching], ...visible.map((d) => [d.id, d.label + " · " + model.facets[d.id]])], selected)}</div>`;
}
function inventory() {
  const mode = C.views.some(([v]) => v === view.id) ? view.id : "all";
  const model = C.caseCatalog(E.list(state), state, {
    view: mode,
    domain: inventoryDomain,
    query,
    type: inventoryType,
    sort: inventorySort,
    page: inventoryPage,
  });
  inventoryPage = model.page;
  return (
    intro(
      "Ситуации",
      "Вопросы, которые могут повлиять на компанию. Выберите состояние работы или тему.",
      B("Изменения за период", "nav:research-new", "quiet", "calendar"),
    ) +
    `<nav class="case-views" aria-label="Состояние работы">${C.views.map(([key, label]) => B(`${label}<span>${model.counts[key]}</span>`, "case-view:" + key, `case-view ${mode === key ? "active" : ""}`, "", `aria-pressed="${mode === key}"`)).join("")}</nav><p class="view-explanation">${{attention:"Ситуации, которые вы ещё не начали проверять.",working:"Только проверки, которые вы взяли в работу.",watch:"Вы следите за изменениями, без активной проверки.",deferred:"Вы решили вернуться к этим ситуациям позже.",all:"Все ситуации компании, включая отложенные и архив.",archive:"Выполненные и скрытые ситуации. Скрытые можно вернуть."}[mode]}</p>
 <div class="catalog-layout">${domainNavigation(model, "case", inventoryDomain)}<section class="catalog-results" aria-label="Список ситуаций"><div class="catalog-filter"><label class="search">${I("search")}<input id="situation-search" placeholder="Найти ситуацию" aria-label="Найти ситуацию" value="${esc(query)}"></label>${selectField(
   "inventorySort",
   "Порядок",
   [
     ["priority", "По приоритету"],
     ["updated", "По обновлению"],
     ["due", "По дате возврата"],
   ],
   inventorySort,
 )}</div>
 <div class="result-toolbar"><div class="result-count" role="status">${model.total ? "Ситуаций: " + model.total : "Ситуаций не найдено"}${inventoryDomain !== "all" ? " · " + esc(C.domains.find((d) => d.id === inventoryDomain)?.label || "") : ""}${model.pages > 1 ? " · страница " + (model.page + 1) + " из " + model.pages : ""}</div>${quickPager(model, "case")}</div>
 ${
   model.items.length
     ? `<div class="case-directory">${model.items
         .map((a) => {
           const b = C.bucket(a, state),
             task = state.actions[a.id],
             unseen =
               !!state.viewedVersions[a.id] &&
               state.viewedVersions[a.id] !== a.signature;
           return `<article class="case-entry situation-card">${situationArt(a.id)}<div class="case-entry-main"><div class="case-entry-meta">${esc(a.metric)}${unseen ? '<span class="unread-mark">Обновлено</span>' : ""}</div><h2><a class="situation-link" href="#detail/${a.id}">${esc(briefTitle(a))}</a></h2><p>${esc(briefImpact(a))}</p><div class="case-entry-foot"><span>${esc(b === "archive" ? (state.hidden.includes(a.id) ? "Скрыто" : actionLabel(task?.status)) : b === "working" ? "В работе" : b === "watch" ? "Наблюдение" : b === "deferred" ? "Отложено" : a.horizon)}${task?.due ? " · до " + date(task.due) : ""}</span><span>${a.sources.length} ${plural(a.sources.length, "материал", "материала", "материалов")}</span></div></div><div class="case-entry-controls"><span class="card-chevron" aria-hidden="true">${I("right")}</span>${state.hidden.includes(a.id) ? iconButton("refresh", "unhide:" + a.id, "Вернуть: " + a.title) : iconButton("more", "more:" + a.id, "Действия: " + a.title, 'aria-haspopup="menu" aria-expanded="false"')}</div></article>`;
         })
         .join("")}</div>`
     : empty(
         "В этой подборке пусто",
         model.matching ? "Измените тему или сбросьте поиск." : "Здесь появятся ситуации с выбранным состоянием работы.",
         model.matching ? B("Сбросить фильтры", "case-reset") : B("Все ситуации", "nav:situations/all"),
       )
 }${pageNavigation(model, "case")}</section></div>`
  );
}
function plural(n, one, few, many) {
  return n % 10 === 1 && n % 100 !== 11
    ? one
    : n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14)
      ? few
      : many;
}

function detail() {
  const a = E.assess(state, view.id);
  if (!a)
    return empty(
      "Ситуация не найдена",
      "Откройте её из главного списка.",
      B("К главному", "nav:overview"),
    );
  const v = state.assessments[a.id],
    tab = view.tab;
  return (
    back(detailReturn, detailReturn.startsWith("news/") ? "К материалу" : detailReturn === "overview" ? "К обзору" : detailReturn === "digest" ? "К сводке" : detailReturn.startsWith("actions") ? "К действию" : "К ситуациям") +
    `<div class="page-intro detail-intro"><div><div class="eyebrow">${a.type === "opportunity" ? "Возможность" : "Риск"} · ${esc(a.metric)}</div><h1>${esc(briefTitle(a))}</h1><p class="detail-lead">${esc(briefImpact(a))}</p></div>${iconButton("more", "more:" + a.id, "Действия с ситуацией", 'aria-haspopup="menu" aria-expanded="false"')}</div>
 <div class="detail-tools"><span class="freshness">${esc(a.horizon)} · ${time(v?.checkedAt)}</span><div class="actions flush">${refreshButton(a.id)}${B("Спросить помощника", "ask:" + a.id, "quiet", "chat")}</div></div>${jobBanner()}
 <nav class="detail-tabs tabs" aria-label="Разбор ситуации">${[
   ["analysis", "Разбор"],
   ["evidence", "Основания · " + a.sources.length],
   ["history", "История"],
 ]
   .map(
     ([t, l]) =>
       `<a class="tab ${tab === t ? "active" : ""}" href="#detail/${a.id}/${t}" ${tab === t ? 'aria-current="page"' : ""}>${l}</a>`,
   )
   .join(
     "",
   )}</nav>${tab === "evidence" ? evidence(a) : tab === "history" ? historyRows(a.id) : analysis(a)}`
  );
}

function eventDescription(a) {
  const latest = [...a.sources].sort((s, t) =>
    t.publishedAt.localeCompare(s.publishedAt),
  )[0];
  return latest.synthetic
    ? eventSummaries[a.id]
    : latest.title +
        " · " +
        latest.kind.toLowerCase() +
        " от " +
        date(latest.publishedAt) +
        ". " +
        latest.text.slice(0, 220) +
        (latest.text.length > 220 ? "…" : "");
}
function analysis(a) {
  const sc = state.scenarios[a.id],
    compare = sc ? E.assess(state, a.id, sc) : null;
  const bank = a.bank.length
    ? `<section class="bank-note"><div class="bank-note-heading"><span class="bank-mark" aria-hidden="true">А</span><div><h2>Взгляд банка</h2><span class="small muted">Прогноз · ${date(a.bank[0].publishedAt)}</span></div></div>${a.bank.map((s) => `<p>${esc(bankBrief(s))}</p><a class="inline-link" href="#source/${esc(s.id)}">${esc(s.title)} ${I("right")}</a>`).join("")}</section>`
    : `<div class="bank-missing">${I("file")}<span>Для этой ситуации подходящая позиция банка пока не добавлена.</span></div>`;
  return `${nextAction(a)}${bankProduct(a)}<div class="card reasoning-card"><div class="section-heading"><h2>Почему это касается вашей компании</h2></div><p class="body-lead">${esc(a.why)}</p><div class="chain">${a.chain.map((x, i) => `${i ? I("right") : ""}<span>${esc(x)}</span>`).join("")}</div><p class="caption">Возможный канал влияния. Связь с показателями компании ещё нужно проверить.</p>${bank}</div>
 ${sc ? `<div class="card scenario-card"><div class="spread"><h2>Ваш сценарий</h2>${B("Удалить", "scenario-delete:" + a.id, "text")}</div><p class="body-lead">${esc(compare.impact)}</p><p class="caption">Допущение рассчитано отдельно. Текущий профиль компании не менялся.</p><div class="actions">${B("Применить к профилю", "scenario-apply:" + a.id, "primary")}${B("Изменить условия", "scenario:" + a.id, "secondary")}</div></div>` : ""}
 <div class="disclosure-stack">${disclosure("Что может изменить вывод", `<p>${esc(a.alt)}</p><p>${esc(a.missing)}</p><div class="actions">${B("Проверить своё допущение", "scenario:" + a.id, "secondary", "context")}</div>`)}${disclosure("Достаточно ли оснований", `<p>Материалов: ${a.sources.length}. Независимых групп: ${a.groups}. ${esc(a.confidence)}.</p><p>Перепечатки одного сообщения не считаются независимыми подтверждениями. Прогноз известного автора остаётся прогнозом.</p><div class="actions">${B("Открыть основания", "nav:detail/" + a.id + "/evidence", "secondary", "file")}</div>`)}</div>`;
}

function bankProduct(a) {
  const product = contextualProduct(a, state);
  if (!product) return "";
  return `<aside class="bank-product" aria-label="Продукт Альфа-Банка"><img src="/assets/situation-${product.art}.webp" alt="" class="situation-art"><div class="bank-product-copy"><span class="commercial-label">Продукт Альфа-Банка</span><h2>${esc(product.title)}</h2><p>${esc(product.description)}</p><div class="bank-product-links"><a class="inline-link" href="${product.url}" target="_blank" rel="noopener">Посмотреть условия ${I("right")}</a><details><summary>Почему здесь</summary><p>${esc(product.reason)}</p></details></div></div>${iconButton("close", "offer-dismiss:" + product.id, "Скрыть предложение на 30 дней")}</aside>`;
}

function evidence(a) {
  return `<p class="section-description">Здесь можно проверить, на чём построена оценка, и исключить материал.</p><div class="card source-evidence">${a.sources.length ? a.sources.map((s) => evidenceRow(s)).join("") : empty("Оснований нет", "Восстановите источник или добавьте материал.", B("К источникам", "nav:sources"))}</div>${disclosure("Как оценивать надёжность", `<p>Наблюдение, прогноз и мнение автора различаются. Авторитет издания не гарантирует точность отдельного утверждения; перепечатки не добавляют независимых подтверждений.</p><p>Учебные материалы отмечены отдельно. В банковских отчётах прочитаны первые страницы с основными выводами; ссылки ведут на оригиналы.</p>`)}`;
}

function evidenceRow(source) {
  return `<article class="evidence-row"><div class="source-heading"><div class="row-grow"><div class="eyebrow">${esc(source.kind)}${source.synthetic ? " · Демоданные" : source.bank ? " · Альфа-Банк" : ""}</div><h3><a href="#source/${esc(source.id)}">${esc(source.title)}</a></h3><p class="source-meta">${esc(source.publisher)} · ${date(source.publishedAt)}</p></div>${iconButton("more", "source-more:" + source.id, "Действия с источником " + source.title, 'aria-haspopup="menu" aria-expanded="false"')}</div><p class="source-excerpt">${esc(source.text.length > 190 ? source.text.slice(0, 190) + "…" : source.text)}</p><a class="inline-link" href="#source/${esc(source.id)}">Читать материал ${I("right")}</a>${source.excluded ? B("Вернуть в анализ", "source-restore:" + source.id, "text") : ""}</article>`;
}

const actionLabel = (s) =>
  ({
    review: "В работе",
    watch: "Наблюдение",
    deferred: "Отложено",
    done: "Выполнено",
  })[s] || "Не начато";
function nextAction(a) {
  const task = state.actions[a.id],
    stale = task && task.basisVersion !== state.assessments[a.id]?.version;
  return `<section class="card decision-card"><div class="spread"><div class="eyebrow">Что стоит проверить</div>${task ? badge(actionLabel(task.status), task.status === "done" ? "green" : "") : ""}</div><h2>${esc(a.next)}</h2><p>${esc(a.missing)}</p>${stale ? '<p class="inline-warning">Оценка изменилась после выбора действия. Проверьте основания ещё раз.</p>' : ""}<div class="actions">${!a.sources.length ? B("Добавить основания", "nav:sources", "primary", "file") : B("Уточнить данные", "context:" + a.id, "primary", "pencil")}${task ? B("Детали действия", "nav:actions/" + a.id, "secondary") : a.sources.length ? B("Взять на проверку", "task-review:" + a.id, "secondary", "check") : ""}${B("Ещё", "decision-menu:" + a.id, "text", "", 'aria-haspopup="menu" aria-expanded="false"')}</div></section>`;
}

function historyRows(id) {
  const rows = state.history.filter((h) => !id || h.caseId === id);
  return `<div class="card">${rows.length ? rows.map((h) => `<article class="history-row"><div>${badge("Версия " + h.version)}<div class="hint">${time(h.at)}</div></div><div><h3>${esc(h.title)}</h3><div class="hint" style="margin-top:8px">${esc(h.reason)}${h.previousStatus ? " · Было: " + esc(h.previousStatus) : ""}</div><p>${esc(h.impact)}</p><details><summary>Состав оснований и контекст версии</summary><div class="hint" style="margin-top:12px">Снимок включает данные на момент оценки и хеш текста каждого материала. Позднейшие изменения не перезаписывают этот состав.</div><pre>${esc(JSON.stringify(h.snapshot, null, 2))}</pre></details></div></article>`).join("") : empty("Пересмотров ещё нет", "История появляется, когда меняется контекст или состав оснований.")}</div>`;
}
function historyPage() {
  return (
    back("overview", "К главному") +
    intro(
      "Журнал обновлений",
      "Здесь видно, когда проверяли данные и почему изменилась оценка. Новая версия сохраняется только при изменениях.",
    ) +
    historyRows() +
    `<div class="card"><h2>Последние проверки</h2>${state.checks
      .slice(0, 10)
      .map(
        (c) =>
          `<div class="research-card"><div class="spread wrap"><strong>${esc(c.reason)}</strong><span class="hint">${time(c.at)}</span></div><p>${c.changes ? "Новых версий: " + c.changes : "Изменений оснований нет — версия сохранена."}</p></div>`,
      )
      .join("")}</div>`
  );
}
function eventList() {
  const demo = view.id === "volume",
    sources = demo ? volumeSources() : state.sources;
  const all = E.events({ ...state, sources });
  const cutoff =
    eventPeriod === "week"
      ? E.addDays(E.TODAY, -7)
      : eventPeriod === "month"
        ? E.addDays(E.TODAY, -30)
        : "";
  const model = C.eventCatalog(all, {
    domain: eventDomain,
    query: eventQuery,
    kind: eventKind,
    cutoff,
    onlyNew: eventOnlyNew,
    seen: demo ? volumeSeen : state.seenEvents,
    page: eventPage,
  });
  eventPage = model.page;
  return (
    intro(
      "Новости",
      "Новости, прогнозы и аналитика из подключённых источников. Читайте материалы и проверяйте их влияние на компанию.",
      demo
        ? B("Вернуться к своим данным", "nav:events", "secondary", "left")
        : refreshButton(),
    ) +
    `${demo ? '<div class="demo-volume"><strong>Демонстрация: 150 материалов</strong><span>Вымышленный поток для проверки навигации. Оценки компании не меняются.</span></div>' : jobBanner()}
 <div class="news-scope"><button class="news-unread" data-action="event-new" aria-pressed="${eventOnlyNew}">${I("check")}Непросмотренные</button><span class="news-total"><span class="full-label">${all.length} ${plural(all.length, "материал", "материала", "материалов")} в источниках</span><span class="short-label">Всего: ${all.length}</span></span></div>
 <div class="catalog-layout">${domainNavigation(model, "event", eventDomain, false)}<section class="catalog-results" aria-label="Список новостей"><div class="news-filter"><label class="search">${I("search")}<input id="event-search" placeholder="Тема, автор или издание" aria-label="Поиск по новостям" value="${esc(eventQuery)}"></label>${B("Фильтры" + ((eventPeriod !== "all" ? 1 : 0) + (eventKind !== "all" ? 1 : 0) ? " · " + ((eventPeriod !== "all" ? 1 : 0) + (eventKind !== "all" ? 1 : 0)) : ""), "event-filters", "text news-more-filter", "settings", `aria-expanded="${advancedNewsFilters}" aria-controls="news-extra-filters"`)}<div id="news-extra-filters" class="news-selects ${advancedNewsFilters ? "is-open" : ""}">${selectField(
   "eventPeriod",
   "Период",
   [
     ["all", "Весь период"],
     ["week", "Последние 7 дней"],
     ["month", "Последние 30 дней"],
   ],
   eventPeriod,
 )}${selectField(
   "eventKind",
   "Тип материала",
   [
     ["all", "Все типы"],
     ["Официальное сообщение", "Официальные сообщения"],
     ["Наблюдение", "Наблюдения"],
     ["forecast", "Прогнозы"],
     ["Мнение", "Мнения"],
     ["Не классифицирован", "Без классификации"],
   ],
   eventKind,
 )}</div></div>
 <div class="result-toolbar"><div class="result-count" role="status">${model.total ? "Найдено " + model.total + " " + plural(model.total, "материал", "материала", "материалов") : "Материалов не найдено"}${model.pages > 1 ? " · страница " + (model.page + 1) + " из " + model.pages : ""}</div>${quickPager(model, "event")}</div>
 ${model.items.length ? `<div class="news-list">${model.items.map((e) => eventEntry(e, demo)).join("")}</div>` : empty("Нет подходящих новостей", "Измените тему, период или тип материала.", B("Сбросить фильтры", "event-reset"))}${pageNavigation(model, "event")}</section></div>`
  );
}
function markEventSeen(id, demo = view.id === "volume") {
  if (!id) return;
  const event = E.events(
    demo ? { ...state, sources: volumeSources() } : state,
  ).find((e) => e.id === id);
  if (!event) return;
  if (demo) volumeSeen[id] = C.eventSignature(event);
  else {
    state.seenEvents[id] = C.eventSignature(event);
    persist();
  }
}
function readingText(event) {
  return event.sources[0]?.bank ? event.text.replace("Страница 1 оригинала проверена визуально.", "").trim() : event.text;
}
function eventEntry(e, demo = false) {
  const seen = !C.unread(e, demo ? volumeSeen : state.seenEvents), source = e.sources[0];
  const excerpt = readingText(e).replace(/\s+/g, " ");
  return `<article class="news-entry situation-card"><div class="news-entry-meta"><span>${date(e.date)}</span><span>${esc(e.kind)}</span>${e.synthetic ? "<span>Демоданные</span>" : ""}${!seen ? '<span class="new-event"><span class="sr-only">Не прочитано</span></span>' : ""}</div><h2><a class="situation-link" href="#news/${esc(e.id)}${demo ? "/volume" : ""}">${esc(e.title)}</a></h2><p class="news-excerpt">${esc(excerpt.length > 210 ? excerpt.slice(0, 210) + "…" : excerpt)}</p><div class="news-entry-bottom"><span>${esc(source.publisher)}${source.author ? " · " + esc(source.author) : ""}</span><span class="news-read-label" aria-hidden="true">Читать ${I("right")}</span></div></article>`;
}
function newsReader() {
  const demo = view.scope === "volume";
  const event = E.events(demo ? {...state, sources: volumeSources()} : state).find((e) => e.id === view.id);
  const returnPath = demo ? "events/volume" : newsReturn || "events";
  if (!event) return back(returnPath, "К новостям") + empty("Материал не найден", "Он мог быть исключён из источников.", B("Открыть новости", "nav:" + returnPath));
  const source = event.sources[0];
  const linked = E.list(state).filter((a) => a.topics.some((t) => event.applies.includes(t)));
  return back(returnPath, "К новостям") + `<article class="news-reader"><header><div class="news-entry-meta"><span>${date(event.date)}</span>${badge(event.kind)}${event.synthetic ? badge("Демоданные") : ""}</div><h1>${esc(event.title)}</h1><p class="reader-byline">${esc(source.publisher)}${source.author ? " · " + esc(source.author) : ""}</p></header><div class="reader-body">${source.bank ? '<p class="reader-note">Краткое изложение прочитанной части отчёта. Полный документ доступен по ссылке на оригинал.</p>' : ""}${readingText(event).split("\n").filter(Boolean).map((p) => `<p>${esc(p)}</p>`).join("")}</div><div class="reader-source"><p>${event.kind.includes("Прогноз") ? "Это прогноз на дату публикации. Его нужно проверять по новым данным." : event.kind === "Мнение" ? "Это мнение автора. Уверенный тон и известность не подтверждают вывод." : "Публикация — основание для проверки, а не доказательство влияния на вашу компанию."}</p><div class="actions">${source.url ? `<a class="btn secondary" href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">Открыть оригинал ${I("right")}</a>` : ""}${!demo ? B("Обсудить материал", "ask-source:" + source.id, "text", "chat") : ""}${!demo ? iconButton("more", "source-more:" + source.id, "Действия с материалом", 'aria-haspopup="menu" aria-expanded="false"') : ""}</div></div></article>${linked.length ? `<section class="reader-related"><h2>Что проверить в бизнесе</h2><p>Совпадение темы ещё не доказывает влияние на компанию.</p><div class="reader-related-list">${linked.map((a) => situation(a)).join("")}</div></section>` : ""}`;
}

function company() {
  const c = state.company,
    sector = E.sectorFor(c);
  const groups = [
    ["Компания", ["activity", "region"]],
    ["Финансирование", ["debt", "rateType", "refinance", "plannedBorrowing"]],
  ];
  if (sector === "packaging")
    groups.splice(
      1,
      0,
      ["Покупатели и производство", ["food", "freeCapacity"]],
      ["Закупки и поставки", ["fixed", "stock", "importShare"]],
    );
  if (sector === "restaurant")
    groups.splice(1, 0, [
      "Спрос и закупки",
      ["visits", "checkTrend", "fixed", "stock"],
    ]);
  if (sector === "software")
    groups.splice(1, 0, [
      "Заказчики и договоры",
      ["retailClients", "contractReview"],
    ]);
  const value = (key, v) =>
    [
      "food",
      "fixed",
      "debt",
      "plannedBorrowing",
      "retailClients",
      "freeCapacity",
      "importShare",
    ].includes(key)
      ? yn(v)
      : ["visits", "checkTrend"].includes(key)
        ? trends.find((t) => t[0] === v)?.[1] || "Не уточнено"
        : key === "rateType"
          ? {
              fixed: "Фиксированная",
              floating: "Плавающая",
              unknown: "Не уточнено",
            }[v] || "Не уточнено"
          : key === "stock"
            ? {
                week: "Меньше недели",
                month: "До месяца",
                more: "Больше месяца",
              }[v] || "Не уточнено"
            : ["refinance", "contractReview"].includes(key) && v
              ? date(v)
              : v && v !== "unknown"
                ? v
                : "Не уточнено";
  return (
    intro(
      "Моя компания",
      "Эти данные помогают оценивать влияние событий.",
      B("Уточнить профиль", "context:all", "primary", "pencil"),
    ) +
    `<div class="company-identity"><span class="company-avatar">${I("building")}</span><div><h2>${esc(c.name)}</h2><p>${esc(sectorLabel())} · ${esc(c.facts.region)}</p></div></div><div class="profile-groups">${groups.map(([title, keys]) => disclosure(title, `<dl class="profile-facts">${keys.map((key) => `<div><dt>${esc(fieldLabels[key])}</dt><dd>${esc(value(key, c.facts[key]))}<span class="fact-origin">${c.confirmed[key] ? "Уточнено вами · " + time(c.confirmed[key].at) : ["activity", "region"].includes(key) ? "Из базы кейса · дата не указана" : "Пока не уточнено"}</span></dd></div>`).join("")}</dl>`, "profile-group", title === "Компания")).join("")}</div><div class="content-section">${disclosure(
      "Исходные данные компании",
      `<p class="caption">Обезличенная строка ${esc(c.id)} из базы кейса. Название компании вымышленное.</p><div class="table-scroll"><table class="raw-table"><tbody>${Object.entries(
        c.raw,
      )
        .map(
          ([key, v]) =>
            `<tr><th scope="row">${esc(key)}</th><td>${esc(v || "Не заполнено")}</td></tr>`,
        )
        .join(
          "",
        )}</tbody></table></div><p>${esc(seed.dataset.unit)} Продуктовые флаги не раскрывают условия договора; принадлежность к группе не доказывает товарную зависимость.</p><a class="inline-link" href="${seed.dataset.url}" target="_blank" rel="noopener">Исходный файл кейса ${I("right")}</a>`,
    )}${
      c.factHistory.length
        ? disclosure(
            "История уточнений",
            c.factHistory
              .slice(0, 10)
              .map(
                (h) =>
                  `<div class="profile-history"><h3>${esc(fieldLabels[h.key] || h.key)}</h3><p>${esc(value(h.key, h.previous))} → ${esc(value(h.key, h.value))}</p><span class="caption">${time(h.at)}</span></div>`,
              )
              .join(""),
          )
        : ""
    }</div>`
  );
}

function contextForm() {
  const id = view.id || "all",
    full = id === "all",
    scenario = view.scope === "scenario",
    sector = E.sectorFor(state.company);
  const companyFields = () =>
    `<div class="form-grid">${textField("activity", "Деятельность", draft.activity, "", "text", 'required maxlength="500"')}${textField("region", "Регион", draft.region, "", "text", 'required maxlength="180"')}</div>`;
  const buyers = () =>
    selectField(
      "food",
      "Есть покупатели из пищевой отрасли?",
      yesno,
      draft.food,
    );
  const procurement = () =>
    `<div class="form-grid">${selectField("fixed", "Закупочная цена зафиксирована?", yesno, draft.fixed)}${selectField(
      "stock",
      "Запас критичных материалов",
      [
        ["unknown", "Неизвестен"],
        ["week", "Меньше недели"],
        ["month", "До месяца"],
        ["more", "Больше месяца"],
      ],
      draft.stock,
    )}</div>`;
  const finance = () =>
    `<div class="form-grid">${selectField("debt", "Есть действующий долг?", yesno, draft.debt)}${selectField(
      "rateType",
      "Тип ставки",
      [
        ["unknown", "Неизвестен"],
        ["fixed", "Фиксированная"],
        ["floating", "Плавающая"],
      ],
      draft.rateType,
    )}${dateField("refinance", "Ближайший пересмотр / рефинансирование", draft.refinance)}${selectField("plannedBorrowing", "Планируется новый заём?", yesno, draft.plannedBorrowing)}</div>`;
  const customers = () =>
    `<div class="form-grid">${selectField("retailClients", "Есть клиенты из розничной торговли?", yesno, draft.retailClients)}${dateField("contractReview", "Ближайшее продление договора", draft.contractReview)}</div>`;
  const demand = () =>
    `<div class="form-grid">${selectField("visits", "Посещаемость за четыре недели", trends, draft.visits)}${selectField("checkTrend", "Средний чек за четыре недели", trends, draft.checkTrend)}</div><p class="field-hint after-fields">Сравнивайте одинаковые дни недели.</p>`;
  const supply = () =>
    `<div class="form-grid">${selectField("importShare", "Есть зависимость от импорта?", yesno, draft.importShare || "unknown")}${selectField(
      "stock",
      "Запас критичных материалов",
      [
        ["unknown", "Неизвестен"],
        ["week", "Меньше недели"],
        ["month", "До месяца"],
        ["more", "Больше месяца"],
      ],
      draft.stock,
    )}</div>`;
  const groups = full ? [["Компания", companyFields()]] : [];
  if (full) {
    if (sector === "packaging")
      groups.push(
        ["Покупатели", buyers()],
        ["Закупки и запасы", procurement()],
      );
    if (sector === "software")
      groups.push(["Заказчики и договоры", customers()]);
    if (sector === "restaurant")
      groups.push(["Спрос", demand()], ["Закупки и запасы", procurement()]);
    groups.push(["Финансирование", finance()]);
    if (sector === "packaging")
      groups.push([
        "Зависимость от импорта",
        selectField(
          "importShare",
          "Есть зависимость от импорта?",
          yesno,
          draft.importShare || "unknown",
        ),
      ]);
  }
  const fields = full
    ? groups
        .map(([title, body], i) =>
          disclosure(title, body, "form-disclosure", i === 0),
        )
        .join("")
    : id === "finance"
      ? finance()
      : id === "raw"
        ? procurement()
        : id === "packaging"
          ? buyers()
          : id === "software"
            ? customers()
            : id === "demand"
              ? demand()
              : id === "logistics"
                ? supply()
                : id === "opportunity"
                  ? selectField(
                      "freeCapacity",
                      "Есть свободные мощности?",
                      yesno,
                      draft.freeCapacity,
                    )
                  : companyFields();
  const title = scenario
    ? "Проверить допущение"
    : {
        finance: "Условия финансирования",
        raw: "Условия закупки",
        packaging: "Покупатели компании",
        software: "Заказчики и договоры",
        demand: "Динамика спроса",
        logistics: "Поставки и запасы",
        opportunity: "Готовность к новым заказам",
        all: "Уточнить профиль",
      }[id] || "Уточнить данные";
  return (
    back(returnTo) +
    `<div class="form-page">${intro(title, scenario ? "Сравните результат, прежде чем менять профиль компании." : "Изменения будут учтены в оценке влияния событий.")}<form id="context-form" class="card ${full ? "full-profile-form" : ""}">${formError()}${fields}<div class="actions"><button type="submit" class="btn primary">${scenario ? "Рассчитать сценарий" : "Сохранить и пересмотреть"}</button>${B("Отмена", "form-cancel", "secondary")}</div></form></div>`
  );
}

function sources() {
  const rows = root.sources.filter(
      (source) =>
        (sourceFilter === "all" ||
          (sourceFilter === "excluded" ? source.excluded : !source.excluded)) &&
        (!sourceQuery ||
          [source.title, source.publisher, source.author]
            .join(" ")
            .toLowerCase()
            .includes(sourceQuery.toLowerCase())),
    ),
    model = C.paginate(rows, sourcePage, 10),
    visible = model.items;
  sourcePage = model.page;
  return (
    intro(
      "Источники и издания",
      "Управляйте составом анализа: добавляйте материалы, исключайте публикации или издание целиком. Для чтения откройте раздел «Новости».",
      B("Добавить материал", "nav:source-new", "primary", "plus"),
    ) +
    `<div class="filterbar"><div class="segmented" aria-label="Статус источников">${[
      ["all", "Все · " + root.sources.length],
      ["active", "В анализе"],
      ["excluded", "Исключённые"],
    ]
      .map(
        ([v, l]) =>
          `<button data-action="source-filter:${v}" class="${v === sourceFilter ? "active" : ""}" aria-pressed="${v === sourceFilter}">${l}</button>`,
      )
      .join(
        "",
      )}</div><label class="search">${I("search")}<input id="source-search" placeholder="Найти источник" aria-label="Найти источник" value="${esc(sourceQuery)}"></label></div>${(root.excludedPublishers || []).length ? `<div class="update-result"><span>Исключены издания: ${root.excludedPublishers.map((p) => esc(p.label) + " " + B("Вернуть", "publisher-restore:" + p.id, "text")).join("; ")}</span></div>` : ""}<div class="card source-directory">${rows.length ? visible.map((source) => `<article class="directory-row"><div class="signalicon ${source.bank ? "brand-tint" : ""}">${I("file")}</div><div class="row-grow"><h3><a href="#source/${esc(source.id)}">${esc(source.title)}</a></h3><p class="source-meta">${esc(source.publisher)} · ${date(source.publishedAt)}</p><span class="source-kind">${esc(source.kind)}${source.synthetic ? " · Демоданные" : ""}${source.excluded ? " · Исключён" : ""}</span></div>${source.excluded ? B("Вернуть", "source-restore:" + source.id, "secondary") : iconButton("more", "source-more:" + source.id, "Действия с источником " + source.title, 'aria-haspopup="menu" aria-expanded="false"')}</article>`).join("") : empty("Таких источников нет", "Измените фильтр или добавьте материал.")}</div><div class="list-footer"><span>${rows.length ? model.page * model.size + 1 : 0}–${Math.min((model.page + 1) * model.size, rows.length)} из ${rows.length}</span></div>${pageNavigation(model, "source")}${disclosure("Как добавляются и исключаются материалы", `<p>Официальные новости ЦБ загружаются автоматически. Для своего источника можно добавить ссылку и текст.</p><p>Меню материала позволяет исключить одну публикацию или всё издание, включая будущие материалы. Предыдущие версии оценок сохранятся.</p>`)}`
  );
}

function topicLabel(t) {
  return (
    {
      finance: "финансирование",
      raw: "затраты",
      packaging: "спрос",
      demand: "потребление",
      software: "бюджеты клиентов",
      energy: "энергия",
      logistics: "логистика",
      markets: "рынки",
    }[t] || t
  );
}
function sourceDetail() {
  const s = root.sources.find((s) => s.id === view.id);
  if (!s)
    return empty(
      "Материал не найден",
      "Откройте список источников.",
      B("Источники", "nav:sources"),
    );
  return (
    back("sources", "К источникам") +
    intro(
      s.title,
      "",
      s.excluded
        ? B("Вернуть в анализ", "source-restore:" + s.id, "primary")
        : B(
            "Исключить",
            "source-confirm:" + s.id,
            "quiet",
            "eyeoff",
            'aria-haspopup="menu" aria-expanded="false"',
          ),
    ) +
    `<div class="card prose"><div class="eyebrow">${badge(s.kind)}${s.synthetic ? badge("Демоданные") : s.bank ? badge("Банк", "blue") : badge("Импорт")}${s.excluded ? badge("Исключён", "red") : ""}</div><div class="source-meta">${esc(s.publisher)} · Автор: ${esc(s.author || "не указан")}</div><div class="source-meta">Публикация: ${date(s.publishedAt)} · Событие: ${s.eventAt ? date(s.eventAt) : "дата не установлена"}${s.page ? " · прочитана страница " + s.page : ""}</div><div class="section" style="margin-top:24px"><h2>Содержание</h2>${s.text
      .split("\n")
      .filter(Boolean)
      .map((p) => `<p>${esc(p)}</p>`)
      .join(
        "",
      )}</div><section class="section"><h2>Как использовать это основание</h2><p>${s.synthetic ? "Учебный материал. Он помогает пройти сценарий, но не подтверждает состояние реального рынка." : s.bank ? "Аналитическая позиция банка на дату публикации. Прогнозы не считаются состоявшимися событиями." : s.kind === "Официальное сообщение" ? "Официальная публикация подтверждает содержание сообщения. Интерпретация её влияния на компанию остаётся гипотезой." : "Проверяйте первоисточник и фактические основания каждого утверждения. Известность автора и уверенный тон не доказывают прогноз."}</p><p>Группа происхождения: ${esc(s.independence || s.publisher || "не установлена")}. Материалы одной группы не считаются независимым подтверждением.</p><div class="hint">Хеш содержания: ${E.hash(s.text)} · Получено: ${time(s.checkedAt)}${s.retrieved ? " · серверная загрузка" : ""}</div></section>${s.url ? `<div class="actions"><a class="btn secondary" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">Открыть оригинал ${I("right")}</a>${B("Уточнить у помощника", "ask-source:" + s.id, "text", "chat")}</div>` : ""}</div>`
  );
}
function sourceForm() {
  const reviewing = sourceStage !== "link";
  const missing = sourceImport ? [["title", "название"], ["publisher", "издание"], ["author", "автор"], ["publishedAt", "дата публикации"]].filter(([k]) => !draft[k]).map(([,label]) => label) : [];
  const importUnavailable = !integration.import;
  return back("sources", "К источникам") + `<div class="form-page">${intro("Добавить материал", "Вставьте текст вручную или используйте ссылку, если загрузка источников доступна.")}<form id="source-form" class="card source-import-form">${formError()}<div class="import-address">${textField("url", "Ссылка на публикацию", draft.url, importUnavailable ? "Загрузка текста по ссылке сейчас недоступна. Можно вставить текст вручную." : "Проверьте загруженные данные перед добавлением.", "url", 'placeholder="https://…" maxlength="2000"')}${B(busy ? "Загружаем…" : sourceImport ? "Загрузить заново" : "Заполнить по ссылке", "import-url", "secondary", busy ? "clock" : "link", busy || importUnavailable ? `disabled title="${importUnavailable ? "Загрузка источников сейчас недоступна." : ""}"` : "")}</div>${importUnavailable ? '<p class="hint">Загрузка источников сейчас недоступна. Вы можете добавить материал, вставив текст вручную.</p>' : ""}${busy ? '<div class="import-progress" role="status"><span class="spinner"></span>Получаем текст и сведения о публикации…</div>' : ""}${!reviewing ? `<div class="import-alternative"><span>Есть свой текст или сайт не поддерживается?</span>${B("Вставить текст вручную", "source-manual", "text")}</div>` : `<div class="import-review"><div class="import-review-heading"><h2>${sourceImport ? "Проверьте найденные данные" : "Сведения о материале"}</h2>${sourceImport ? badge("Загружено", "green") : ""}</div>${sourceImport ? `<p class="import-result" role="status">${missing.length ? "Не найдены: " + esc(missing.join(", ")) + ". Укажите дату; автора можно оставить пустым." : "Название, издание, автор и дата заполнены из публикации."} Характер утверждений определён предварительно.</p>` : ""}<div class="form-grid">${textField("title", "Название", draft.title, "", "text", 'required maxlength="240"')}${textField("publisher", "Издание / первоисточник", draft.publisher, "Материалы одного первоисточника не считаются независимыми подтверждениями.", "text", 'required maxlength="200"')}${textField("author", "Автор", draft.author, "Оставьте пустым, если не указан.", "text", 'maxlength="180"')}${dateField("publishedAt", "Дата публикации", draft.publishedAt)}${selectField("kind", "Характер материала", [["Не классифицирован", "Требует проверки"], ["Прогноз автора", "Прогноз автора"], ["Мнение", "Мнение"], ["Наблюдение", "Наблюдение / данные"]], draft.kind)}<div class="full">${textarea("text", "Текст материала", draft.text, "До 30 000 знаков.", 'required maxlength="30000"')}</div></div><div class="actions"><button type="submit" class="btn primary" ${busy ? "disabled" : ""}>Добавить в анализ</button>${B("Отмена", "form-cancel", "secondary")}</div></div>`}</form></div>`;
}

function researchList() {
  return (
    intro(
      "Анализ периода",
      "Выберите даты, сравните показатели и проверьте возможные связи с новостями.",
      B("Исследовать период", "nav:research-new", "primary", "calendar"),
    ) +
    `<p class="demo-note">${I("info")}Показатели компании в прототипе демонстрационные.</p>` +
    (state.research.length
      ? `<div class="card research-list">${state.research.map((r) => `<article class="research-card"><div class="eyebrow">${date(r.start)} — ${date(r.end)}</div><h2><a href="#report/${r.id}">${esc(r.question)}</a></h2><p class="source-meta">${r.coverage.days} ${plural(r.coverage.days,"день","дня","дней")} · ${r.coverage.events} ${plural(r.coverage.events,"событие","события","событий")}${r.delta !== null ? " · заказы " + (r.delta > 0 ? "+" : "") + num(r.delta, 1) + "%" : ""}</p><div class="actions">${B("Открыть", "nav:report/" + r.id, "secondary")}${B("Повторить анализ", "research-repeat:" + r.id, "text", "refresh")}</div></article>`).join("")}</div>`
      : empty(
          "Посмотрите, что изменилось",
          "Выберите период и задайте вопрос: например, как менялись заказы и затраты.",
          B("Выбрать период", "nav:research-new", "primary", "calendar"),
        ))
  );
}

function researchForm() {
  return (
    back(returnTo) +
    `<div class="form-page">${intro("Исследовать период", "Выберите даты и сформулируйте, что хотите проверить.")}<form id="research-form" class="card">${formError()}<div class="form-grid"><div class="full">${selectField(
      "period",
      "Период",
      [
        ["q3", "III квартал 2026"],
        ["oct", "1–22 октября"],
        ["custom", "Свой интервал"],
      ],
      draft.period,
    )}</div>${dateField("start", "Начало", draft.start, "2026-04-01", E.TODAY)}${dateField("end", "Окончание", draft.end, "2026-04-01", E.TODAY)}<p class="field-hint full">Сравним с предыдущим периодом той же длины.</p><div class="full">${textarea("question", "Что хотите проверить?", draft.question, "Например: как менялись заказы и затраты.", 'required maxlength="2000"')}</div></div><div class="actions"><button type="submit" class="btn primary">Исследовать</button>${B("Отмена", "form-cancel", "secondary")}</div><p class="demo-note">Показатели компании демонстрационные · апрель — октябрь 2026.</p></form></div>`
  );
}

function chart(rows) {
  if (!rows.length) return "";
  const w = Math.max(
      200,
      Math.min(
        760,
        document.documentElement.clientWidth <= 700
          ? document.documentElement.clientWidth - 78
          : ($(".workspace")?.clientWidth || 900) - 58,
      ),
    ),
    h = 220,
    pad = 34,
    x = (i) =>
      pad + (w - pad * 2) * (rows.length === 1 ? 0.5 : i / (rows.length - 1));
  const scaled = rows.map((r) => ({
    ...r,
    orders: (100 * r.orders) / rows[0].orders,
    market: (100 * r.market) / rows[0].market,
  }));
  const vals = scaled.flatMap((r) => [r.orders, r.market]),
    min = Math.min(...vals) - 5,
    max = Math.max(...vals) + 5,
    y = (v) => h - pad - ((v - min) / (max - min)) * (h - pad * 2);
  const line = (k) =>
    scaled
      .map(
        (r, i) => (i ? "L" : "M") + x(i).toFixed(1) + "," + y(r[k]).toFixed(1),
      )
      .join(" ");
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Динамика синтетических заказов и индекса рынка за выбранный период">${[
    0, 1, 2,
  ]
    .map((i) => {
      const v = min + ((max - min) * i) / 2;
      return `<path d="M${pad},${y(v)} H${w - pad}" stroke="#e0e4eb"/><text x="0" y="${y(v) + 4}">${Math.round(v)}</text>`;
    })
    .join(
      "",
    )}<path d="${line("orders")}" fill="none" stroke="#cf251b" stroke-width="2.5"/><path d="${line("market")}" fill="none" stroke="#1561ae" stroke-width="2" stroke-dasharray="5 3"/><text x="${pad}" y="${h - 5}">${new Date(rows[0].date + "T12:00Z").toLocaleDateString("ru-RU", { day: "numeric", month: "short" })}</text><text x="${w - pad}" y="${h - 5}" text-anchor="end">${new Date(rows.at(-1).date + "T12:00Z").toLocaleDateString("ru-RU", { day: "numeric", month: "short" })}</text></svg>`;
}
function researchReport() {
  const r = state.research.find((r) => r.id === view.id);
  if (!r)
    return empty(
      "Исследование не найдено",
      "Откройте результат из списка исследований.",
      B("К исследованиям", "nav:research"),
    );
  const changed = E.hash(r.context.facts) !== E.hash(state.company.facts),
    sourceChanged =
      E.hash(r.sourceSnapshot) !==
      E.hash(
        E.activeSources(state).map((s) => ({ id: s.id, hash: E.hash(s.text) })),
      );
  const delta =
    r.delta === null
      ? "Нет базы"
      : (r.delta > 0 ? "+" : "") + num(r.delta, 1) + "%";
  return (
    back("research", "К анализу периода") +
    intro(
      "Изменения за период",
      `${date(r.start)} — ${date(r.end)} · ${r.context.name}`,
      B("Повторить анализ", "research-repeat:" + r.id, "secondary", "refresh") +
        B(
          "",
          "report-menu:" + r.id,
          "iconbtn",
          "more",
          'aria-label="Действия с исследованием" aria-haspopup="menu" aria-expanded="false"',
        ),
    ) +
    `<p class="research-question">${esc(r.question)}</p><p class="demo-note">${I("info")}Демонстрационные показатели. Совпадения не доказывают причину.</p>${changed || sourceChanged ? `<div class="update-result">${I("refresh")}<span>Условия или материалы изменились после расчёта. Для текущей оценки повторите анализ.</span></div>` : ""}${!r.supported ? '<div class="update-result warning">На этот вопрос расчётный режим не отвечает. Ниже показана доступная динамика периода.</div>' : ""}
 <div class="card research-result"><h2>Динамика бизнеса</h2><div class="stats"><div class="stat"><span>Заказы</span><strong>${delta}</strong><span>${r.complete ? "К предыдущим " + r.rows.length + " " + plural(r.rows.length,"дню","дням","дням") : "Предыдущий период покрыт не полностью"}</span></div><div class="stat"><span>Маржа</span><strong>${num(r.margin, 1)}%</strong><span>За выбранный период</span></div><div class="stat"><span>События</span><strong>${r.coverage.events}</strong><span>${r.coverage.sources} ${plural(r.coverage.sources,"материал","материала","материалов")}</span></div></div>${chart(r.rows)}<div class="legend"><span>Заказы</span><span>Индекс рынка</span></div><p class="caption">Оба ряда приведены к 100 на начало периода. ${r.coverage.days} ${plural(r.coverage.days,"день","дня","дней")} наблюдений.</p></div>
 <section class="content-section"><h2>Какие совпадения проверить</h2><div class="disclosure-stack">${disclosure("Цены материалов и доля затрат", `<p>${r.materialCorrelation === null ? "Для сравнения нужно хотя бы 14 дней." : "Корреляция: " + num(r.materialCorrelation, 2) + ". " + (r.materialCorrelation > 0.3 ? "Ряды двигались в одном направлении. Это согласуется с возможной передачей цены в затраты, но не подтверждает причину." : r.materialCorrelation < -0.3 ? "Обратное движение не подтверждает простую передачу роста цен в долю затрат." : "Заметной линейной связи в интервале нет.")}</p><p>Проверьте другие объяснения: объём закупок, ассортимент и структуру производства.</p>`)}${disclosure("Индекс рынка и заказы", `<p>${r.bestLag ? "Самое заметное совпадение среди проверенных сдвигов — " + r.bestLag.lag + " " + plural(r.bestLag.lag,"день","дня","дней") + ". Корреляция: " + num(r.bestLag.r, 2) + ", наблюдений: " + r.bestLag.n + "." : "Недостаточно наблюдений для проверки временных сдвигов."}</p><p>Нужны реальные показатели, сезонный контроль и проверка на другом периоде.</p>`)}</div></section>
 <section class="content-section"><div class="section-heading"><h2>События в этом периоде</h2><span class="muted">${r.events.length}</span></div>${r.events.length ? `<div class="card period-events">${r.events.map((event) => `<a class="period-event" href="#source/${esc(event.sources[0].id)}"><span>${date(event.date)}</span><strong>${esc(event.title)}</strong>${I("right")}</a>`).join("")}</div>` : '<p class="muted">В подключённых источниках нет публикаций за выбранные даты.</p>'}</section>
 <div class="disclosure-stack">${disclosure("Метод и ограничения", `<p>${esc(r.limits)}</p><p>Сохранены условия компании и состав материалов на момент расчёта. Сравниваются одинаковые по длине интервалы.</p>`)}${disclosure("Дневные показатели · " + r.rows.length + " " + plural(r.rows.length,"строка","строки","строк"), `<div class="table-scroll"><table class="raw-table"><thead><tr><th>Дата</th><th>Заказы</th><th>Выручка</th><th>Затраты</th></tr></thead><tbody>${r.rows.map((row) => `<tr><td>${date(row.date)}</td><td>${num(row.orders)}</td><td>${num(row.revenue)}</td><td>${num(row.cost)}</td></tr>`).join("")}</tbody></table></div>`)}</div><div class="actions">${B("Обсудить результат", "ask-report:" + r.id, "secondary", "chat")}</div>`
  );
}

function digest() {
  const d = E.digest(state, digestPeriod);
  return (
    back("overview", "К главному") +
    intro(
      digestPeriod === "week" ? "Сводка за неделю" : "Сводка за день",
      `${state.company.name} · текущие приоритеты и действия, обновления за день или неделю. Можно скачать HTML или сохранить PDF через печать.`,
      B("Скачать HTML", "digest-download", "secondary", "file") + B("Сохранить PDF", "print", "quiet", "file"),
    ) +
    `<div class="filterbar"><div class="segmented" aria-label="Период сводки">${[
      ["day", "За день"],
      ["week", "За неделю"],
    ]
      .map(
        ([p, l]) =>
          `<button data-action="digest-period:${p}" class="${digestPeriod === p ? "active" : ""}" aria-pressed="${digestPeriod === p}">${l}</button>`,
      )
      .join(
        "",
      )}</div><span class="freshness">${d.changes.length ? "Пересмотров: " + d.changes.length : "Новых пересмотров нет"} · событий: ${d.events.length}</span></div><div class="card digest-list">${d.items.map((a) => `<article class="digest-item"><div class="eyebrow">${esc(a.metric)}</div><h2><a href="#detail/${a.id}">${esc(briefTitle(a))}</a></h2><p>${esc(briefImpact(a))}</p><div class="digest-next"><span>Проверить</span><strong>${esc(a.next)}</strong></div><a class="inline-link" href="#detail/${a.id}">Открыть разбор ${I("right")}</a></article>`).join("")}</div>${d.actions.length ? `<div class="section-heading"><h2>Принятые действия · ${d.actions.length}</h2>${B("Все действия", "nav:actions", "text", "right")}</div><div class="card">${d.actions.map(([id, task]) => `<a class="task-summary" href="#actions/${id}"><div><h3>${esc(E.assess(state, id)?.next || id)}</h3><p>${esc(actionLabel(task.status))}${task.due ? " · до " + date(task.due) : ""}</p></div>${I("right")}</a>`).join("")}</div>` : ""}`
  );
}

function actionsPage() {
  const ids = view.id ? [view.id] : Object.keys(state.actions);
  return (
    back("overview", "К главному") +
    intro("Мои действия", "Проверки и решения, к которым нужно вернуться.") +
    (ids.length
      ? ids
          .map((id) => {
            const a = E.assess(state, id),
              task = state.actions[id];
            if (!a || !task) return "";
            return `<section class="card task-card"><div class="eyebrow">${esc(briefTitle(a))}</div><div class="task-heading"><h2>${esc(a.next)}</h2>${badge(actionLabel(task.status), task.status === "done" ? "green" : "")}</div><p class="task-meta">${task.owner ? esc(task.owner) : "Ответственный не указан"}${task.due ? " · до " + date(task.due) : ""}</p><div class="actions">${B("Изменить статус", "task-menu:" + id, "secondary", "down", 'aria-haspopup="menu" aria-expanded="false"')}${B("Открыть разбор", "nav:detail/" + id, "text", "right")}</div>${disclosure("Ответственный и заметка", `<form class="task-form" data-task="${id}">${textField("owner", "Ответственный", task.owner || "", "", "text", 'maxlength="120"')}${dateField("due", "Вернуться к действию", task.due || "", "", "", id)}<div class="full">${textarea("note", "Результат проверки", task.note || "", "Заметка сохраняется отдельно от подтверждённых данных.", 'maxlength="3000"')}</div><div class="actions full"><button class="btn secondary" type="submit">Сохранить детали</button></div></form>`, "task-details", !!view.id)}</section>`;
          })
          .join("")
      : empty(
          "Пока нет действий",
          "В разборе ситуации можно взять вопрос на проверку или начать наблюдение.",
          B("К ситуациям", "nav:overview"),
        ))
  );
}

function settings() {
  return (
    intro(
      "Настройки модуля",
      "Управляйте частотой проверок и рабочим контекстом.",
    ) +
    `<div class="card form-page"><section class="section"><h2>Обновления</h2><label class="switch"><span>Проверять новости каждые 5 минут<div class="hint">${integration.news ? "Пока открыт сайт. Сбой источника не удаляет предыдущие оценки." : "Загрузка новостей сейчас недоступна."}</div></span><span class="row"><input type="checkbox" id="auto-check" ${integration.news && root.auto ? "checked" : ""} ${integration.news ? "" : "disabled"}><span class="track"></span></span></label></section><section class="section"><h2>Ваш фокус</h2>${selectField(
      "role",
      "Показывать в первую очередь",
      [
        ["owner", "Общий взгляд на бизнес"],
        ["finance", "Денежный поток и финансирование"],
        ["supply", "Закупки и поставки"],
      ],
      root.role,
    )}<p class="hint" style="margin-top:12px">Меняет порядок ситуаций. Это приоритет просмотра, а не настройка прав доступа.</p></section><section class="section"><h2>Помощник</h2><label class="switch"><span>Показывать пиксельного персонажа</span><span class="row"><input type="checkbox" id="pet-check" ${root.pet ? "checked" : ""}><span class="track"></span></span></label></section><section class="section"><h2>Предложения банка</h2><label class="switch"><span>Показывать подходящие продукты<div class="hint">Предложения можно скрыть. На оценки ситуаций это не влияет.</div></span><span class="row"><input type="checkbox" id="bank-offers" ${state.bankOffers !== false ? "checked" : ""}><span class="track"></span></span></label></section><section class="section"><h2>Подключения</h2><div class="integration-list"><div class="row"><div><strong>Официальные новости ЦБ</strong><p>${integration.news && root.lastFeed ? "Проверка: " + time(root.lastFeed.checkedAt) : "Загрузка новостей сейчас недоступна в этой сборке."}</p></div>${badge(!integration.news ? "Не подключено" : root.lastFeed?.status === "ok" ? "Подключено" : root.lastFeed?.status === "partial" ? "Частично" : "Ожидает проверки", integration.news && root.lastFeed?.status === "ok" ? "green" : "")}</div><div class="row"><div><strong>Банковская аналитика</strong><p>Два отчёта кейса: прочитаны первые страницы с ключевыми выводами.</p></div>${badge("Загружено", "blue")}</div><div class="row"><div><strong>Модель для глубокого анализа</strong><p>${integration.llm ? "Настроен серверный шлюз. Выводы проверяются по ссылкам на материалы." : "Ключ модели не настроен. Доступен расчётный режим с явными ограничениями."}</p></div>${badge(integration.llm ? "Шлюз настроен" : "Не подключена", integration.llm ? "green" : "")}</div></div></section><div class="actions">${B("Проверить сейчас", "refresh:all", "secondary", "refresh", integration.news ? "" : 'disabled title="Загрузка новостей сейчас недоступна."')}${B("О возможностях и данных", "nav:about", "text")}</div></div>`
  );
}
function about() {
  return (
    intro("О прототипе", "Бизнес-пульс · версия 10") +
    `<div class="card prose"><section class="section"><h2>Что уже работает</h2><p>Связанные ситуации, управление сохранёнными источниками, ручное добавление текста, отдельные факты и допущения, пересмотр с версиями, статусы действий, сводки, исследования произвольных дней доступного периода, выгрузка и контекстный помощник.</p></section><section class="section"><h2>Какие данные используются</h2><p>Три обезличенных профиля из CSV кейса. Названия компаний вымышлены. Банковские выводы пересказаны с первых страниц двух оригинальных отчётов. Учебные отраслевые материалы и показатели отмечены как демоданные.</p><p>Исходные документы кейса описывают сценарий хакатона. Они не заменяют проверку актуального состояния рынка. Сайт не подключён к счетам, платежам и внутренним системам банка.</p></section><section class="section"><h2>Анализ и помощник</h2><p>Без настроенной модели используются воспроизводимые правила и расчёты. Шлюз LLM поддерживает план, ограниченный выбор материалов, проверку ссылок и один повтор при ошибке. ${integration.llm ? "Настройки шлюза есть; доступность проверяется при запросе." : "В этой публикации ключ LLM отсутствует; ответы по правилам не выдаются за ответ нейросети."}</p><p>Система не наблюдает за «всем миром»: полнота зависит от подключённых источников, доступа и периода. Слабые косвенные связи показываются как гипотезы с альтернативами и условиями опровержения.</p></section><section class="section"><h2>Хранение и границы</h2><p>Профиль, уточнения, история, задачи и переписка хранятся в этом браузере отдельно по компаниям. Это не многопользовательская банковская система. Роли меняют приоритет просмотра; серверные права банка не моделируются.</p><p>Загрузка новостей и получение текста по ссылке сейчас не подключены. Автоматическая проверка недоступна. Уведомления доступны внутри модуля; push, рассылки и отправка менеджеру не подключены.</p></section><div class="actions">${B("Проверить поток из 150 материалов", "nav:events/volume", "secondary", "file")}${B("Экспортировать состояние", "export-state", "secondary", "file")}${B("Сбросить демо", "reset-confirm", "danger")}</div></div>`
  );
}
function feedbackForm() {
  const a = E.assess(state, view.id);
  return (
    back(returnTo) +
    `<div class="form-page">${intro("Уточнить качество оценки", a?.title || "Сообщите, что стоит исправить.")}<form id="feedback-form" class="card">${formError()}${selectField(
      "type",
      "Что не так?",
      [
        ["wrong", "Неверная связь с бизнесом"],
        ["source", "Проблема с источником"],
        ["missing", "Не хватает данных"],
        ["other", "Другое"],
      ],
      draft.type,
    )}<div style="margin-top:24px">${textarea("text", "Комментарий", draft.text, "Сохраним в этом браузере. Для изменения оценки уточните факт или исключите источник.", 'required maxlength="3000"')}</div><div class="actions"><button class="btn primary" type="submit">Сохранить отзыв</button>${B("Уточнить данные", "context:" + (view.id || "all"), "secondary")}</div></form></div>`
  );
}
let assistantContext = null;
function renderAssistant() {
  const mount = $("#assistant");
  if (!assistant) {
    mount.innerHTML = "";
    return;
  }
  const ctx =
    assistantContext?.type === "source"
      ? root.sources.find((s) => s.id === assistantContext.id)?.title
      : assistantContext?.type === "report"
        ? state.research.find((r) => r.id === assistantContext.id)?.question
        : assistantContext?.type === "manager"
          ? "Черновик вопроса менеджеру"
          : E.assess(state, assistantContext?.id || view.id)?.title;
  mount.innerHTML = `<section class="assistant-panel ${expanded ? "expanded" : ""}" role="dialog" aria-label="Помощник по бизнес-контексту" id="assistant-dialog"><header class="assistant-head">${root.pet ? '<img src="/assets/03_listening_right.webp" alt="">' : I("chat")}<div><strong>Помощник</strong><small>${esc(state.company.name)}</small></div><div class="controls">${iconButton(expanded ? "minimize" : "maximize", "assistant-expand", expanded ? "Свернуть помощника" : "Развернуть помощника")}${iconButton("close", "assistant-close", "Закрыть помощника")}</div></header><div class="assistant-context">${ctx ? "Контекст: " + esc(ctx) : "Контекст: текущие ситуации компании"} · ${integration.llm ? "серверный анализ доступен" : "по сохранённым данным"}</div><div class="chat-body" id="chat-log" role="log" aria-live="polite">${
    state.messages.length
      ? state.messages
          .slice(-30)
          .map(
            (m) =>
              `<div class="message ${m.role === "user" ? "user" : ""}">${m.text
                .split("\n")
                .filter(Boolean)
                .map((p) => `<p>${esc(p)}</p>`)
                .join(
                  "",
                )}${m.action ? B(esc(m.label || "Открыть"), m.action, "secondary") : ""}${m.mode ? `<span class="hint">${esc(m.mode)}</span>` : ""}</div>`,
          )
          .join("")
      : `<div class="message"><p>Могу объяснить основание, запустить пересмотр или исследовать период. Уточнение факта проверим перед применением к профилю.</p></div><div class="chat-suggestions">${["Что изменилось и что проверить?", "Обнови гипотезы", "Исследуй 1–15 октября: заказы и затраты"].map((x) => `<button data-action="suggest:${esc(x)}">${esc(x)}</button>`).join("")}</div>`
  }${chatBusy ? '<div class="row hint" role="status"><span class="spinner"></span>Проверяю контекст и основания…</div>' : ""}</div><form id="chat-form" class="chat-form"><div class="chat-compose"><textarea id="chat-input" name="message" aria-label="Сообщение помощнику" placeholder="Спросите или поручите анализ" maxlength="2000" ${chatBusy ? "disabled" : ""}></textarea><button type="submit" aria-label="Отправить" ${chatBusy ? "disabled" : ""}>${I("send")}</button></div><div class="hint">Выводы нужно проверять. Секретные данные в чат не добавляйте.</div></form></section>`;
  const log = $("#chat-log");
  log.scrollTop = log.scrollHeight;
}
function addMessage(text, action = "", label = "", mode = "") {
  state.messages.push({
    role: "assistant",
    text,
    action,
    label,
    mode,
    at: new Date().toISOString(),
  });
  state.messages = state.messages.slice(-80);
  persist();
  renderAssistant();
}
function closeAssistant() {
  assistant = false;
  expanded = false;
  renderAssistant();
  $(".assistant-button")?.setAttribute("aria-expanded", "false");
  $(".assistant-button")?.classList.remove("active");
  $(".assistant-button")?.focus();
}
function menu(anchor, id) {
  UI.show(
    anchor,
    UI.item(
      C.bucket(E.assess(state, id), state) === "watch" ? "Убрать из наблюдений" : "Наблюдать",
      "watch:" + id,
      "book",
    ) +
      UI.item("Обновить оценку", "refresh:" + id, "refresh") +
      `<button type="button" class="menu-item" role="menuitem" data-ui="sub" data-case="${id}" aria-haspopup="menu" aria-expanded="false">${I("bell")}<span>Уведомления</span>${I("right")}</button><div class="menu-divider"></div>` +
      UI.item("Уточнить данные", "context:" + id, "pencil") +
      UI.item("Проверить допущение", "scenario:" + id, "context") +
      UI.item("Обратная связь", "feedback:" + id, "chat") +
      UI.item("Скрыть из главного", "hide:" + id, "eyeoff"),
    "Действия с ситуацией",
  );
}
function notifyMenu(anchor, id) {
  state.notify ||= {};
  UI.nested(
    anchor,
    [
      ["important", "Значимые изменения"],
      ["digest", "В сводке"],
      ["off", "Не уведомлять"],
    ]
      .map(
        ([v, l]) =>
          `<button type="button" class="menu-item" role="menuitemradio" aria-checked="${(state.notify[id] || "important") === v}" data-action="notify:${id}:${v}"><span>${l}</span>${(state.notify[id] || "important") === v ? I("check") : ""}</button>`,
      )
      .join(""),
  );
}
function notificationPopover(anchor) {
  if (assistant) closeAssistant();
  UI.show(
    anchor,
    `<div class="popover-head"><h2>Обновления</h2><div class="row">${iconButton("check", "notifications-read", "Прочитать все")}${iconButton("close", "close-pop", "Закрыть обновления")}</div></div>${
      state.notifications.length
        ? state.notifications
            .slice(0, 20)
            .map(
              (n) =>
                `<button class="notification-row ${!n.read ? "unread" : ""}" data-action="notification:${n.id}"><strong>${esc(n.title)}</strong>${esc(n.text)}<span class="hint">${time(n.at)}</span></button>`,
            )
            .join("")
        : '<div style="padding:24px 20px"><strong>Пока нет обновлений</strong><p class="hint" style="margin-top:10px">Здесь появятся изменения оценок и готовые исследования.</p></div>'
    }<div class="popover-foot">${B("Журнал обновлений", "nav:history", "text", "clock")}</div>`,
    "Обновления",
    "dialog",
    "notifications",
  );
}
let dirty = false;
function perform(action, anchor) {
  const [type, id, value] = action.split(":");
  if (pulse.action(type, id, view.page)) return;
  if (type === "demo-login") {
    error = ""; coreError = ""; coreMode = false; coreUser = null; demoSignedIn = true;
    try { sessionStorage.setItem("business-context-demo-session-v2", "1"); } catch {}
    navigate("home"); render(); return;
  }
  if (type === "core-company-menu") {
    UI.show(anchor, `<div class="menu-caption">Доступные компании</div>` + coreCompanies.map((c) => UI.item(`<strong>${esc(coreCompanyLabel(c))}</strong><span class="hint">${esc(c.mainokved_name || c.region || "")}</span>`, "switch-core-company:" + c.id, "building")).join(""), "Выбор компании");
    return;
  }
  if (type === "switch-core-company") {
    if (dirty) { toast("Сначала сохраните изменения или отмените редактирование."); return; }
    UI.close();
    loadCoreCompany(id).then(() => { if (!coreError) navigate("home"); });
    return;
  }
  if (type === "core-logout") {
    coreCompanyGeneration++;
    coreClient.logout().catch(() => {}).finally(() => {
      coreMode = false; coreUser = null; coreCompanies = []; coreCompany = null;
      coreContext = coreDashboardData = null; coreMetrics = coreNewsSources = coreNewsDocuments = [];
      coreMetricEvents = []; coreMetricEventsId = ""; coreMetricEventsLoading = false;
      dirty = false; draft = {};
      error = ""; location.hash = "login"; render();
    });
    return;
  }
  if (type === "demo-logout") {
    demoSignedIn = false;
    assistant = false;
    UI.close();
    try { sessionStorage.removeItem("business-context-demo-session-v2"); } catch {}
    location.hash = "login";
    render();
    return;
  }
  if (
    dirty &&
    ["nav", "switch-company", "context", "scenario", "feedback"].includes(
      type,
    ) &&
    anchor
  ) {
    UI.show(
      anchor,
      `<div class="confirm-popover"><h3>Перейти без сохранения?</h3><p>Введённые изменения пока не сохранены.</p><div class="actions">${B("Продолжить редактирование", "close-pop", "secondary")}${B("Перейти", "leave-form:" + encodeURIComponent(action), "danger")}</div></div>`,
      "Несохранённые изменения",
      "dialog",
    );
    return;
  }
  const keep = [
    "more",
    "company-menu",
    "notifications",
    "notify-sub",
    "source-more",
    "source-confirm",
    "task-menu",
    "reset-confirm",
    "form-cancel",
    "publisher-confirm",
    "report-menu",
    "decision-menu",
    "module-menu",
  ];
  if (!keep.includes(type)) UI.close();
  if (type === "leave-form") {
    dirty = false;
    perform(decodeURIComponent(id), anchor);
    return;
  }
  if (type === "module-menu") {
    UI.show(anchor, UI.item("Источники и издания", "nav:sources", "file") + UI.item("Данные компании", "nav:company", "building") + UI.item("Настройки модуля", "nav:settings", "settings") + '<div class="menu-divider"></div>' + UI.item("О прототипе", "nav:about", "info"), "Управление модулем");
  } else if (type === "report-menu") {
    UI.show(
      anchor,
      UI.item("Скачать CSV", "research-csv:" + id, "file") +
        UI.item("Напечатать / сохранить PDF", "print", "file"),
      "Действия с исследованием",
    );
  } else if (type === "case-view") {
    inventoryDomain = "all";
    inventoryPage = 0;
    listMemory.delete("situations/" + id);
    navigate("situations/" + id);
  } else if (type === "case-domain") {
    inventoryDomain = id;
    inventoryPage = 0;
    render();
  } else if (type === "case-page") {
    inventoryPage = +id;
    render();
    document
      .querySelector(".catalog-filter")
      ?.scrollIntoView({ block: "start" });
  } else if (type === "case-reset") {
    inventoryDomain = "all";
    query = "";
    inventoryType = "all";
    inventoryPage = 0;
    render();
  } else if (type === "event-filters") {
    advancedNewsFilters = !advancedNewsFilters;
    render();
  } else if (type === "event-domain") {
    eventDomain = id;
    eventPage = 0;
    render();
  } else if (type === "event-new") {
    eventOnlyNew = !eventOnlyNew;
    eventPage = 0;
    render();
  } else if (type === "event-reset") {
    eventDomain = "all";
    eventQuery = "";
    eventKind = "all";
    eventPeriod = "all";
    eventOnlyNew = false;
    eventPage = 0;
    render();
  } else if (type === "event-page") {
    eventPage = +id;
    render();
    document.querySelector(".news-filter")?.scrollIntoView({ block: "start" });
  } else if (type === "offer-dismiss") {
    state.dismissedOffers[id] = Date.now();
    persist();
    render();
    toast("Предложение скрыто на 30 дней.", "offer-restore:" + id);
  } else if (type === "offer-restore") {
    delete state.dismissedOffers[id];
    persist();
    render();
  } else if (type === "decision-menu") {
    const task = state.actions[id];
    UI.show(
      anchor,
      (task
        ? UI.item("Изменить статус", "task-menu:" + id, "check")
        : UI.item("Наблюдать", "task-watch:" + id, "book") +
          UI.item("Отложить", "task-deferred:" + id, "clock")) +
        '<div class="menu-divider"></div>' +
        UI.item("Проверить допущение", "scenario:" + id, "context") +
        UI.item("Вопрос менеджеру", "manager:" + id, "chat"),
      "Другие действия",
    );
  } else if (type === "nav") navigate(action.slice(4));
  else if (type === "mobile") {
    mobile = !mobile;
    render();
  } else if (type === "bank")
    toast("Раздел «" + id + "» находится за пределами прототипа аналитики.");
  else if (type === "filter") {
    filter = id;
    render();
  } else if (type === "only-watch") {
    onlyWatch = !onlyWatch;
    render();
  } else if (type === "show-all") {
    showAll = !showAll;
    render();
  } else if (type === "clear-filter") {
    filter = "all";
    query = "";
    onlyWatch = false;
    render();
  } else if (type === "more") menu(anchor, id);
  else if (type === "watch") {
    const wasWatching = C.bucket(E.assess(state, id), state) === "watch";
    C.setWorkStatus(state, id, wasWatching ? "todo" : "watch", state.assessments[id]?.version);
    persist();
    render();
    toast(
      wasWatching ? "Наблюдение снято. Ситуация снова ожидает проверки." : "Ситуация перенесена в наблюдения.",
      "nav:situations/" + (wasWatching ? "attention" : "watch"),
      wasWatching ? "Открыть проверки" : "Открыть наблюдения",
    );
  } else if (type === "hide") {
    state.hidden = [...new Set([...state.hidden, id])];
    persist();
    navigate("overview");
    toast("Ситуация скрыта из главного.", "unhide:" + id);
  } else if (type === "unhide") {
    state.hidden = state.hidden.filter((x) => x !== id);
    persist();
    render();
  } else if (type === "restore-hidden") {
    state.hidden = [];
    persist();
    render();
  } else if (type === "notify-sub") notifyMenu(anchor, anchor.dataset.case);
  else if (type === "notify") {
    state.notify ||= {};
    state.notify[id] = value;
    persist();
    toast("Предпочтение уведомлений сохранено.");
  } else if (type === "company-menu") {
    UI.show(
      anchor,
      `<div class="menu-caption">Демонстрационные компании</div>` +
        Object.values(root.companies)
          .map((c) =>
            UI.item(
              `<strong>${esc(c.company.name)}</strong><span class="hint">${esc(c.company.raw.MAINOKVED_NAME)}</span>`,
              "switch-company:" + c.company.id,
              "building",
            ),
          )
          .join(""),
      "Выбор компании",
    );
  } else if (type === "switch-company") {
    if (busy) {
      toast("Дождитесь окончания проверки или отмените её.");
      return;
    }
    const companyDestination = view.page === "home" ? "home" : view.page === "account" ? "account" : "overview";
    if (view.page === "account") draft = { ...root.profile };
    root.active = id;
    listMemory.clear();
    inventoryDomain = "all";
    inventoryPage = 0;
    eventDomain = "all";
    eventQuery = "";
    sync();
    assistantContext = null;
    lastResult = "";
    query = "";
    onlyWatch = false;
    filter = "all";
    eventPage = 0;
    persist();
    navigate(companyDestination);
    toast("Выбрана компания " + state.company.name + ".");
  } else if (type === "context") navigate("context/" + id);
  else if (type === "scenario") navigate("context/" + id + "/scenario");
  else if (type === "scenario-delete") {
    delete state.scenarios[id];
    persist();
    render();
    toast("Допущение удалено.");
  } else if (type === "scenario-apply") {
    const changes = E.updateFacts(
      state,
      state.scenarios[id],
      "Применено ваше допущение",
    );
    delete state.scenarios[id];
    persist();
    render();
    toast("Профиль обновлён. Пересмотрено оценок: " + changes.length + ".");
  } else if (type === "form-cancel") {
    if (dirty)
      UI.show(
        anchor,
        `<div class="confirm-popover"><h3>Закрыть без сохранения?</h3><p>Введённые изменения не попадут в профиль или исследование.</p><div class="actions">${B("Продолжить", "close-pop", "secondary")}${B("Закрыть", "discard", "danger")}</div></div>`,
        "Несохранённые изменения",
        "dialog",
      );
    else navigate(returnTo);
  } else if (type === "discard") {
    dirty = false;
    navigate(returnTo);
  } else if (type === "close-pop") UI.close(true);
  else if (type === "source-manual") {
    Object.assign(draft, Object.fromEntries(new FormData($("#source-form"))));
    sourceStage = "manual";
    error = "";
    render();
    document.querySelector('[name="title"]')?.focus();
  } else if (type === "source-filter") {
    sourceFilter = id;
    sourcePage = 0;
    render();
  } else if (type === "source-page") {
    sourcePage = +id;
    render();
    document.querySelector(".filterbar")?.scrollIntoView({ block: "start" });
  } else if (type === "publisher-confirm") {
    const s = root.sources.find((s) => s.id === id);
    UI.show(
      anchor,
      `<div class="confirm-popover"><h3>Не использовать издание?</h3><p>Все текущие и будущие материалы «${esc(s.publisher)}» будут исключены из анализа. Состав предыдущих версий сохранится.</p><div class="actions">${B("Исключить издание", "publisher-exclude:" + id, "primary")}${B("Отмена", "close-pop", "secondary")}</div></div>`,
      "Исключение издания",
      "dialog",
    );
  } else if (type === "publisher-exclude") {
    const s = root.sources.find((s) => s.id === id),
      pid = E.hash(s.independence || s.publisher);
    root.excludedPublishers ||= [];
    if (!root.excludedPublishers.some((x) => x.id === pid))
      root.excludedPublishers.push({
        id: pid,
        label: s.publisher,
        group: s.independence || s.publisher,
      });
    applyPublisherBlocks();
    updateAll("Исключено издание: " + s.publisher);
    render();
    toast(
      "Текущие и будущие материалы издания исключены.",
      "publisher-restore:" + pid,
      "Вернуть",
    );
  } else if (type === "publisher-restore") {
    const entry = (root.excludedPublishers || []).find((x) => x.id === id);
    if (entry) {
      root.excludedPublishers = root.excludedPublishers.filter(
        (x) => x.id !== id,
      );
      for (const s of root.sources)
        if (s.excludedByPublisher && s.excludedByPublisher === id) {
          s.excluded = false;
          delete s.excludedByPublisher;
        }
      updateAll("Восстановлено издание: " + entry.label);
      render();
      toast("Издание возвращено в анализ.");
    }
  } else if (type === "source-more") {
    const s = root.sources.find((s) => s.id === id);
    UI.show(
      anchor,
      UI.item("Читать материал", "nav:source/" + id, "file") +
        '<div class="menu-divider"></div>' +
        UI.item(
          s.excluded ? "Вернуть в анализ" : "Исключить из анализа",
          (s.excluded ? "source-restore:" : "source-confirm:") + id,
          "eyeoff",
        ) +
        UI.item(
          "Не использовать всё издание",
          "publisher-confirm:" + id,
          "eyeoff",
        ),
      "Источник",
    );
  } else if (type === "source-confirm") {
    const s = root.sources.find((s) => s.id === id);
    UI.show(
      anchor,
      `<div class="confirm-popover"><h3>Исключить источник?</h3><p>«${esc(s.title)}» не будет основанием новых оценок. История сохранится.</p><div class="actions">${B("Исключить", "source-exclude:" + id, "primary")}${B("Отмена", "close-pop", "secondary")}</div></div>`,
      "Исключение источника",
      "dialog",
    );
  } else if (type === "source-exclude" || type === "source-restore") {
    const s = root.sources.find((s) => s.id === id);
    if (type === "source-restore" && s.excludedByPublisher) {
      toast(
        "Для материала исключено всё издание. Верните издание в анализ.",
        "publisher-restore:" + s.excludedByPublisher,
        "Вернуть издание",
      );
      return;
    }
    s.excluded = type === "source-exclude";
    const n = updateAll(
      s.excluded
        ? "Исключён источник: " + s.title
        : "Восстановлен источник: " + s.title,
    );
    render();
    toast(
      (s.excluded ? "Источник исключён." : "Источник восстановлен.") +
        " Пересмотрено оценок: " +
        n +
        ".",
      s.excluded ? "source-restore:" + id : "",
    );
  } else if (type === "refresh") refresh(id);
  else if (type === "cancel-job") {
    busy?.controller.abort();
    busy = null;
    render();
    toast("Проверка отменена. Последние оценки сохранены.");
  } else if (type === "events-prev" || type === "events-next") {
    eventPage = Math.max(0, eventPage + (type === "events-next" ? 1 : -1));
    render();
    window.scrollTo(0, 0);
  } else if (type === "digest-period") {
    digestPeriod = id;
    render();
  } else if (type === "task-menu") {
    UI.show(
      anchor,
      ["review", "watch", "deferred", "done"]
        .map((v) =>
          UI.item(
            actionLabel(v),
            "task-" + v + ":" + id,
            v === "done" ? "check" : v === "watch" ? "book" : "clock",
          ),
        )
        .join(""),
      "Статус действия",
    );
  } else if (type.startsWith("task-")) {
    const status = type.slice(5),
      a = E.assess(state, id);
    if (!["review", "watch", "deferred", "done"].includes(status) || !a) return;
    C.setWorkStatus(state, id, status, state.assessments[id]?.version);
    persist();
    render();
    const destination = {review:"working",watch:"watch",deferred:"deferred",done:"archive"}[status];
    toast("Статус: " + actionLabel(status) + ".", "nav:situations/" + destination, "Открыть подборку");
  } else if (type === "notifications") notificationPopover(anchor);
  else if (type === "notifications-read") {
    state.notifications.forEach((n) => (n.read = true));
    persist();
    render();
    toast("Обновления прочитаны.");
  } else if (type === "notification") {
    const n = state.notifications.find((n) => n.id === id);
    if (n) {
      n.read = true;
      persist();
      navigate(
        n.reportId
          ? "report/" + n.reportId
          : "detail/" + (n.caseId || E.list(state)[0].id),
      );
    }
  } else if (type === "feedback") navigate("feedback/" + id);
  else if (type === "assistant") {
    assistant = !assistant;
    UI.close();
    if (assistant && !assistantContext && view.page === "detail")
      assistantContext = { type: "case", id: view.id };
    render();
    if (assistant) $("#chat-input")?.focus();
  } else if (type === "assistant-close") closeAssistant();
  else if (type === "assistant-expand") {
    expanded = !expanded;
    renderAssistant();
  } else if (["ask", "ask-source", "ask-report", "manager"].includes(type)) {
    assistantContext = {
      type:
        type === "ask-source"
          ? "source"
          : type === "ask-report"
            ? "report"
            : type === "manager"
              ? "manager"
              : "case",
      id,
    };
    assistant = true;
    render();
    if (type === "manager") {
      const a = E.assess(state, id);
      addMessage(
        "Черновик вопроса менеджеру:\n" +
          a.title +
          ". " +
          a.impact +
          "\nКакие условия договора и данные стоит проверить: " +
          a.missing +
          "\nСообщение не отправлено. Его можно скопировать из чата.",
      );
    }
    $("#chat-input")?.focus();
  } else if (type === "suggest") sendChat(action.slice(8));
  else if (type === "import-url") importURL();
  else if (type === "research-repeat") {
    const r = state.research.find((r) => r.id === id);
    if (r)
      runResearch({
        question: r.question,
        start: r.start,
        end: r.end,
        period: r.period,
        previousId: r.id,
      });
  } else if (type === "research-csv") {
    const r = state.research.find((r) => r.id === id);
    if (r)
      download(
        "research-" + r.start + "-" + r.end + ".csv",
        "\ufeffdate;orders;revenue;cost;price_index;market_index\n" +
          r.rows
            .map((r) =>
              [r.date, r.orders, r.revenue, r.cost, r.price, r.market].join(
                ";",
              ),
            )
            .join("\n"),
        "text/csv;charset=utf-8",
      );
  } else if (type === "digest-download") {
    const d = E.digest(state, digestPeriod);
    download(
      "business-digest-" + E.TODAY + ".html",
      `<!doctype html><html lang="ru"><meta charset="utf-8"><title>Сводка бизнеса</title><style>body{font:16px/1.6 system-ui;max-width:850px;margin:40px auto;padding:0 24px}article{padding:20px 0;border-bottom:1px solid #ddd}small{color:#555}</style><h1>Сводка ${digestPeriod === "day" ? "за день" : "за неделю"}</h1><p>${esc(state.company.name)} · ${time(d.at)}</p><p>Новых пересмотров за период: ${d.changes.length}. Материалы и учебные данные различаются в основаниях.</p>${d.items.map((a) => `<article><small>${esc(a.status)} · ${a.metric}</small><h2>${esc(a.title)}</h2><p>${esc(a.impact)}</p><p><b>Проверить:</b> ${esc(a.missing)}</p><p><b>Основания:</b> ${a.sources.map((s) => esc(s.title) + " (" + date(s.publishedAt) + ", " + esc(s.kind) + (s.synthetic ? ", демоданные" : "") + ")").join("; ")}</p></article>`).join("")}<p><small>Прототип. Не подключён к банковским операциям. Решение требует проверки исходных данных.</small></p></html>`,
      "text/html;charset=utf-8",
    );
  } else if (type === "print") window.print();
  else if (type === "export-state") {
    const copy = {
      ...root,
      companies: Object.fromEntries(
        Object.entries(root.companies).map(([id, c]) => {
          const { sources, ...v } = c;
          return [id, v];
        }),
      ),
    };
    download(
      "business-context-state.json",
      JSON.stringify(copy, null, 2),
      "application/json",
    );
  } else if (type === "reset-confirm")
    UI.show(
      anchor,
      `<div class="confirm-popover"><h3>Сбросить данные прототипа?</h3><p>Уточнения, история, задачи и переписка в этом браузере будут удалены. Перед сбросом можно экспортировать состояние.</p><div class="actions">${B("Отмена", "close-pop", "secondary")}${B("Сбросить", "reset", "danger")}</div></div>`,
      "Сброс прототипа",
      "dialog",
    );
  else if (type === "reset") {
    root = E.initial(seed);
    sync();
    persist();
    assistant = false;
    dirty = false;
    lastResult = "";
    navigate("overview");
    toast("Демо возвращено к исходному состоянию.");
  }
}
function deepReport() {
  const r = state.deepAnalysis;
  if (!r)
    return empty(
      "Дополнительных связей пока нет",
      "Запустите обновление после подключения модели.",
      B("Настройки", "nav:settings"),
    );
  return (
    back("overview", "К главному") +
    intro(
      "Дополнительные связи",
      "Гипотезы модели требуют проверки. Они не записаны в подтверждённые факты.",
    ) +
    notice(esc(r.limit), "warning") +
    `<div class="card prose"><h2>Результат анализа</h2>${r.answer
      .split("\n")
      .filter(Boolean)
      .map((x) => `<p style="margin-top:14px">${esc(x)}</p>`)
      .join("")}</div>` +
    r.hypotheses
      .map(
        (h) =>
          `<div class="card prose"><h2>${esc(h.title)}</h2><section class="section" style="margin-top:20px"><h3>Канал влияния</h3><p>${esc(h.mechanism)}</p></section><section class="section"><h3>Альтернативное объяснение</h3><p>${esc(h.alternative)}</p></section><section class="section"><h3>Что опровергнет гипотезу</h3><p>${esc(h.falsifier)}</p></section><section class="section"><h3>Основания</h3>${h.sourceIds
            .map((id) => {
              const s = root.sources.find((x) => x.id === id);
              return s
                ? `<p><a class="link" href="#source/${esc(id)}">${esc(s.title)}</a> · ${date(s.publishedAt)}</p>`
                : "";
            })
            .join("")}</section></div>`,
      )
      .join("") +
    `<div class="card"><h2>Как выполнен анализ</h2><p class="hint" style="margin-top:12px">${esc(r.caseModel)} · ${time(r.createdAt)}. Проверяется структура и наличие ссылок, а не истинность всех утверждений.</p><ol class="trace">${r.trace.map((t) => `<li>${esc({ plan: "План исследования", retrieve: "Подбор оснований", validate: "Проверка ответа" }[t.tool] || t.tool)}: ${esc(t.result?.join(", ") || t.sourceIds?.join(", ") || t.status)}</li>`).join("")}</ol></div>`
  );
}
function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type })),
    a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.hidden = true;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
  toast("Файл подготовлен для скачивания.");
}
async function api(path, body, signal) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  let data;
  try {
    data = await r.json();
  } catch {
    throw Error("Сервер вернул неподдерживаемый ответ.");
  }
  if (!r.ok) throw Error(data.error || "Не удалось выполнить запрос.");
  return data;
}
async function refresh(id = "all", automatic = false) {
  if (!integration.news) {
    if (!automatic) toast("Загрузка новостей сейчас недоступна. Сохранённые демоданные остаются доступны.");
    return;
  }
  if (busy) return;
  const controller = new AbortController(),
    token = Symbol("refresh");
  busy = {
    controller,
    token,
    step: "Загружаем новости и проверяем резервный источник…",
  };
  render();
  try {
    const result = await api("/api/news", null, controller.signal);
    if (busy?.token !== token) return;
    const merged = E.mergeSources(root.sources, result.sources || []);
    applyPublisherBlocks();
    root.lastFeed = {
      checkedAt: result.checkedAt,
      status: result.status,
      feeds: result.feeds,
    };
    busy.step = "Сопоставляем источники и пересматриваем влияние…";
    render();
    const n = updateAll(
      automatic
        ? "Автоматическая проверка источников"
        : "Ручная проверка источников",
    );
    lastResult = `${result.status === "error" ? "Источники временно недоступны. Предыдущие материалы сохранены." : `Новых материалов: ${merged.added}, обновлено: ${merged.updated}.`} ${n ? "Пересмотрено оценок: " + n + "." : "Основания оценок не изменились — версии сохранены."}`;
    if (integration.llm && !automatic) {
      busy.step = "Ищем дополнительные связи и проверяем основания…";
      render();
      try {
        const deep = await api(
          "/api/analyze",
          {
            question:
              "Найди прямые и косвенные риски и возможности для этой компании. Для каждой связи покажи альтернативу и условие опровержения.",
            company: state.company.facts,
            sources: E.contextSources(state).map((s) => ({
              id: s.id,
              title: s.title,
              text: s.text,
              kind: s.kind,
              publishedAt: s.publishedAt,
              bank: !!s.bank,
              synthetic: !!s.synthetic,
            })),
          },
          controller.signal,
        );
        if (busy?.token === token) {
          state.deepAnalysis = deep;
          state.messages.push({
            role: "assistant",
            text: deep.answer,
            action: "nav:deep",
            label: "Связи и проверка оснований",
            mode:
              "Проверенный ответ модели · основания: " +
              deep.citations.join(", "),
          });
          lastResult += " Дополнительный анализ сохранён в помощнике.";
        }
      } catch (e) {
        if (e.name === "AbortError") throw e;
        lastResult +=
          " Глубокий анализ недоступен; расчётные оценки сохранены.";
      }
    }
    persist();
  } catch (e) {
    if (e.name !== "AbortError") {
      lastResult =
        "Не удалось получить новые материалы. Предыдущие оценки сохранены. Можно повторить проверку.";
      root.lastFeed = { checkedAt: new Date().toISOString(), status: "error" };
      persist();
    }
  } finally {
    if (busy?.token === token) {
      busy = null;
      render();
      if (!automatic) toast(lastResult);
    }
  }
}
async function importURL() {
  if (!integration.import) {
    error = "Загрузка источников сейчас недоступна. Вы можете добавить материал, вставив текст вручную.";
    render();
    return;
  }
  const form = $("#source-form");
  if (!form || busy) return;
  Object.assign(draft, Object.fromEntries(new FormData(form)));
  if (!draft.url) {
    error = "Сначала укажите ссылку.";
    render();
    return;
  }
  const controller = new AbortController(),
    token = Symbol("import");
  busy = { controller, token, step: "Получаем текст первоисточника…" };
  render();
  try {
    const result = await api(
      "/api/import",
      { url: draft.url },
      controller.signal,
    );
    if (busy?.token !== token) return;
    Object.assign(draft, {
      ...result.source,
      publishedAt: result.source.publishedAt || "",
      kind: ["Наблюдение", "Прогноз автора", "Мнение"].includes(
        result.source.kind,
      )
        ? result.source.kind
        : "Не классифицирован",
    });
    sourceStage = "review";
    sourceImport = { url: draft.url };
    dirty = true;
    error = "";
    toast(
      "Данные загружены. Проверьте их перед добавлением в анализ.",
    );
  } catch (e) {
    if (e.name !== "AbortError") error = /fetch|network|timeout|aborted/i.test(e.message)
      ? "Не удалось загрузить страницу. Попробуйте ещё раз или вставьте текст вручную."
      : e.message;
  } finally {
    if (busy?.token === token) {
      busy = null;
      render();
    }
  }
}
function runResearch(input) {
  try {
    const r = E.research(state, input);
    if (input.previousId) r.previousId = input.previousId;
    state.research.unshift(r);
    state.research = state.research.slice(0, 30);
    state.notifications.unshift({
      id: "n-" + r.id,
      at: r.createdAt,
      title: "Исследование готово",
      text: date(r.start) + " — " + date(r.end),
      reportId: r.id,
      read: false,
    });
    dirty = false;
    persist();
    navigate("report/" + r.id);
    return r;
  } catch (e) {
    error = e.message;
    render();
    return null;
  }
}
function chatPeriod(text) {
  const iso = text.match(/(20\d{2}-\d{2}-\d{2}).*?(20\d{2}-\d{2}-\d{2})/);
  if (iso) return { start: iso[1], end: iso[2] };
  const m = text.match(
    /(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)/i,
  );
  if (m) {
    const names = [
        "январ",
        "феврал",
        "март",
        "апрел",
        "ма",
        "июн",
        "июл",
        "август",
        "сентябр",
        "октябр",
        "ноябр",
        "декабр",
      ],
      month = names.findIndex((x) => m[3].toLowerCase().startsWith(x)) + 1;
    return {
      start: `2026-${String(month).padStart(2, "0")}-${m[1].padStart(2, "0")}`,
      end: `2026-${String(month).padStart(2, "0")}-${m[2].padStart(2, "0")}`,
    };
  }
  return null;
}
async function sendChat(text) {
  text = String(text || "").trim();
  if (!text || chatBusy) return;
  state.messages.push({ role: "user", text, at: new Date().toISOString() });
  persist();
  chatBusy = true;
  renderAssistant();
  const ctx = assistantContext,
    localState = state;
  try {
    const lower = text.toLowerCase(),
      period = chatPeriod(text),
      id =
        ctx?.type === "case"
          ? ctx.id
          : view.page === "detail"
            ? view.id
            : E.list(state)[0].id,
      a = E.assess(state, id);
    if (
      period &&
      /исслед|анализ|закономер|период|проверь|сравн|заказ|затрат/.test(lower)
    ) {
      const r = E.research(state, {
        ...period,
        question: text,
        period: "custom",
      });
      state.research.unshift(r);
      state.research = state.research.slice(0, 30);
      state.notifications.unshift({
        id: "n-" + r.id,
        at: r.createdAt,
        title: "Исследование готово",
        text: date(r.start) + " — " + date(r.end),
        reportId: r.id,
        read: false,
      });
      addMessage(
        `Исследован период ${date(r.start)} — ${date(r.end)}. Покрытие: ${r.coverage.days} ${plural(r.coverage.days,"день","дня","дней")} и ${r.coverage.events} ${plural(r.coverage.events,"событие","события","событий")}. ${r.delta !== null ? "Изменение заказов: " + num(r.delta, 1) + "%." : "Полной базы сравнения нет."}\nРасчёты показателей синтетические; обнаруженные совпадения не доказывают причину.`,
        "nav:report/" + r.id,
        "Открыть исследование",
        "Расчётный режим",
      );
    } else if (/обнов|пересмотр|проверь.*новост/.test(lower)) {
      await refresh(id);
      addMessage(
        lastResult,
        "nav:history",
        "Посмотреть проверки",
        "Действие выполнено системой",
      );
    } else if (/если|допущ|сценар/.test(lower)) {
      addMessage(
        "Проверим условия отдельным сценарием. Вы увидите изменение вывода до применения к профилю компании.",
        "scenario:" + id,
        "Задать допущение",
        "Профиль пока не менялся",
      );
    } else if (
      /уточн|данн|исправ|у нас|ставк.*фиксир|нет.*долг/.test(lower) &&
      ctx?.type !== "source"
    ) {
      addMessage(
        "Откройте поля, относящиеся к этой оценке. Свободный текст не записывается в подтверждённые факты без проверки.",
        "context:" + id,
        "Уточнить данные",
        "Подтверждение контекста",
      );
    } else if (ctx?.type === "source") {
      const s = root.sources.find((s) => s.id === ctx.id);
      addMessage(
        s
          ? `${s.title}.\n${s.text}\nЭто ${s.kind.toLowerCase()}. ${s.synthetic ? "Материал синтетический." : s.bank ? "Это позиция банка на дату отчёта, прогноз остаётся прогнозом." : "Проверьте первоисточник, автора и фактическую базу."} Сам текст не доказывает изменение показателей компании.`
          : "Материал больше недоступен.",
        "nav:source/" + ctx.id,
        "Материал и ограничения",
        "Разбор по сохранённым основаниям",
      );
    } else if (ctx?.type === "report") {
      const r = state.research.find((r) => r.id === ctx.id);
      addMessage(
        r
          ? `${date(r.start)} — ${date(r.end)}: ${r.coverage.days} ${plural(r.coverage.days,"день","дня","дней")}, ${r.coverage.events} ${plural(r.coverage.events,"событие","события","событий")}. ${r.complete ? "Сравнение с равным предыдущим интервалом." : "Предыдущий период покрыт не полностью."}\n${r.limits}`
          : "Исследование недоступно.",
        "nav:report/" + ctx.id,
        "Открыть результат",
        "Расчётный режим",
      );
    } else if (integration.llm) {
      const result = await api("/api/analyze", {
        question: text,
        company: state.company.facts,
        sources: E.contextSources(state).map((s) => ({
          id: s.id,
          title: s.title,
          text: s.text,
          kind: s.kind,
          publishedAt: s.publishedAt,
          bank: !!s.bank,
          synthetic: !!s.synthetic,
        })),
      });
      if (localState !== state) return;
      state.deepAnalysis = result;
      addMessage(
        result.answer,
        "nav:deep",
        "Связи и основания",
        "Модель · ссылки: " + result.citations.join(", "),
      );
    } else
      addMessage(
        `${a.title}: ${a.impact}\nКанал влияния: ${a.chain.join(" → ")}.\nАльтернатива: ${a.alt}\nЧто проверить: ${a.missing}\n${a.sources.length ? "Основания: " + a.sources.map((s) => s.title + " — " + s.kind.toLowerCase()).join("; ") + "." : "Подходящих оснований нет."}`,
        "nav:detail/" + id + "/evidence",
        "Посмотреть основания",
        "Расчётный режим. Модель не подключена; произвольная глубокая интерпретация недоступна.",
      );
  } catch (e) {
    addMessage(
      e.message || "Не удалось выполнить запрос. Предыдущие оценки сохранены.",
      "",
      "",
      "Запрос не выполнен",
    );
  } finally {
    chatBusy = false;
    persist();
    renderAssistant();
    $("#chat-input")?.focus();
  }
}
document.addEventListener("click", (e) => {
  const link = e.target.closest('a[href^="#"]');
  if (dirty && link && !link.classList.contains("skip")) {
    e.preventDefault();
    perform("nav:" + link.getAttribute("href").slice(1), link);
    return;
  }
  if (e.target.closest(".skip")) {
    e.preventDefault();
    $("#main-content")?.focus();
    window.scrollTo(0, 0);
    return;
  }
  const el = e.target.closest("[data-action]");
  if (!el) return;
  e.preventDefault();
  if (el.closest("#assistant") && el.dataset.action.startsWith("nav:")) {
    assistant = false;
    expanded = false;
  }
  try {
    perform(el.dataset.action, el);
  } catch (err) {
    toast("Не удалось выполнить действие: " + err.message);
  }
});
document.addEventListener("input", (e) => {
  const taskForm = e.target.closest("[data-task]");
  if (taskForm) {
    state.actions[taskForm.dataset.task] = {
      ...state.actions[taskForm.dataset.task],
      ...Object.fromEntries(new FormData(taskForm)),
    };
    persist();
  }
  if (
    e.target.closest("form") &&
    !e.target.closest("#chat-form") &&
    !e.target.closest("#demo-login-form") &&
    !e.target.closest("[data-task]")
  ) {
    draft[e.target.name] = e.target.value;
    dirty = true;
  }
  if (e.target.id === "situation-search") {
    query = e.target.value;
    inventoryPage = 0;
    renderKeepingInput(e.target.id, e.target.selectionStart);
  }
  if (e.target.id === "source-search") {
    sourceQuery = e.target.value;
    sourcePage = 0;
    renderKeepingInput(e.target.id, e.target.selectionStart);
  }
  if (e.target.id === "event-search") {
    eventQuery = e.target.value;
    eventPage = 0;
    renderKeepingInput(e.target.id, e.target.selectionStart);
  }
});
function renderKeepingInput(id, pos) {
  render();
  const field = document.getElementById(id);
  field?.focus({ preventScroll: true });
  field?.setSelectionRange(pos, pos);
}
document.addEventListener("change", (e) => {
  const el = e.target;
  const taskForm = el.closest("[data-task]");
  if (taskForm) {
    state.actions[taskForm.dataset.task] = {
      ...state.actions[taskForm.dataset.task],
      ...Object.fromEntries(new FormData(taskForm)),
    };
    persist();
  }
  if (el.closest("form") && !el.closest("[data-task]") && !el.closest("#demo-login-form")) {
    draft[el.name] = el.value;
    dirty = true;
  }
  if (el.name === "period" && view.page === "research-new") {
    if (el.value === "q3") {
      draft.start = "2026-07-01";
      draft.end = "2026-09-30";
    }
    if (el.value === "oct") {
      draft.start = "2026-10-01";
      draft.end = E.TODAY;
    }
    render();
  }
  if (
    ["start", "end"].includes(el.name) &&
    view.page === "research-new" &&
    draft.period !== "custom"
  ) {
    draft.period = "custom";
    render();
  }
  if (el.name === "caseDomain") {
    inventoryDomain = el.value;
    inventoryPage = 0;
    render();
  }
  if (el.name === "eventDomain") {
    eventDomain = el.value;
    eventPage = 0;
    render();
  }
  if (el.name === "inventorySort") {
    inventorySort = el.value;
    inventoryPage = 0;
    render();
  }
  if (el.name === "eventPeriod") {
    eventPeriod = el.value;
    eventPage = 0;
    render();
  }
  if (el.name === "eventKind") {
    eventKind = el.value;
    eventPage = 0;
    render();
  }
  if (el.name === "role") {
    root.role = el.value;
    persist();
    toast("Приоритет ситуаций обновлён.");
  }
  if (el.id === "auto-check") {
    root.auto = integration.news && el.checked;
    persist();
    toast(
      root.auto
        ? "Автопроверка включена, пока открыт сайт."
        : integration.news
          ? "Автопроверка выключена."
          : "Автопроверка недоступна: загрузка новостей не подключена.",
    );
  }
  if (el.id === "bank-offers") {
    state.bankOffers = el.checked;
    persist();
    toast(
      el.checked ? "Предложения банка включены." : "Предложения банка скрыты.",
    );
  }
  if (el.id === "pet-check") {
    root.pet = el.checked;
    persist();
    render();
  }
});
document.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target,
    data = Object.fromEntries(new FormData(f));
  error = "";
  try {
    if (f.id === "demo-login-form") {
      const email = String(data.email || "").trim(), password = String(data.password || "");
      await startCoreSession(email, password);
      if (coreMode) f.reset();
      dirty = false;
    } else if (f.id === "core-metric-form") {
      if (coreCompany?.role === "viewer") throw Error("У вас есть доступ только для чтения.");
      const companyId = coreCompany.id;
      const created = await coreClient.createMetric(companyId, {
        title: String(data.title || "").trim(), description: String(data.description || "").trim(),
        unit: String(data.unit || "").trim(), direction: data.direction || "neutral",
      });
      if (coreCompany?.id !== companyId) return;
      await loadCoreCompany(companyId, false);
      dirty = false; navigate("metric/" + created.id); toast("Показатель создан.");
    } else if (f.id === "core-observation-form") {
      if (coreCompany?.role === "viewer") throw Error("У вас есть доступ только для чтения.");
      const metricId = f.dataset.metricId;
      const companyId = coreCompany.id;
      if (!E.validDate(data.periodStart) || !E.validDate(data.periodEnd) || data.periodStart > data.periodEnd) throw Error("Проверьте даты периода.");
      const result = await coreClient.addObservation(companyId, metricId, {
        value: String(data.value || ""), unit: String(data.unit || "").trim(),
        periodStart: data.periodStart, periodEnd: data.periodEnd, source: String(data.source || "").trim(),
      });
      if (coreCompany?.id !== companyId) return;
      await loadCoreCompany(companyId, false);
      dirty = false; navigate("metric/" + metricId); toast("Наблюдение сохранено.");
    } else if (f.id === "core-context-form") {
      const companyId = coreCompany.id;
      const facts = {};
      for (const key of ["activities", "products", "markets", "suppliers", "customers", "risks", "notes"]) {
        facts[key] = String(data[key] || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
        if (facts[key].length > 30 || facts[key].some((x) => x.length > 300)) throw Error("В каждом разделе допускается до 30 строк, не более 300 знаков каждая.");
      }
      try {
        await coreClient.saveContext(companyId, Number(coreContext?.version || 0), facts);
        if (coreCompany?.id !== companyId) return;
        await loadCoreCompany(companyId, false);
        dirty = false; draft = {}; navigate("company"); render(); toast("Контекст сохранён.");
      } catch (e) {
        if (coreCompany?.id !== companyId) return;
        error = e.status === 409 ? "Контекст уже изменился в другой сессии. Данные перечитаны; проверьте форму и сохраните ещё раз." : e.message;
        if (e.status === 409 && coreCompany?.id === companyId) await loadCoreCompany(companyId, false);
        dirty = false; render();
      }
    } else if (f.id === "account-form") {
      const firstName = String(data.firstName || "").trim(), lastName = String(data.lastName || "").trim();
      if (!firstName || firstName.length > 80 || lastName.length > 80) throw Error("Укажите имя не длиннее 80 знаков.");
      root.profile = { firstName, lastName };
      draft = { ...root.profile };
      dirty = false;
      persist();
      render();
      toast("Профиль сохранён.");
    } else if (f.id === "context-form") {
      const patch = { ...data };
      if (view.scope === "scenario") {
        E.validateFacts({ ...state.company.facts, ...patch });
        state.scenarios[view.id] = patch;
        persist();
        dirty = false;
        navigate("detail/" + view.id);
        toast("Допущение рассчитано. Профиль компании не изменился.");
      } else {
        const n = E.updateFacts(
          state,
          patch,
          "Уточнение: " +
            (view.id === "finance"
              ? "условия финансирования"
              : view.id === "raw"
                ? "условия закупки"
                : view.id === "packaging"
                  ? "покупатели"
                  : "профиль компании"),
        ).length;
        dirty = false;
        persist();
        navigate(view.id === "all" ? "company" : "detail/" + view.id);
        toast(
          n
            ? "Профиль обновлён. Пересмотрено оценок: " + n + "."
            : "Изменений данных нет.",
        );
      }
    } else if (f.id === "source-form") {
      if (sourceStage === "link") { await importURL(); return; }
      Object.assign(draft, data);
      if (sourceImport && data.url !== sourceImport.url) throw Error("Ссылка изменилась. Загрузите данные по новой ссылке перед добавлением материала.");
      if (!data.title.trim() || !data.publisher.trim() || !data.text.trim())
        throw Error("Укажите название, первоисточник и содержание.");
      if (data.text.length > 30000)
        throw Error("Материал должен быть не длиннее 30 000 знаков.");
      if (!E.validDate(data.publishedAt) || data.publishedAt > E.TODAY)
        throw Error(
          "Проверьте дату публикации. Будущая публикация не считается состоявшейся.",
        );
      if (data.url) {
        const u = new URL(data.url);
        if (u.protocol !== "https:" || u.username || u.password)
          throw Error("Используйте публичную HTTPS-ссылку без учётных данных.");
      }
      const s = {
        ...data,
        title: data.title.trim(),
        publisher: data.publisher.trim(),
        id: "custom-" + Date.now(),
        independence: data.publisher.trim().toLowerCase(),
        kind: E.classify(data.text, data.kind),
        applies: E.topics(data.title + " " + data.text),
        available: true,
        status: "ready",
        synthetic: false,
        custom: true,
        checkedAt: new Date().toISOString(),
      };
      if (
        root.sources.some(
          (x) =>
            (s.url && x.url === s.url) || E.hash(x.text) === E.hash(s.text),
        )
      )
        throw Error(
          "Такой материал уже есть. Найдите его в списке источников.",
        );
      root.sources.push(s);
      applyPublisherBlocks();
      const n = updateAll("Добавлен материал: " + s.title);
      dirty = false;
      navigate("source/" + s.id);
      toast("Источник добавлен. Пересмотрено оценок: " + n + ".");
    } else if (f.id === "research-form") {
      Object.assign(draft, data);
      runResearch(data);
    } else if (f.id === "feedback-form") {
      if (!data.text.trim()) throw Error("Опишите, что стоит исправить.");
      state.feedback.push({
        ...data,
        caseId: view.id,
        at: new Date().toISOString(),
      });
      dirty = false;
      persist();
      navigate(returnTo);
      toast("Отзыв сохранён в этом браузере.");
    } else if (f.dataset.task) {
      state.actions[f.dataset.task] = {
        ...state.actions[f.dataset.task],
        ...data,
        at: new Date().toISOString(),
      };
      persist();
      toast("Детали действия сохранены.");
    } else if (f.id === "chat-form") await sendChat(data.message);
  } catch (err) {
    error = err.message;
    if (f.id !== "chat-form") render();
    else toast(error);
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && assistant && !UI.isOpen()) {
    e.preventDefault();
    closeAssistant();
  }
  if (
    e.target.id === "chat-input" &&
    e.key === "Enter" &&
    !e.shiftKey &&
    !e.isComposing
  ) {
    e.preventDefault();
    e.target.form.requestSubmit();
  }
});
addEventListener("beforeunload", (e) => {
  if (dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
addEventListener("hashchange", () => {
  dirty = false;
  route();
});
addEventListener("storage", (e) => {
  if (e.key === "business-context-v6" && e.newValue && !dirty && !busy) {
    try {
      const data = JSON.parse(e.newValue);
      if (data.schema === 6) {
        root = data;
        sync();
        render();
        toast("Данные обновлены в другом окне.");
      }
    } catch {}
  }
});
setInterval(() => {
  if (integration.news && root.auto && !document.hidden && !busy && !dirty)
    refresh("all", true);
}, 300000);
async function bootstrapCore() {
  if (demoSignedIn) return;
  try {
    const result = await coreClient.me();
    coreUser = result.user;
    coreMode = true;
    try { sessionStorage.removeItem("business-context-demo-session-v2"); } catch {}
    if (!location.hash || location.hash === "#login") location.hash = "#home";
    await loadCoreCompanies();
  } catch (e) {
    if (!(e instanceof CoreApiError) || e.status !== 401) {
      coreError = e.message || "Сервис пока недоступен.";
    }
  }
  render();
}
if (location.hash.startsWith("#panel/context/"))
  location.hash = location.hash.replace("#panel/context/", "#context/");
if (!location.hash) history.replaceState(null, "", demoSignedIn ? "#home" : "#login");
route();
if (demoSignedIn) persist();
void bootstrapCore();

let resizeTimer;
addEventListener("resize", () => {
  if (view.page === "report") {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(render, 120);
  }
});
