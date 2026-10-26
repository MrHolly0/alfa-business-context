import test from "node:test";
import assert from "node:assert/strict";
import { resolveCoreNewsView } from "../src/core-news-view.js";

test("Core news link without an event id opens the saved news list", () => {
  assert.deepEqual(resolveCoreNewsView(undefined, [{ id: "event-1" }]), { kind: "list" });
  assert.deepEqual(resolveCoreNewsView("event-1", [{ id: "event-1", title: "Saved event" }]), {
    kind: "detail",
    event: { id: "event-1", title: "Saved event" },
  });
  assert.deepEqual(resolveCoreNewsView("missing", []), { kind: "missing" });
});
