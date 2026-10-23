import { build } from "esbuild";
import { cp, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { extname, relative, resolve } from "node:path";

const root = fileURLToPath(new URL("..", import.meta.url));
const source = resolve(root, "src");
const publicDir = resolve(root, "public");
const dist = resolve(root, "dist");

await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
await cp(publicDir, dist, { recursive: true });

const bundle = await build({
  entryPoints: [resolve(source, "app.js")],
  bundle: true,
  write: false,
  format: "esm",
  target: ["es2022"],
  minify: true,
  legalComments: "eof",
  outdir: "out",
  assetNames: "assets/[name]-[hash]",
  define: { "process.env.NODE_ENV": '"production"' },
});

for (const file of bundle.outputFiles) {
  const name = file.path.split(/[\\/]/).at(-1);
  const target = name === "app.css" ? "core-ui.css" : name;
  const outputPath = resolve(dist, target === name ? relative(resolve(root, "out"), file.path) : target);
  await mkdir(resolve(outputPath, ".."), { recursive: true });
  await writeFile(outputPath, file.contents);
}

console.log("Built static frontend in apps/web/dist.");
