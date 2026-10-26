import { build } from "esbuild";
import { cp, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
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

// Keep stable aliases for local tooling, but give the browser a new URL for
// every script and stylesheet referenced by the HTML after each content change.
const htmlPath = resolve(dist, "index.html");
let html = await readFile(htmlPath, "utf8");
const fingerprint = async (name, extension) => {
  const sourcePath = resolve(dist, `${name}.${extension}`);
  const contents = await readFile(sourcePath);
  const digest = createHash("sha256").update(contents).digest("hex").slice(0, 12);
  const versionedName = `${name}-${digest}.${extension}`;
  await writeFile(resolve(dist, versionedName), contents);
  html = html.replaceAll(`/${name}.${extension}`, `/${versionedName}`);
};
await fingerprint("app", "js");
const stylesheets = [...html.matchAll(/href="\/([^"/]+\.css)"/g)].map((match) => match[1]);
for (const stylesheet of stylesheets) {
  await fingerprint(stylesheet.slice(0, -4), "css");
}
await writeFile(htmlPath, html);

console.log("Built static frontend in apps/web/dist.");
