import { build } from "esbuild";
import { readFile, writeFile, mkdir, rm, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve, relative, extname } from "node:path";
const root = fileURLToPath(new URL("..", import.meta.url)),
  web = resolve(root, "web");
const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".webp": "image/webp",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
};
const bundle = await build({
  entryPoints: [resolve(web, "app.js")],
  bundle: true,
  write: false,
  format: "esm",
  target: ["es2022"],
  minify: true,
  legalComments: "eof",
  outdir: "out",
  define: { "process.env.NODE_ENV": '"production"' },
});
const assets = Object.fromEntries(
  bundle.outputFiles.map((file) => [
    file.path.endsWith(".css") ? "/core-ui.css" : "/app.js",
    {
      type: file.path.endsWith(".css")
        ? "text/css; charset=utf-8"
        : "application/javascript; charset=utf-8",
      data: Buffer.from(file.contents).toString("base64"),
    },
  ]),
);
async function walk(dir) {
  for (const item of await readdir(dir, { withFileTypes: true })) {
    const p = resolve(dir, item.name);
    if (item.name.startsWith("situation-") && item.name.endsWith(".png"))
      continue;
    if (item.name === "packaging-3d.png") continue;
    if (
      ["app.js", "core-ui.jsx", "catalog.js", "volume-demo.js"].includes(
        item.name,
      )
    )
      continue;
    if (
      [
        "02_question_right.png",
        "04_speaking_right.png",
        "05_heart_right.png",
        "06_okay_right.png",
      ].includes(item.name)
    )
      continue;
    if (item.isDirectory()) await walk(p);
    else
      assets["/" + relative(web, p).replaceAll("\\", "/")] = {
        type: types[extname(p)] || "application/octet-stream",
        data: (await readFile(p)).toString("base64"),
      };
  }
}
await walk(web);
const engine = (await readFile(resolve(web, "engine.js"), "utf8")).replace(
  /\bexport /g,
  "",
);
let source = await readFile(resolve(root, "worker/index.js"), "utf8");
source = source.replace(
  /import \* as E from ["']\.\.\/web\/engine\.js["'];?/,
  `const E=(()=>{${engine}\nreturn {topics,classify,hash};})();`,
);
source = "const ASSETS=" + JSON.stringify(assets) + ";\n" + source;
await rm(resolve(root, "dist"), { recursive: true, force: true });
await mkdir(resolve(root, "dist/server"), { recursive: true });
await writeFile(resolve(root, "dist/server/index.js"), source);
console.log(
  "Built Worker and " + Object.keys(assets).length + " embedded assets.",
);
