import assert from "node:assert/strict";
import test from "node:test";
import { backendCapabilities } from "../src/capabilities.js";

test("backend capabilities default to unavailable until explicitly enabled", () => {
  assert.deepEqual(backendCapabilities({ llm: true }), {
    news: false,
    import: false,
    analyze: false,
    llm: false,
  });
});

test("LLM use requires both a configured LLM and analyze capability", () => {
  assert.deepEqual(
    backendCapabilities({
      llm: true,
      capabilities: { news: true, import: true, analyze: false },
    }),
    { news: true, import: true, analyze: false, llm: false },
  );
  assert.equal(
    backendCapabilities({ llm: true, capabilities: { analyze: true } }).llm,
    true,
  );
});
