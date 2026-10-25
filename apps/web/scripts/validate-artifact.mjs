import assert from "node:assert/strict";
import { access, readFile, readdir } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const dist = resolve(root, "dist");
const html = await readFile(resolve(dist, "index.html"), "utf8");

for (const asset of ["app.js", "core-ui.css", "product.css", "workspace.css", "compact.css"]) {
  await access(resolve(dist, asset));
}
assert.match(html, /src="\/app\.js"/, "index.html loads the compiled app");
assert.match(html, /href="\/core-ui\.css"/, "index.html loads compiled UI styles");
assert.match(html, /href="\/compact\.css"/, "index.html loads compact layout styles");
assert.doesNotMatch(html, /\/src\/|\.jsx/, "index.html does not expose source modules");

const app = await readFile(resolve(dist, "app.js"), "utf8");
const style = await readFile(resolve(dist, "core-ui.css"), "utf8");
const assetPaths = new Set([
  ...[...app.matchAll(/(?:src=|\()?["'](\/assets\/[^"')]+)["']/g)].map((m) => m[1]),
  ...[...style.matchAll(/url\(["']?(\/assets\/[^"')]+)["']?\)/g)].map((m) => m[1]),
]);
for (const path of assetPaths) {
  if (!path.includes("${")) await access(resolve(dist, path.slice(1)));
}

const files = await readdir(dist);
assert.ok(files.includes("index.html"), "static artifact has its HTML entry point");
assert.ok(!files.includes("server"), "static artifact contains no Node server bundle");
assert.doesNotMatch(app, /LLM_API_KEY|OPENROUTER_API_KEY/, "browser bundle contains no provider secrets");
console.log(`Static frontend is complete (${assetPaths.size} referenced assets checked).`);
