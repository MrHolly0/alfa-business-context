import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
const worker = await import(
  "data:text/javascript;base64," +
    Buffer.from(
      await readFile(
        new URL("../dist/server/index.js", import.meta.url),
        "utf8",
      ),
    ).toString("base64")
);
const server = createServer(async (req, res) => {
  try {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const url = "http://" + (req.headers.host || "localhost") + req.url;
    const request = new Request(url, {
      method: req.method,
      headers: req.headers,
      ...(!["GET", "HEAD"].includes(req.method)
        ? { body: Buffer.concat(chunks) }
        : {}),
    });
    const response = await worker.default.fetch(request, process.env, {});
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch {
    res.writeHead(500);
    res.end("Preview request failed");
  }
});
server.listen(Number(process.env.PORT || 4173), "0.0.0.0", () =>
  console.log("Business Context preview ready"),
);
