// Screenshots of the reskin review views through headless Edge/Chrome over the DevTools protocol
// (Node 22+: built-in fetch + WebSocket, no npm packages).
// Usage: node scripts/shoot_views.mjs <label> [url]   -> data/screens/<label>/<view>.png
// Needs the UI running (uv run osint run, http://127.0.0.1:8765/). BROWSER overrides the binary.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { join, resolve } from "node:path";

const label = process.argv[2] || "shots";
const url = process.argv[3] || "http://127.0.0.1:8765/";
const CANDIDATES = [process.env.BROWSER, "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe", "/usr/bin/google-chrome", "/usr/bin/chromium"].filter(Boolean);
const bin = CANDIDATES.find((p) => existsSync(p));
if (!bin) { console.error("no Edge/Chrome found; set BROWSER"); process.exit(2); }
const out = join("data", "screens", label);
mkdirSync(out, { recursive: true });
const port = 9333;
// fresh profile per run: no localStorage (layer state, CRT toggle) leaks between before / after
const profile = resolve("data", "screens", ".profile-" + label);
rmSync(profile, { recursive: true, force: true });
const proc = spawn(bin, ["--headless=new", "--remote-debugging-port=" + port, "--user-data-dir=" + profile,
  "--window-size=1600,900", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
  "--no-first-run", "--no-default-browser-check", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function target() {
  for (let i = 0; i < 150; i++) {
    try {
      const list = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* browser still starting */ }
    await sleep(200);
  }
  throw new Error("DevTools endpoint did not come up");
}

const ws = new WebSocket(await target());
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let seq = 0;
const pending = new Map();
const errors = [];
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
  if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.text + " " + ((d.params.exceptionDetails.exception || {}).description || ""));
  if (d.method === "Runtime.consoleAPICalled" && d.params.type === "error") errors.push(d.params.args.map((a) => a.value ?? a.description).join(" "));
};
const send = (method, params = {}) => new Promise((r) => { const id = ++seq; pending.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
const run = async (expr) => { const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true }); if (r.result && r.result.exceptionDetails) { const x = r.result.exceptionDetails; throw new Error(expr + ": " + x.text + " " + ((x.exception && (x.exception.description || x.exception.value)) || "")); } };
const shot = async (name) => {
  const r = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(out, name + ".png"), Buffer.from(r.result.data, "base64"));
  console.log("wrote " + join(out, name + ".png"));
};

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url });
await sleep(15000); // globe tiles + first polls

const VIEWS = [
  ["01-globe", ""],
  ["02-layer-tree", "document.querySelector('#rail .rail-btn[data-g=\"events\"]').click()"],
  ["03-inspector-event", "(() => { const o = window.__osint, it = [...o.byKind.events.items.values()][0]; if (!it) throw new Error('no events loaded'); o.select('events', it.id); })()"],
  ["04-events-tab", "document.querySelector('.ptab[data-p=\"events\"]').click()"],
  ["05-terror-tab", "document.querySelector('.ptab[data-p=\"terror\"]').click()"],
  ["06-dossier-intel", "document.dispatchEvent(new KeyboardEvent('keydown', { key: 'i' }))"],
  ["07-timeline-replay", "(() => { window.__osint.dossier.minimise(); const s = document.getElementById('tl-slider'); s.value = String(Math.round(+s.max / 2)); s.dispatchEvent(new Event('input')); })()"],
  ["08-settings", "(() => { window.__osint.timeline.goLive(); document.getElementById('cfg-btn').click(); })()"],
];
for (const [name, js] of VIEWS) {
  try { if (js) await run(js); } catch (e) { console.warn("step " + name + " failed: " + e.message); }
  await sleep(name === "06-dossier-intel" ? 6000 : 2500);
  await shot(name);
}
writeFileSync(join(out, "console-errors.txt"), errors.join("\n"));
console.log(errors.length + " console errors (see " + join(out, "console-errors.txt") + ")");
ws.close();
proc.kill();
process.exit(0);
