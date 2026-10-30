/**
 * Render check: sign in to a running LOCAL Shorthop, open every page in headless Chrome at
 * desktop and phone width in both themes, MEASURE what is wrong, and save screenshots.
 *
 *   SHORTHOP_URL=http://localhost:8099 OUT=/tmp/shots node scripts/render.mjs
 *
 * Needs a server seeded by scripts/demo_seed.py. Checks per page: horizontal overflow of the
 * page, an element sticking out of its card, text reading "undefined" / "NaN" / "Invalid
 * Date", and that the page actually rendered content (a blank page must fail, not pass).
 */
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.env.SHORTHOP_URL || "http://localhost:8099";
const OUT = process.env.OUT || join(tmpdir(), "shorthop-shots");
const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
mkdirSync(OUT, { recursive: true });

// --- sign in once, with the break-glass form, to get a session cookie -------------------
const login = await fetch(`${BASE}/login`, {
  method: "POST", redirect: "manual",
  headers: { "Content-Type": "application/x-www-form-urlencoded" },
  body: "email=admin%40example.com&password=demo-password-123&next=%2F",
});
const cookie = (login.headers.get("set-cookie") || "").split(";")[0];
if (!cookie.startsWith("shorthop_session=")) throw new Error(`login failed: ${login.status}`);
const [cname, cvalue] = [cookie.slice(0, cookie.indexOf("=")), cookie.slice(cookie.indexOf("=") + 1)];

