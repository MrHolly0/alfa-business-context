import test from "node:test";
import assert from "node:assert/strict";
import { createPulse } from "../src/pulse.js";

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

test("Core overview renders saved source roles, candidate evidence, and dashboard summary", () => {
  const pulse = createPulse({
    E: {}, C: {}, esc: escapeHtml, I: () => "", B: () => "",
    state: () => ({}), root: {}, persist() {}, navigate() {}, render() {},
  });
  const company = { synthetic_u: "company-1", mainokved_name: "Торговля", region: "Москва", role: "admin" };
  const context = { version: 2, source: "organizer_csv", facts: {
    ai_profile: { name: "Организация", description: "Профиль из принятого контекста", activities: [{ label: "Оптовая торговля" }] },
  } };
  const run = {
    id: "run-1", status: "partial", context_version: 2, policy_version: 4,
    result: { provider: { profile: "hackathon" }, candidates: [{
      execution_mode: "model", status: "partial", summary: "Сохранённая оценка",
      evidence: [{ evidence_id: "e1", source_role: "external", canonical_uri: "https://news.example/item", title: "Источник", published_at: "2026-10-25" }],
      claims: [{ kind: "external_fact", text: "В материале описано изменение", evidence_refs: ["e1"] }],
    }] },
  };
  const profile = pulse.coreProfileSnapshot(company, context);
  const analysis = pulse.coreAnalysisPanel(run, false, true, "core-analyze", 2, 4);
  const dashboard = pulse.coreDashboard(company, {
    summary: { status: "partial", model_status: "model_done", text: "Сохранённая оценка" },
    metrics: [], events: [],
  });
  const overview = profile + analysis + dashboard;

  assert.match(overview, /Оптовая торговля/);
  assert.match(overview, /Модельная оценка сохранена/);
  assert.match(overview, /Внешний источник/);
  assert.match(overview, /https:\/\/news\.example\/item/);
  assert.match(overview, /Сохранённая оценка/);
});

test("Core overview keeps a long clarification list collapsed and prioritizes confirmed profile assertions", () => {
  const pulse = createPulse({
    E: {}, C: {}, esc: escapeHtml, I: () => "", B: () => "",
    state: () => ({}), root: {}, persist() {}, navigate() {}, render() {},
  });
  const assertions = [
    ...Array.from({ length: 3 }, (_, index) => ({ kind: "product", label: `Unknown product ${index + 1}`, status: "unknown" })),
    { kind: "product", label: "Confirmed product", status: "confirmed" },
    { kind: "product", label: "Active product", status: "active" },
  ];
  const profile = pulse.coreProfileSnapshot({}, { version: 1, facts: { ai_profile: { assertions } } });
  const products = profile.match(/<dt>Продукты и услуги<\/dt><dd>(.*?)<\/dd>/)?.[1] || "";
  assert.match(products, /Confirmed product; Active product/);

  const clarifications = Array.from({ length: 30 }, (_, index) => `Question ${index + 1}`);
  const analysis = pulse.coreAnalysisPanel({
    status: "partial",
    result: { candidates: [{ clarifications }] },
  });
  assert.match(analysis, /Question 1/);
  assert.match(analysis, /Question 3/);
  assert.match(analysis, /Ещё 27 уточнений/);
  assert.doesNotMatch(analysis.slice(0, analysis.indexOf("Ещё 27 уточнений")), /Question 4/);
  assert.match(analysis, /Question 30/);
});
