import test from "node:test";
import assert from "node:assert/strict";
import * as C from "../web/catalog.js";
import * as E from "../web/engine.js";
import { seed } from "../web/seed.js";
import { volumeSources } from "../web/volume-demo.js";
import { contextualProduct } from "../web/bank-products.js";

function state() {
  const root = E.initial(seed);
  const s = root.companies[root.active];
  s.sources = root.sources;
  return s;
}
test("150 events remain unique and reachable across all 15 pages", () => {
  const events = E.events({ sources: volumeSources() }),
    ids = [];
  for (let page = 0; page < 15; page++) {
    const m = C.eventCatalog(events, { page });
    assert.equal(m.items.length, 10);
    ids.push(...m.items.map((e) => e.id));
  }
  assert.equal(new Set(ids).size, 150);
  assert.deepEqual(new Set(ids), new Set(events.map((e) => e.id)));
});
test("narrowing a late page clamps to a valid filtered result", () => {
  const m = C.eventCatalog(E.events({ sources: volumeSources() }), {
    domain: "finance",
    page: 14,
  });
  assert.equal(m.total, 30);
  assert.equal(m.pages, 3);
  assert.equal(m.page, 2);
  assert.ok(m.items.every((e) => e.applies.includes("finance")));
});
test("event facets, author search and forecast type work together without duplicates", () => {
  const events = E.events({ sources: volumeSources() });
  const m = C.eventCatalog(events, { kind: "forecast", query: "издание 1" });
  assert.ok(m.total > 0);
  assert.ok(
    m.items.every(
      (e) =>
        e.kind === "Прогноз автора" && e.sources[0].publisher.endsWith("1"),
    ),
  );
  const multi = { ...events[0], applies: ["finance", "raw"] };
  assert.equal(C.eventCatalog([multi, multi]).total, 1);
  assert.equal(C.eventCatalog([multi]).facets.finance, 1);
  assert.equal(C.eventCatalog([multi]).facets.procurement, 1);
});
test("reading is version-sensitive and does not transfer between companies", () => {
  const event = E.events({ sources: volumeSources(1) })[0],
    seen = { [event.id]: C.eventSignature(event) };
  assert.equal(C.eventCatalog([event], { onlyNew: true, seen }).total, 0);
  assert.equal(C.eventCatalog([event], { onlyNew: true, seen: {} }).total, 1);
  event.sources[0].text += " Исправление.";
  assert.equal(C.eventCatalog([event], { onlyNew: true, seen }).total, 1);
});
test("150 cases are partitioned by work state and not dumped into one page", () => {
  const s = state(),
    base = E.list(s)[0];
  const cases = Array.from({ length: 150 }, (_, i) => ({
    ...base,
    id: "case-" + i,
    score: 100 - i / 10,
    title: "Проверка " + i,
  }));
  s.actions = {
    "case-0": { status: "review" },
    "case-1": { status: "done" },
    "case-2": { status: "deferred" },
  };
  s.hidden = ["case-3"];
  s.watch = ["case-4"];
  const m = C.caseCatalog(cases, s, { view: "attention", page: 100 });
  assert.equal(m.counts.all, 150);
  assert.equal(m.counts.attention, 145);
  assert.equal(m.counts.working, 1);
  assert.equal(m.counts.watch, 1);
  assert.equal(m.counts.deferred, 1);
  assert.equal(m.counts.archive, 2);
  assert.equal(m.items.length, 5);
  assert.equal(m.page, 14);
  const reopened = C.caseCatalog(cases, s, {
    view: "all",
    query: "Проверка 149",
    page: 14,
  });
  assert.equal(reopened.items[0].id, "case-149");
  assert.equal(reopened.page, 0);
});
test("work state takes precedence over watch marker and archived cases stay recoverable", () => {
  const s = state(),
    item = E.list(s)[0];
  s.watch = [item.id];
  s.actions = { [item.id]: { status: "review" } };
  assert.equal(C.bucket(item, s), "working");
  s.hidden = [item.id];
  assert.equal(C.bucket(item, s), "archive");
  assert.equal(C.caseCatalog([item], s, { view: "archive" }).total, 1);
  assert.equal(C.caseCatalog([item], s, { view: "attention" }).total, 0);
});
test("taking an active check under observation moves its count and preserves task details", () => {
  const s = state(), item = E.list(s)[0];
  s.actions = {[item.id]: {status:"review", owner:"Сергей", note:"Уточнить договор", due:"2026-10-25"}};
  C.setWorkStatus(s, item.id, "watch", 2);
  assert.equal(C.bucket(item, s), "watch");
  assert.equal(C.caseCatalog([item], s).counts.working, 0);
  assert.equal(C.caseCatalog([item], s).counts.watch, 1);
  assert.equal(s.actions[item.id].owner, "Сергей");
  assert.equal(s.actions[item.id].note, "Уточнить договор");
  C.setWorkStatus(s, item.id, "review", 2);
  assert.equal(C.bucket(item, s), "working");
  assert.equal(s.watch.includes(item.id), false);
  C.setWorkStatus(s, item.id, "deferred", 3);
  assert.equal(C.bucket(item, s), "deferred");
  C.setWorkStatus(s, item.id, "done", 3);
  assert.equal(C.bucket(item, s), "archive");
});
test("low urgency does not put an unchosen case into the user's observations", () => {
  const s = state(), item = {...E.list(s)[0], score: 5};
  assert.equal(C.bucket(item, s), "attention");
  C.setWorkStatus(s, item.id, "watch", 1);
  C.setWorkStatus(s, item.id, "todo", 1);
  assert.equal(C.bucket(item, s), "attention");
  assert.equal(C.caseCatalog([item], s).counts.watch, 0);
});
test("commercial finance placement requires explicit borrowing intent and does not alter assessment", () => {
  const s = state(),
    a = E.assess(s, "finance"),
    before = E.stable(a);
  assert.equal(contextualProduct(a, s), null);
  s.company.facts.plannedBorrowing = "yes";
  assert.equal(contextualProduct(a, s).id, "business-credit");
  assert.equal(E.stable(a), before);
  s.bankOffers = false;
  assert.equal(contextualProduct(a, s), null);
});
test("commercial placement is suppressed after completion or dismissal and can expire", () => {
  const s = state(),
    a = E.assess(s, "opportunity"),
    now = Date.parse("2026-10-22");
  assert.equal(contextualProduct(a, s, now).id, "counterparty-check");
  s.dismissedOffers = { "counterparty-check": now };
  assert.equal(contextualProduct(a, s, now + 86400000), null);
  assert.ok(contextualProduct(a, s, now + 31 * 86400000));
  s.actions = { [a.id]: { status: "done" } };
  assert.equal(contextualProduct(a, s, now + 31 * 86400000), null);
  s.actions = {};
  s.company.facts.freeCapacity = "no";
  assert.equal(contextualProduct(a, s, now + 31 * 86400000), null);
});
test("volume preview does not mutate company evidence, research or versions", () => {
  const s = state(),
    before = E.stable(s);
  C.eventCatalog(E.events({ ...s, sources: volumeSources() }), { page: 14 });
  assert.equal(E.stable(s), before);
});
