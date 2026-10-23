import test from "node:test";
import assert from "node:assert/strict";
import { seed } from "../web/seed.js";
import * as E from "../web/engine.js";
function state(id) {
  const root = E.initial(seed),
    s = root.companies[id || root.active];
  s.sources = root.sources;
  return s;
}
test("actual case contains distinct company profiles and preserves raw field units", () => {
  assert.equal(seed.companies.length, 3);
  assert.equal(seed.companies[0].raw.SYNTHETIC_U, "U_150736");
  assert.match(seed.dataset.unit, /не определены/);
});
test("industry switch changes core situations, not just labels", () => {
  const s = state();
  assert.ok(E.list(s).some((x) => x.id === "packaging"));
  E.updateFacts(
    s,
    { activity: "Разработка программного обеспечения" },
    "Смена деятельности",
  );
  assert.ok(E.list(s).some((x) => x.id === "software"));
  assert.ok(!E.list(s).some((x) => x.id === "packaging"));
});
test("finance clarification changes finance, leaves procurement facts and versions untouched", () => {
  const s = state(),
    rawVersion = s.assessments.raw.version,
    fixed = s.company.facts.fixed;
  const changes = E.updateFacts(
    s,
    { debt: "yes", rateType: "floating" },
    "Уточнение финансирования",
  );
  assert.equal(s.company.facts.fixed, fixed);
  assert.equal(s.assessments.raw.version, rawVersion);
  assert.deepEqual(
    changes.map((x) => x.caseId),
    ["finance"],
  );
  assert.equal(E.assess(s, "finance").status, "Проверить договор");
});
test("fixed financing and no new borrowing reduce urgency", () => {
  const s = state();
  E.updateFacts(
    s,
    { debt: "yes", rateType: "fixed", plannedBorrowing: "no" },
    "Уточнение",
  );
  assert.equal(E.assess(s, "finance").status, "Влияние отложено");
});
test("private scenario does not mutate facts or published versions", () => {
  const s = state(),
    before = E.stable(s.company),
    v = s.assessments.raw.version;
  assert.equal(E.assess(s, "raw", { fixed: "yes" }).status, "Влияние отложено");
  assert.equal(E.stable(s.company), before);
  assert.equal(s.assessments.raw.version, v);
});
test("manual check without changed evidence preserves version and notifications", () => {
  const s = state(),
    before = E.stable(s.assessments);
  const changes = E.revise(
    s,
    "Ручная проверка",
    undefined,
    "2026-10-22T15:00:00Z",
  );
  assert.equal(changes.length, 0);
  assert.equal(s.history.length, 5);
  assert.equal(s.notifications.length, 0);
  assert.equal(s.assessments.finance.version, 1);
  assert.equal(s.assessments.finance.checkedAt, "2026-10-22T15:00:00Z");
});
test("excluding all financing evidence removes present basis, retains previous snapshot", () => {
  const s = state(),
    before = E.clone(s.history.find((h) => h.caseId === "finance"));
  s.sources.forEach((x) => {
    if (x.applies.includes("finance")) x.excluded = true;
  });
  E.revise(s, "Исключение");
  assert.equal(E.assess(s, "finance").status, "Недостаточно оснований");
  assert.deepEqual(
    s.history.find((h) => h.id === before.id),
    before,
  );
});
test("changed article content creates a new version even if status is unchanged", () => {
  const s = state();
  s.sources.find((x) => x.id === "bank-rate").text += " Дополнение к оценке.";
  const changes = E.revise(s, "Новый текст");
  assert.deepEqual(
    changes.map((x) => x.caseId),
    ["finance"],
  );
  assert.equal(s.assessments.finance.version, 2);
});
test("bank view disappears from the present assessment when excluded", () => {
  const s = state();
  s.sources.find((x) => x.id === "bank-consumption").excluded = true;
  assert.equal(E.assess(s, "packaging").bank.length, 0);
});
test("source groups prevent counting two reports by same analyst as independent", () => {
  const s = state();
  s.sources.push({
    ...s.sources.find((x) => x.id === "bank-rate"),
    id: "copy",
    title: "Перепечатка",
    independence: "alfa-macro",
  });
  const a = E.assess(s, "finance");
  assert.equal(a.sources.length, 2);
  assert.equal(a.groups, 1);
});
test("forecast cannot be promoted to observation by assertive title", () => {
  assert.equal(
    E.classify("Известный автор ожидает падения спроса"),
    "Прогноз автора",
  );
  assert.equal(E.classify("Точный и несомненный рост"), "Не классифицирован");
});
test("URL duplicate update preserves exclusion and source identity", () => {
  const s = state(),
    src = s.sources.find((x) => x.id === "bank-rate");
  src.excluded = true;
  const r = E.mergeSources(s.sources, [
    { ...src, id: "other", text: "Изменённый текст", excluded: false },
  ]);
  assert.equal(r.updated, 1);
  assert.equal(src.id, "bank-rate");
  assert.equal(src.excluded, true);
});
test("research includes every day in an arbitrary partial-month interval", () => {
  const r = E.research(state(), {
    start: "2026-10-01",
    end: "2026-10-15",
    question: "Заказы и затраты",
  });
  assert.equal(r.rows.length, 15);
  assert.equal(r.rows.at(-1).date, "2026-10-15");
  assert.equal(r.baseline.length, 15);
  assert.ok(r.complete);
});
test("incomplete baseline is not zero and is clearly unavailable", () => {
  const r = E.research(state(), { start: "2026-04-01", end: "2026-04-10" });
  assert.equal(r.delta, null);
  assert.equal(r.complete, false);
  assert.deepEqual(r.baseline, []);
});
test("research snapshots survive later source and context changes", () => {
  const s = state(),
    r = E.research(s, { start: "2026-08-01", end: "2026-09-30" }),
    snapshot = E.clone(r);
  s.company.facts.food = "no";
  s.sources[0].text = "changed";
  assert.deepEqual(r, snapshot);
});
test("research rejects reversed, nonexistent, and unsupported date ranges", () => {
  for (const input of [
    { start: "2026-10-16", end: "2026-10-15" },
    { start: "2026-04-31", end: "2026-05-01" },
    { start: "2025-12-01", end: "2026-04-01" },
  ])
    assert.throws(() => E.research(state(), input));
});
test("unsupported freeform research questions are not presented as answered", () => {
  assert.equal(
    E.research(state(), {
      start: "2026-09-01",
      end: "2026-09-30",
      question: "Кто выиграет чемпионат?",
    }).supported,
    false,
  );
});
test("zero variance and short periods do not produce fabricated correlations", () => {
  assert.equal(E.pearson([1, 1, 1], [3, 4, 5]), null);
  assert.equal(
    E.research(state(), { start: "2026-10-01", end: "2026-10-03" }).bestLag,
    null,
  );
});
test("profile change records provenance and validates controlled values", () => {
  const s = state();
  E.updateFacts(s, { food: "yes" }, "Уточнение", "2026-10-22T12:00:00Z");
  assert.equal(s.company.confirmed.food.origin, "Уточнение пользователя");
  assert.equal(s.company.factHistory[0].value, "yes");
  assert.throws(() =>
    E.updateFacts(s, { rateType: "always-low" }, "Bad input"),
  );
});
test("company tasks, messages, and hypotheses are isolated", () => {
  const root = E.initial(seed),
    [a, b] = Object.values(root.companies);
  a.actions.finance = { status: "done" };
  a.messages.push({ text: "private" });
  assert.deepEqual(b.actions, {});
  assert.deepEqual(b.messages, []);
});