const alerts = await (await fetch(`${BASE}/api/v1/alerts`, { headers: { cookie } })).json();
const incs = await (await fetch(`${BASE}/api/v1/incidents`, { headers: { cookie } })).json();
const scheds = await (await fetch(`${BASE}/api/v1/schedules`, { headers: { cookie } })).json();
const crash = alerts.find((a) => a.alertname === "KubePodCrashLooping");
const resolvedOne = (await (await fetch(`${BASE}/api/v1/alerts?status=resolved`, { headers: { cookie } })).json())[0];
const PAGES = [
  ["incidents", "/incidents"],
  ["incident-detail", `/incidents/${incs[0].id}`],
  // Behind clicks: "Move to another team", with the Databases team picked (its one policy prefills).
  ["incident-reassign", `/incidents/${incs[0].id}`, `(() => { [...document.querySelectorAll("button")].find((b) => b.textContent.includes("Move to another team"))?.click(); setTimeout(() => { const sel = document.querySelector(".reassign-form select"); const opt = [...sel.options].find((o) => o.textContent === "Databases"); const set = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set; set.call(sel, opt.value); sel.dispatchEvent(new Event("change", { bubbles: true })); }, 400); })()`],
  // Behind clicks: the level row, and "Add responder" + "Snooze" opened.
  ["incident-more", `/incidents/${incs[0].id}`, `setTimeout(() => { const b = (t) => [...document.querySelectorAll("button")].find((x) => x.textContent.trim() === t); b("Add responder")?.click(); b("Snooze")?.click(); }, 400)`],
  ["schedules", "/schedules"],
  ["schedule-detail", `/schedules/${scheds[0].id}`],
  // Behind a click: a stretch picked on the month view opens the override panel pre-filled.
  ["schedule-pick", `/schedules/${scheds[0].id}`, `setTimeout(() => [...document.querySelectorAll("button.mcal-bar")].find((b) => b.textContent.includes("Ada"))?.click(), 400)`],
  // Behind clicks: the Layers view stepped one window ahead, then a shift picked on it.
  ["schedule-layers", `/schedules/${scheds[0].id}`, `(async () => { const w = (ms) => new Promise((r) => setTimeout(r, ms)); [...document.querySelectorAll("button.seg-btn")].find((b) => b.textContent === "Layers")?.click(); await w(400); document.querySelector("button[aria-label^='Next 14']")?.click(); await w(1200); document.querySelector(".tl-final button.tl-seg")?.click(); })()`],
  ["schedule-new-override", `/schedules/${scheds[0].id}`, `setTimeout(() => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "New override")?.click(), 400)`],
  // The FIRST bar of the paged-forward window: a shift that began before the window. The
  // panel must offer the whole shift (shift_start), not the part on screen.
  ["schedule-layers-cut", `/schedules/${scheds[0].id}`, `(async () => { const w = (ms) => new Promise((r) => setTimeout(r, ms)); [...document.querySelectorAll("button.seg-btn")].find((b) => b.textContent === "Layers")?.click(); await w(400); document.querySelector("button[aria-label^='Next 14']")?.click(); await w(1200); document.querySelector(".tl-row:not(.tl-final) button.tl-seg")?.click(); })()`],
  ["schedule-edit", `/schedules/${scheds[0].id}/edit`],
  // Behind a drag (real pointer events): the month view with Tue–Thu of one week dragged over.
  ["schedule-drag-days", `/schedules/${scheds[0].id}`, `(async () => { const w = (ms) => new Promise((r) => setTimeout(r, ms)); await w(600); const d = (i) => document.querySelector(\`[data-day="\${i}"]\`); const at = (el, type) => { const r = el.getBoundingClientRect(); el.dispatchEvent(new PointerEvent(type, { bubbles: true, button: 0, pointerId: 1, clientX: r.x + r.width / 2, clientY: r.y + 8 })); }; at(d(15), "pointerdown"); await w(50); at(d(16), "pointermove"); await w(50); at(d(17), "pointermove"); await w(50); at(d(17), "pointerup"); })()`],
  // Behind a drag: the first person in the rotation dragged onto the third.
  ["schedule-edit-reorder", `/schedules/${scheds[0].id}/edit`, `setTimeout(() => { const chips = document.querySelectorAll(".rotation-order")[0].querySelectorAll("[data-pos]"); const at = (el, type, tgt) => { const r = el.getBoundingClientRect(); (tgt || el).dispatchEvent(new PointerEvent(type, { bubbles: true, button: 0, pointerId: 1, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 })); }; at(chips[0].querySelector(".rot-grip"), "pointerdown"); at(chips[1], "pointermove"); setTimeout(() => { const c2 = document.querySelectorAll(".rotation-order")[0].querySelectorAll("[data-pos]"); at(c2[2], "pointermove"); at(c2[2], "pointerup"); }, 100); }, 600)`],
  ["label-rules", "/label-rules"],
  // Behind a click: the tester run on the seeded example (warning + "silent socket" → P1).
  ["label-rules-test", "/label-rules", `[...document.querySelectorAll("button")].find((b) => b.textContent.includes("Apply the rules"))?.click()`],
  ["label-rules-edit", "/label-rules", `[...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "New rule")?.click()`],
  ["routing", "/routing"],
  ["escalation", "/escalation"],
  ["channels", "/channels"],
  // Behind clicks: the webhook form with the Jira preset (method, headers, template, events).
  ["channels-webhook", "/channels", `(() => { const b = (t) => [...document.querySelectorAll("button")].find((x) => x.textContent.trim().includes(t)); b("New channel")?.click(); setTimeout(() => { b("Webhook")?.click(); setTimeout(() => { const sel = [...document.querySelectorAll("select")].find((x) => [...x.options].some((o) => o.value === "jira")); const set = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set; set.call(sel, "jira"); sel.dispatchEvent(new Event("change", { bubbles: true })); const n = document.querySelector("input[placeholder='ops mailbox']"); const iset = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; iset.call(n, "jira ops"); n.dispatchEvent(new Event("input", { bubbles: true })); }, 150); }, 150); })()`],
  ["channels-ai-triage", "/channels", `(() => { const b = (t) => [...document.querySelectorAll("button")].find((x) => x.textContent.trim().includes(t)); b("New channel")?.click(); setTimeout(() => { b("Webhook")?.click(); setTimeout(() => { const sel = [...document.querySelectorAll("select")].find((x) => [...x.options].some((o) => o.value === "ai-triage")); const set = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set; set.call(sel, "ai-triage"); sel.dispatchEvent(new Event("change", { bubbles: true })); const n = document.querySelector("input[placeholder='ops mailbox']"); const iset = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; iset.call(n, "ai triage"); n.dispatchEvent(new Event("input", { bubbles: true })); }, 150); }, 150); })()`],
  ["channels-jira", "/channels", `(() => { const b = (t) => [...document.querySelectorAll("button")].find((x) => x.textContent.trim() === t); b("New channel")?.click(); setTimeout(() => b("Jira")?.click(), 150); })()`],
  ["inhibitions", "/inhibitions"],
  ["deliveries", "/deliveries"],
  ["insight", "/insight"],
  ["alerts", "/alerts"],
  ["alerts-resolved", "/alerts?status=resolved"],
  ["alert-detail", `/alerts/${crash.id}`],
  ["alert-detail-resolved", `/alerts/${resolvedOne.id}`],
  ["alert-detail-ruled", `/alerts/${alerts.find((a) => a.alertname === "AisFeedStale").id}`],
  ["silences", "/silences"],
  ["silence-new", `/silences/new?alert=${crash.id}`],
  ["integrations", "/integrations"],
  ["people", "/people"],
  ["account", "/account"],
  ["account-paging", "/account?tab=paging"],
  ["account-phones", "/account?tab=phones"],
  ["account-appearance", "/account?tab=appearance"],
  ["account-calendar", "/account?tab=calendar"],
  ["account-calendar-created", "/account?tab=calendar", `[...document.querySelectorAll("button")].find((b) => b.textContent.includes("Create subscription link"))?.click()`],
  ["tokens", "/account?tab=api"],
  // Behind a click: a token just created — the curl, MCP command and .mcp.json snippets.
  ["tokens-created", "/account?tab=api", `(() => { const n = document.querySelector("input[placeholder='claude on my laptop']"); const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; set.call(n, "claude on my laptop"); n.dispatchEvent(new Event("input", { bubbles: true })); setTimeout(() => [...document.querySelectorAll("button")].find((b) => b.textContent.includes("Create token"))?.click(), 100); })()`],
  // Behind a click: the pairing panel (code + QR) only exists after "Add a phone".
  ["tokens-pairing", "/account?tab=phones", `[...document.querySelectorAll("button")].find((b) => b.textContent.includes("Add a phone"))?.click()`],
  ["audit", "/audit"],
  ["settings", "/settings"],
  ["settings-email", "/settings?tab=email"],
  ["settings-phones", "/settings?tab=phones"],
  ["settings-network", "/settings?tab=network"],
  // Behind a click: the Slack tab with its manifest shown.
  ["settings-slack", "/settings?tab=slack", `setTimeout(() => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "this manifest")?.click(), 400)`],
  ["channels-slack", "/channels", `(() => { const b = (t) => [...document.querySelectorAll("button")].find((x) => x.textContent.trim() === t); b("New channel")?.click(); setTimeout(() => b("Slack app")?.click(), 150); })()`],
];
const ONLY = process.env.ONLY ? new Set(process.env.ONLY.split(",")) : null;
const VIEWPORTS = [["desktop", 1366, 900, false], ["phone", 390, 844, true]];
const THEMES = ["light", "dark"];

