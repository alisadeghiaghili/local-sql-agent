// tests/web_ui/run_admin_card_errors.mjs
//
// Node-side half of tests/web_ui/test_web_ui_admin_card_errors.py.
//
// Drives the REAL web/admin/main.js (module-graph copy, as in
// run_admin_auto_refresh.mjs) with every /admin/* call failing in the way
// given as argv[3], and prints, as one JSON line, what the page ended up
// showing: the HTML of each card body ("<name>-body"), the 403 banner, and
// the notice area. The Python test asserts that no card is blank.
//
// Usage: node run_admin_card_errors.mjs <path-to-copied-main.mjs> <scenario-json>
//
// scenario-json: {"status": 401|403|500, "key": true|false, "network": false}
//   status   HTTP status every call answers with
//   key      whether an API key is stored in localStorage
//   network  when true, fetch rejects (no response at all) instead

import { pathToFileURL } from "node:url";

const [mainMjsPath, scenarioJson] = process.argv.slice(2);
if (!mainMjsPath || !scenarioJson) {
  console.error("usage: node run_admin_card_errors.mjs <main.mjs> <scenario-json>");
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

// Mirrors main.js's CARDS list. The Python test pins it to the real file,
// so a card added there without being added here fails loudly.
const CARDS = [
  "summary", "health", "cache", "config", "feedback", "accessRequests",
  "maintenance", "keys", "schemaDrift", "vocabulary", "usage", "authFailures",
];

// Plain recorders for the elements under test.
const bodies = Object.fromEntries(CARDS.map((name) => [name, { innerHTML: "", disabled: false }]));
const banner = { textContent: "", hidden: true };
const notice = { innerHTML: "", children: [], appendChild(child) { this.children.push(child); } };

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
    if (id === "admin-forbidden-banner") return banner;
    if (id === "admin-notice-area") return notice;
    return makeFakeElement();
  },
  createElement() { return { className: "", textContent: "" }; },
  querySelectorAll() { return []; },
  querySelector() { return makeFakeElement(); },
  addEventListener() {},
};

globalThis.fetch = async () => {
  if (scenario.network) throw new TypeError("Failed to fetch");
  return {
    ok: false,
    status: scenario.status,
    json: async () => ({ error: { code: "TEST", message: "refused by the test server" } }),
  };
};
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {};

// The harness shares main.mjs's apikey module instance (same file URL).
const apikeyUrl = pathToFileURL(mainMjsPath.replace(/admin[\\/]main\.mjs$/, "js/apikey.mjs")).href;
const { setApiKey, clearApiKey } = await import(apikeyUrl);
if (scenario.key) setApiKey("stored-key"); else clearApiKey();

await import(pathToFileURL(mainMjsPath).href);
await new Promise((resolve) => setTimeout(resolve, 100));

console.log(JSON.stringify({
  bodies: Object.fromEntries(CARDS.map((name) => [name, bodies[name].innerHTML])),
  banner: { hidden: banner.hidden, text: banner.textContent },
  notice: notice.children.map((c) => c.textContent),
  cards: CARDS,
}));
