import test from "node:test";
import assert from "node:assert/strict";
import { CoreApiError, createCoreClient } from "../src/core-client.js";

test("Core client uses same-origin cookie session for login and authorized reads", async () => {
  const calls = [];
  const client = createCoreClient(async (path, options) => {
    calls.push({ path, options });
    return { ok: true, status: 200, json: async () => ({ user: { id: "u1" } }) };
  });
  await client.login("demo@example.test", "password");
  await client.me();
  assert.equal(calls[0].path, "/api/auth/login");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.deepEqual(JSON.parse(calls[0].options.body), { email: "demo@example.test", password: "password" });
  assert.equal(calls[1].path, "/api/auth/me");
  assert.equal(calls[1].options.credentials, "same-origin");
});

test("Core client sends context expectedVersion and encodes company IDs", async () => {
  let request;
  const client = createCoreClient(async (path, options) => {
    request = { path, options };
    return { ok: true, status: 201, json: async () => ({ version: 2 }) };
  });
  await client.saveContext("company/a", 1, { activities: ["Производство"] });
  assert.equal(request.path, "/api/companies/company%2Fa/context");
  assert.equal(request.options.method, "PUT");
  assert.deepEqual(JSON.parse(request.options.body), { expectedVersion: 1, facts: { activities: ["Производство"] } });
});

test("Core client preserves conflict status and payload for version CAS", async () => {
  const client = createCoreClient(async () => ({
    ok: false,
    status: 409,
    json: async () => ({ error: "Version conflict", currentVersion: 3 }),
  }));
  await assert.rejects(client.saveContext("c1", 2, {}), (error) => {
    assert.ok(error instanceof CoreApiError);
    assert.equal(error.status, 409);
    assert.deepEqual(error.payload, { error: "Version conflict", currentVersion: 3 });
    return true;
  });
});

test("Core client serializes dashboard range and reads server news endpoints", async () => {
  const calls = [];
  const client = createCoreClient(async (path, options) => {
    calls.push([path, options.credentials]);
    return { ok: true, status: 200, json: async () => ({}) };
  });
  await client.dashboard("c1", { from: "2026-01-01", to: "2026-01-31" });
  await client.newsSources("c1");
  await client.newsDocuments("c1", { from: "2026-01-01" });
  await client.newsRuns("c1");
  await client.newsRun("c1", "run/1");
  assert.equal(calls[0][0], "/api/companies/c1/dashboard?from=2026-01-01&to=2026-01-31");
  assert.equal(calls[1][0], "/api/companies/c1/news/sources");
  assert.equal(calls[2][0], "/api/companies/c1/news/documents?from=2026-01-01");
  assert.equal(calls[3][0], "/api/companies/c1/news/runs");
  assert.equal(calls[4][0], "/api/companies/c1/news/runs/run%2F1");
  assert.ok(calls.every(([, credentials]) => credentials === "same-origin"));
});

test("Core client posts metrics and observations and reads metric event links", async () => {
  const calls = [];
  const client = createCoreClient(async (path, options) => {
    calls.push({ path, options });
    return { ok: true, status: 201, json: async () => ({ id: "x" }) };
  });
  await client.createMetric("c1", { title: "Выручка", description: "За месяц", unit: "₽", direction: "up" });
  await client.addObservation("c1", "m1", { value: "125.50", unit: "₽", periodStart: "2026-01-01", periodEnd: "2026-01-31", source: "Отчёт" });
  await client.metricEvents("c1", "m1");
  await client.newsAssessment("c1", "e1");
  assert.equal(calls[0].path, "/api/companies/c1/metrics");
  assert.deepEqual(JSON.parse(calls[0].options.body), { title: "Выручка", description: "За месяц", unit: "₽", direction: "up" });
  assert.equal(calls[1].path, "/api/companies/c1/metrics/m1/observations");
  assert.equal(JSON.parse(calls[1].options.body).value, "125.50");
  assert.equal(calls[2].path, "/api/companies/c1/metrics/m1/events");
  assert.equal(calls[3].path, "/api/companies/c1/news/events/e1/assessment");
});

test("Core client starts a bounded company news analysis through Core", async () => {
  let request;
  const client = createCoreClient(async (path, options) => {
    request = { path, options };
    return { ok: true, status: 200, json: async () => ({ run: { id: "run-1", status: "completed" } }) };
  });
  const result = await client.analyzeNews("company/one", 1);
  assert.equal(request.path, "/api/companies/company%2Fone/news/runs");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.credentials, "same-origin");
  assert.deepEqual(JSON.parse(request.options.body), { maxEvents: 1 });
  assert.equal(result.run.id, "run-1");
});
