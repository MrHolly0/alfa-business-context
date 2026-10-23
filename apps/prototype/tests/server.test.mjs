import test from "node:test";
import assert from "node:assert/strict";
import worker, { safeURL, parseRSS, analyze, importArticle } from "../worker/index.js";
test("URL loader blocks credentials, private hosts, ports, lookalikes and non-HTTPS", () => {
  for (const u of [
    "http://www.cbr.ru/news",
    "https://127.0.0.1",
    "https://localhost",
    "https://www.cbr.ru.evil.test",
    "https://user:pass@www.cbr.ru",
    "https://www.cbr.ru:444",
    "https://169.254.169.254",
  ])
    assert.throws(() => safeURL(u));
  assert.equal(safeURL("https://www.cbr.ru/news/").hostname, "www.cbr.ru");
});
test("RSS parser extracts provenance and keeps forecast uncertain", () => {
  const s = parseRSS(
    "<rss><channel><item><title><![CDATA[ЦБ ожидает замедления]]></title><link>https://www.cbr.ru/press/event/?id=123</link><description>&lt;p&gt;Автор предполагает изменение спроса.&lt;/p&gt;</description><pubDate>Wed, 21 Oct 2026 12:00:00 +0300</pubDate></item></channel></rss>",
    { id: "test" },
  );
  assert.equal(s.length, 1);
  assert.equal(s[0].kind, "Прогноз автора");
  assert.equal(s[0].independence, "bank-of-russia");
  assert.equal(s[0].publishedAt, "2026-10-21");
  assert.match(s[0].text, /Автор/);
  assert.ok(!s[0].text.includes("<p>"));
});
test("RSS items without reliable dates or public source URLs are excluded", () => {
  const s = parseRSS(
    "<item><title>Missing date</title><link>https://www.cbr.ru/x</link></item><item><title>Private</title><link>https://localhost</link><pubDate>Wed, 21 Oct 2026 12:00:00 GMT</pubDate></item>",
    { id: "test" },
  );
  assert.equal(s.length, 0);
});
test("absent or disallowed LLM configuration cannot masquerade as a model result", async () => {
  await assert.rejects(() => analyze({}, {}), /не настроена/);
  const r = await worker.fetch(new Request("https://site.test/api/status"), {
    LLM_BASE_URL: "https://x",
    LLM_API_KEY: "hidden",
    LLM_MODEL: "x",
    LLM_CASE_MODEL: "External agent",
  });
  assert.equal((await r.json()).llm, false);
});
test("cross-origin mutations and excessive request payloads are rejected", async () => {
  let r = await worker.fetch(
    new Request("https://site.test/api/import", {
      method: "POST",
      headers: {
        Origin: "https://evil.test",
        "Content-Type": "application/json",
      },
      body: "{}",
    }),
  );
  assert.equal(r.status, 403);
  r = await worker.fetch(
    new Request("https://site.test/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: "https://www.cbr.ru",
        x: "a".repeat(180001),
      }),
    }),
  );
  assert.equal(r.status, 400);
});
test("bounded agent plans, retrieves, validates and repairs unknown citations", async () => {
  const old = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    const content =
      calls === 1
        ? JSON.stringify({
            themes: ["ставка"],
            question: "проверка",
            needBank: true,
          })
        : JSON.stringify({
            answer:
              "Проверяемая гипотеза о влиянии рыночной ставки на условия компании. Прогноз требует подтверждения.",
            citations: [calls === 2 ? "invented" : "b"],
            hypotheses: [
              {
                title: "Ставка",
                mechanism: "Условия займа",
                alternative: "Фиксированная ставка",
                falsifier: "Нет заимствований",
                sourceIds: ["b"],
              },
            ],
          });
    return new Response(
      JSON.stringify({ choices: [{ message: { content } }] }),
      { headers: { "Content-Type": "application/json" } },
    );
  };
  try {
    const r = await analyze(
      {
        LLM_BASE_URL: "https://gateway.test/v1",
        LLM_API_KEY: "secret",
        LLM_MODEL: "test",
        LLM_CASE_MODEL: "Qwen 3.6 35B A3B",
      },
      {
        question: "Что проверить?",
        company: { activity: "Производство" },
        sources: [
          {
            id: "b",
            title: "Ставка",
            text: "Ожидается сохранение паузы.",
            kind: "Прогноз банка",
            bank: true,
          },
        ],
      },
    );
    assert.equal(calls, 3);
    assert.equal(r.trace.filter((t) => t.status === "rejected").length, 1);
    assert.deepEqual(r.citations, ["b"]);
  } finally {
    globalThis.fetch = old;
  }
});


test("link import fills published fields and leaves missing metadata empty", async () => {
  const previousFetch = globalThis.fetch;
  const body = "<article><p>Автор ожидает изменение спроса в следующем квартале. Это предварительный прогноз, который требует сопоставления с реальными показателями компании.</p><p>Снижение заказов пока не подтверждено.</p></article>";
  try {
    globalThis.fetch = async () => new Response('<html><title>Прогноз спроса</title><meta name="author" content="Иван Иванов"><meta property="article:published_time" content="2026-10-20T09:00:00Z">' + body + '</html>', {headers:{'Content-Type':'text/html'}});
    const complete = await importArticle("https://www.cbr.ru/test-fixture/");
    assert.equal(complete.title, "Прогноз спроса");
    assert.equal(complete.author, "Иван Иванов");
    assert.equal(complete.publishedAt, "2026-10-20");
    assert.equal(complete.kind, "Прогноз автора");
    assert.ok(complete.text.includes("предварительный прогноз"));
    assert.ok(!complete.text.includes("<article>"));
    globalThis.fetch = async () => new Response('<html><title>Без даты</title>' + body + '</html>', {headers:{'Content-Type':'text/html'}});
    const incomplete = await importArticle("https://www.cbr.ru/test-fixture/");
    assert.equal(incomplete.author, "");
    assert.equal(incomplete.publishedAt, "");
    assert.equal(incomplete.publisher, "www.cbr.ru");
  } finally {
    globalThis.fetch = previousFetch;
  }
});
