// tests/web_ui/run_admin_diagnosis.mjs
//
// Node-side half of tests/web_ui/test_web_ui_admin_diagnosis.py.
//
// Drives the REAL web/admin/main.js (module-graph copy, as in
// run_admin_card_errors.mjs) while the address it calls answers the way the
// scenario says, and prints, as one JSON line, what the page ended up showing:
// each card body, the diagnosis banner (its text, whether it is hidden, how
// many times its text was written), the 403 banner, and every URL fetched.
//
// Usage: node run_admin_diagnosis.mjs <path-to-copied-main.mjs> <scenario-json>
//
// scenario-json:
//   page    the page's own origin (location.origin); default http://localhost:8077
//   mode    what the configured API address does:
//     "static_404"   a static file server: 404 HTML to everything
//     "html_200"     a server that returns its index page (200 HTML) to everything
//     "wrong_health" 404 to /admin/*, and a 200 JSON that is not the API's to /health
//     "network"      every fetch rejects with a TypeError (refused CORS or no host)
//     "api_500"      the real API: /admin/* answers 500 JSON, /health the real shape
//     "api_403"      the real API: /admin/* answers 403 JSON, /health the real shape
//     "api_404"      the real API, but /admin/* answers 404 JSON (a route it lacks)
//   key     whether an API key is stored (default true)

import { pathToFileURL } from "node:url";

const [mainMjsPath, scenarioJson] = process.argv.slice(2);
if (!mainMjsPath || !scenarioJson) {
  console.error("usage: node run_admin_diagnosis.mjs <main.mjs> <scenario-json>");
  process.exit(2);
}
const scenario = JSON.parse(scenarioJson);
const PAGE = scenario.page || "http://localhost:8077";
const pageUrl = new URL(PAGE);

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

const CARDS = [
  "summary", "health", "cache", "config", "feedback", "accessRequests",
  "maintenance", "keys", "schemaDrift", "vocabulary", "quality", "usage", "authFailures",
];
const bodies = Object.fromEntries(CARDS.map((name) => [name, { innerHTML: "", disabled: false }]));
const forbiddenBanner = { textContent: "", hidden: true };
const notice = { innerHTML: "", children: [], appendChild(child) { this.children.push(child); } };
// The diagnosis banner counts how often its text is written: "shown once".
const diagnosisBanner = {
  hidden: true,
  writes: 0,
  _text: "",
  get textContent() { return this._text; },
  set textContent(v) { this.writes += 1; this._text = v; },
};

globalThis.localStorage = (() => {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  };
})();
globalThis.location = {
  protocol: pageUrl.protocol, hostname: pageUrl.hostname, origin: pageUrl.origin,
  search: "", href: `${PAGE}/admin/`,
};
globalThis.document = {
  hidden: false,
  documentElement: { removeAttribute() {}, setAttribute() {} },
  getElementById(id) {
    const card = CARDS.find((name) => id === `${name}-body`);
    if (card) return bodies[card];
    if (id === "admin-forbidden-banner") return forbiddenBanner;
    if (id === "admin-diagnosis-banner") return diagnosisBanner;
    if (id === "admin-notice-area") return notice;
    return makeFakeElement();
  },
  createElement() { return { className: "", textContent: "" }; },
  querySelectorAll() { return []; },
  querySelector() { return makeFakeElement(); },
  addEventListener() {},
};

const fetched = [];
function reply({ status, contentType, body, jsonThrows }) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (n) => (n.toLowerCase() === "content-type" ? contentType : null) },
    json: async () => {
      if (jsonThrows) throw new SyntaxError("Unexpected token <");
      return body;
    },
  };
}
const REAL_HEALTH = { status: "ok", openai: true, database: true };
globalThis.fetch = async (url) => {
  fetched.push(String(url));
  const isHealth = String(url).endsWith("/health");
  switch (scenario.mode) {
    case "static_404":
      return reply({ status: 404, contentType: "text/html", jsonThrows: true });
    case "html_200":
      return reply({ status: 200, contentType: "text/html", jsonThrows: true });
    case "wrong_health":
      return isHealth
        ? reply({ status: 200, contentType: "application/json", body: { service: "something else" } })
        : reply({ status: 404, contentType: "application/json", body: { detail: "Not Found" } });
    case "network":
      throw new TypeError("Failed to fetch");
    case "api_500":
      return isHealth
        ? reply({ status: 200, contentType: "application/json", body: REAL_HEALTH })
        : reply({ status: 500, contentType: "application/json", body: { error: { code: "TEST", message: "refused by the test server" } } });
    case "api_404":
      return isHealth
        ? reply({ status: 200, contentType: "application/json", body: REAL_HEALTH })
        : reply({ status: 404, contentType: "application/json", body: { error: { code: "NOT_FOUND", message: "no such admin route" } } });
    case "api_403":
      return isHealth
        ? reply({ status: 200, contentType: "application/json", body: REAL_HEALTH })
        : reply({ status: 403, contentType: "application/json", body: { error: { code: "ADMIN_REQUIRED", message: "not an admin key" } } });
    default:
      throw new Error(`unknown mode ${scenario.mode}`);
  }
};
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {};

const apikeyUrl = pathToFileURL(mainMjsPath.replace(/admin[\\/]main\.mjs$/, "js/apikey.mjs")).href;
const { setApiKey, clearApiKey } = await import(apikeyUrl);
if (scenario.key === false) clearApiKey(); else setApiKey("stored-key");

await import(pathToFileURL(mainMjsPath).href);
await new Promise((resolve) => setTimeout(resolve, 150));

console.log(JSON.stringify({
  bodies: Object.fromEntries(CARDS.map((name) => [name, bodies[name].innerHTML])),
  diagnosis: { hidden: diagnosisBanner.hidden, text: diagnosisBanner._text, writes: diagnosisBanner.writes },
  forbidden: { hidden: forbiddenBanner.hidden, text: forbiddenBanner.textContent },
  fetched,
  cards: CARDS,
}));
