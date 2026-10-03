import { mkdir, readFile, copyFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const dist = resolve(root, "dist");
const workerTemplate = await readFile(resolve(root, "site-worker/index.js"), "utf8");
const assets = {
  "__AEGIS_HTML__": await readFile(resolve(root, "web/index.html"), "utf8"),
  "__AEGIS_APP_JS__": await readFile(resolve(root, "web/app.js"), "utf8"),
  "__AEGIS_STYLES_CSS__": await readFile(resolve(root, "web/styles.css"), "utf8"),
};

let worker = workerTemplate;
for (const [placeholder, value] of Object.entries(assets)) {
  worker = worker.replace(placeholder, JSON.stringify(value));
}
await mkdir(resolve(dist, "server"), { recursive: true });
await mkdir(resolve(dist, ".openai"), { recursive: true });
await writeFile(resolve(dist, "server/index.js"), worker, "utf8");
await copyFile(resolve(root, ".openai/hosting.json"), resolve(dist, ".openai/hosting.json"));
console.log(`Built ${resolve(dist, "server/index.js")}`);
