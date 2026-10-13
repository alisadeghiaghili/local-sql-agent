// tests/web_ui/run_admin_quality_card.mjs
//
// Node-side half of tests/web_ui/test_web_ui_admin_quality_card.py.
//
// Drives the REAL web/admin/main.js (module-graph copy, as in
// run_admin_card_errors.mjs) with GET /admin/quality answering as given in
// argv[3], and prints, as one JSON line, what the "quality" card ended up
// showing plus which /admin/quality URLs were fetched at boot, on the 30s
// auto-refresh tick and on the card's own refresh button.
//
// Usage: node run_admin_quality_card.mjs <path-to-copied-main.mjs> <scenario-json>
//
// scenario-json:
//   payload   the JSON body GET /admin/quality answers with (default {})
//   status    HTTP status for that endpoint (default 200)
//   days      the value of the card's window <select> (default "7")
//   tick      when true, also invoke the 30s auto-refresh callback once
//   click     when true, also press the card's own refresh button
//   change    when true, also fire the window <select>'s change event

import { pathToFileURL } from "node:url";

const [mainMjsPath, scenarioJson] = process.argv.slice(2);
if (!mainMjsPath || !scenarioJson) {
  console.error("usage: node run_admin_quality_card.mjs <main.mjs> <scenario-json>");
  process.exit(2);
}
const scenario = JSON.parse(scenarioJson);

// A maximally permissive fake element (see run_admin_auto_refresh.mjs).
function makeFakeElement() {
  const fn = function () { return makeFakeElement(); };
  const scalarDefaults = {
    value: "", checked: false, hidden: false, disabled: false,
    textContent: "", innerHTML: "", className: "",
  };
  return new Proxy(fn, {
    get(_target, prop) {
      if (prop in scalarDefaults) return scalarDefaults[prop];
      if (prop === "classList") return { add() {}, remove() {}, contains() { return false; }, toggle() {} };
      if (prop === "dataset") return {};
      if (prop === "style") return {};
      return makeFakeElement();
    },
    set() { return true; },
    apply() { return makeFakeElement(); },
  });
}

// Mirrors main.js's CARDS list. The Python test pins it to the real file.
const CARDS = [
  "summary", "health", "cache", "config", "feedback", "accessRequests",
  "maintenance", "keys", "schemaDrift", "vocabulary", "quality", "usage", "authFailures",
];

const bodies = Object.fromEntries(CARDS.map((name) => [name, { innerHTML: "", disabled: false }]));
const banner = { textContent: "", hidden: true };
const notice = { innerHTML: "", children: [], appendChild(child) { this.children.push(child); } };

const daysSelect = { value: scenario.days ?? "7", listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } };
const qualityButton = { dataset: { refresh: "quality" }, disabled: false, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } };

globalThis.localStorage = (() => {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  };
})();
globalThis.location = { protocol: "http:", hostname: "localhost", search: "", href: "http://localhost/admin/" };
globalThis.document = {
  hidden: false,
  documentElement: { removeAttribute() {}, setAttribute() {} },
  getElementById(id) {
    const card = CARDS.find((name) => id === `${name}-body`);
    if (card) return bodies[card];
    if (id === "quality-days") return daysSelect;
    if (id === "admin-forbidden-banner") return banner;
    if (id === "admin-notice-area") return notice;
    return makeFakeElement();
  },
  createElement() { return { className: "", textContent: "" }; },
  querySelectorAll(selector) { return selector === "[data-refresh]" ? [qualityButton] : []; },
  querySelector(selector) { return selector === '[data-refresh="quality"]' ? qualityButton : makeFakeElement(); },
  addEventListener() {},
};

let qualityUrls = [];
globalThis.fetch = async (url) => {
  const s = String(url);
  if (/\/admin\/quality/.test(s)) {
    qualityUrls.push(s);
    const status = scenario.status ?? 200;
    if (status !== 200) {
      return { ok: false, status, json: async () => ({ error: { code: "TEST", message: "refused by the test server" } }) };
    }
    return { ok: true, status: 200, json: async () => scenario.payload ?? {} };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};

const intervals = [];
globalThis.setInterval = (callback, delayMs) => { intervals.push({ callback, delayMs }); return intervals.length; };
globalThis.clearInterval = () => {};

const apikeyUrl = pathToFileURL(mainMjsPath.replace(/admin[\\/]main\.mjs$/, "js/apikey.mjs")).href;
const { setApiKey } = await import(apikeyUrl);
setApiKey("stored-key");

await import(pathToFileURL(mainMjsPath).href);
const settle = () => new Promise((resolve) => setTimeout(resolve, 80));
await settle();

const bootUrls = qualityUrls.slice();
const bootBody = bodies.quality.innerHTML;

qualityUrls = [];
if (scenario.tick) {
  const tick = intervals.find((e) => e.delayMs === 30_000);
  tick.callback();
  await settle();
}
const tickUrls = qualityUrls.slice();

qualityUrls = [];
if (scenario.click) {
  qualityButton.listeners.click();
  await settle();
}
const clickUrls = qualityUrls.slice();

qualityUrls = [];
if (scenario.change) {
  daysSelect.value = "30";
  daysSelect.listeners.change();
  await settle();
}
const changeUrls = qualityUrls.slice();

console.log(JSON.stringify({
  body: bootBody,
  finalBody: bodies.quality.innerHTML,
  bootUrls, tickUrls, clickUrls, changeUrls,
  cards: CARDS,
}));
