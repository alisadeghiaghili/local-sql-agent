// tests/web_ui/run_admin_schema_drift.mjs
//
// Node-side half of tests/web_ui/test_web_ui_admin_schema_drift.py.
//
// Drives the REAL web/admin/main.js (module-graph copy, as in
// run_admin_auto_refresh.mjs) with /admin/schema-drift answering the JSON
// given as argv[3], and prints, as one JSON line, the HTML main.js wrote
// into the schema-drift card ("schemaDrift-body"). The Python test asserts
// on that HTML.
//
// Usage: node run_admin_schema_drift.mjs <path-to-copied-main.mjs> <drift-json>

import { pathToFileURL } from "node:url";

const [mainMjsPath, driftJson] = process.argv.slice(2);
if (!mainMjsPath || !driftJson) {
  console.error("usage: node run_admin_schema_drift.mjs <main.mjs> <drift-json>");
  process.exit(2);
}

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

// The one element under test records what is assigned to innerHTML.
const driftBody = { innerHTML: "" };

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
  getElementById(id) { return id === "schemaDrift-body" ? driftBody : makeFakeElement(); },
  createElement() { return makeFakeElement(); },
  querySelectorAll() { return []; },
  querySelector() { return makeFakeElement(); },
  addEventListener() {},
};

const drift = JSON.parse(driftJson);
globalThis.fetch = async (url) => {
  const body = /\/admin\/schema-drift/.test(String(url)) ? drift : {};
  return { ok: true, status: 200, json: async () => body };
};
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {};

await import(pathToFileURL(mainMjsPath).href);
await new Promise((resolve) => setTimeout(resolve, 50));

console.log(JSON.stringify({ html: driftBody.innerHTML }));