// --- CDP plumbing ----------------------------------------------------------------------
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--remote-debugging-port=0",
  "--no-first-run", `--user-data-dir=${mkdtempSync(join(tmpdir(), "cdp-"))}`, "about:blank"],
  { stdio: ["ignore", "ignore", "pipe"] });
// An interrupted run must not leave a headless Chrome behind: the next run then cannot start.
process.on("exit", () => chrome.kill());
const wsUrl = await new Promise((res, rej) => {
  let buf = "";
  chrome.stderr.on("data", (d) => { buf += d; const m = buf.match(/DevTools listening on (ws:\/\/\S+)/); if (m) res(m[1]); });
  setTimeout(() => rej(new Error("chrome did not start")), 15000);
});
const port = new URL(wsUrl).port;
const target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((t) => t.type === "page");
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let seq = 0;
const pending = new Map();
ws.addEventListener("message", (e) => {
  const msg = JSON.parse(e.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
});
// Every call has a deadline: a page that hangs the renderer must FAIL with its name, not
// stall the whole run with nothing printed.
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++seq;
  const timer = setTimeout(() => { pending.delete(id); rej(new Error(`${method}: timed out`)); }, 20000);
  pending.set(id, (m) => { clearTimeout(timer); m.error ? rej(new Error(`${method}: ${m.error.message}`)) : res(m.result); });
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expr) => (await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true })).result.value;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await send("Page.enable");
await send("Network.enable");
await send("Network.setCookie", { name: cname, value: cvalue, url: BASE });

const PROBE = `(() => {
  const problems = [];
  const vw = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth > vw + 1) problems.push("page scrolls sideways: " + document.documentElement.scrollWidth + " > " + vw);
  const text = document.querySelector(".main")?.innerText || "";
  if (text.trim().length < 40) problems.push("page looks empty");
  for (const bad of ["undefined", "NaN", "Invalid Date", "[object Object]"]) if (text.includes(bad)) problems.push("text contains " + bad);
  const inScroll = (el) => { for (let p = el.parentElement; p; p = p.parentElement) { const s = getComputedStyle(p); if (/(auto|scroll|hidden)/.test(s.overflowX) && p.scrollWidth > p.clientWidth) return true; } return false; };
  for (const card of document.querySelectorAll(".card")) {
    const cr = card.getBoundingClientRect();
    for (const el of card.querySelectorAll("*")) {
      const r = el.getBoundingClientRect();
      if (r.width && r.right > cr.right + 2 && !inScroll(el)) { problems.push("sticks out of a card: " + el.tagName.toLowerCase() + "." + (el.className || "")); break; }
    }
  }
  return problems;
})()`;

const report = [];
for (const theme of THEMES) {
  await send("Page.addScriptToEvaluateOnNewDocument", { source: `try { localStorage.setItem("shorthop.theme", "${theme}"); } catch (e) {}` });
  for (const [vp, w, h, mobile] of VIEWPORTS) {
    await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
    for (const [name, path, action] of PAGES) {
      if (ONLY && !ONLY.has(name)) continue;
      let problems;
      try {
        await send("Page.navigate", { url: BASE + path });
        await sleep(1400);
        if (action) { await evaluate(action); await sleep(700); }
        problems = await evaluate(PROBE);
        if (name === "tokens-pairing" && !(await evaluate(`!!document.querySelector("img.pair-qr")?.naturalWidth`))) problems.push("no QR code rendered");
        const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
        writeFileSync(join(OUT, `${name}-${vp}-${theme}.png`), Buffer.from(shot.data, "base64"));
      } catch (e) {
        problems = [`render failed: ${e.message}`];
      }
      report.push({ page: name, vp, theme, problems });
      console.log(`${problems.length ? "FAIL" : "ok  "} ${name} ${vp} ${theme}${problems.length ? "  " + problems.join("; ") : ""}`);
    }
  }
}
ws.close();
chrome.kill();
const failed = report.filter((r) => r.problems.length);
console.log(`\n${report.length - failed.length}/${report.length} pages clean — screenshots in ${OUT}`);
process.exit(failed.length ? 1 : 0);