test("notification preference suppresses alerts without suppressing revision history", () => {
  const s = state();
  s.notify = { finance: "off" };
  E.updateFacts(s, { rateType: "floating" }, "Уточнение");
  assert.equal(s.assessments.finance.version, 2);
  assert.equal(s.notifications.length, 0);
});

test("no debt prevents a stale floating-rate field from implying current interest exposure", () => {
  const s = state();
  E.updateFacts(
    s,
    { debt: "no", plannedBorrowing: "yes", rateType: "floating" },
    "Уточнение",
  );
  assert.equal(E.assess(s, "finance").status, "Проверить планы");
});
test("software-specific clarification can exclude the retail budget hypothesis", () => {
  const s = state("U_171");
  E.updateFacts(s, { retailClients: "no" }, "Уточнение клиентов");
  assert.equal(E.assess(s, "software").status, "Связь не подтверждена");
  assert.ok(!s.assessments.raw);
});

test("bank evidence survives high-volume news selection for the model", () => {
  const s = state();
  for (let i = 0; i < 60; i++)
    s.sources.push({
      id: "fresh-" + i,
      publishedAt: "2026-10-22",
      available: true,
      status: "ready",
      text: "Новое событие",
    });
  const rows = E.contextSources(s);
  assert.equal(rows.length, 35);
  assert.ok(rows.some((r) => r.id === "bank-rate"));
  assert.ok(rows.some((r) => r.id === "bank-consumption"));
});
