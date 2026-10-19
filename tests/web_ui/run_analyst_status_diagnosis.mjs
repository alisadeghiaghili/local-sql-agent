// tests/web_ui/run_analyst_status_diagnosis.mjs
//
// Node-side half of tests/web_ui/test_web_ui_analyst_status_diagnosis.py.
//
// Runs the REAL, unmodified `refreshHealth` (extracted verbatim from
// web/js/main.js by the wrapper) with the REAL diagnosis.js, against a fake
// `Api.health()` and a fake `fetch`, and prints, as one JSON line, what the
// status view ended up showing for each scenario: the three lights, the
// tooltip, the banner's text, whether it is hidden and how often its text was
// written.
//
// Usage: node run_analyst_status_diagnosis.mjs <path-to-built-module.mjs>

import { pathToFileURL } from "node:url";

const [modulePath] = process.argv.slice(2);
if (!modulePath) {
  console.error("usage: node run_analyst_status_diagnosis.mjs <built-module.mjs>");
  process.exit(2);
}
const h = await import(pathToFileURL(modulePath).href);

const PAGE = "http://localhost:8077";
const BASE = "http://localhost:8000";
globalThis.location = { origin: PAGE };
h.state.baseUrl = BASE;

const REAL_HEALTH = { status: "ok", openai: true, database: true, model: "m" };

function reply({ status = 200, contentType = "application/json", body, jsonThrows = false }) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (n) => (n.toLowerCase() === "content-type" ? contentType : null) },
    json: async () => {
      if (jsonThrows) throw new SyntaxError("Unexpected token <");
      return body;
    },
  };
}

/** What Api.health() resolves with for a healthy API. */
function healthy(raw) {
  return { api: true, llm: !!raw.openai, db: !!raw.database, status: raw.status, model: raw.model || null, llmDetail: null, dbDetail: null, raw };
}
const transportError = () => new h.ApiError("Network error calling /health: Failed to fetch", 0, "TRANSPORT_ERROR");

async function scenario(name, { health, probe, times = 1 }) {
  h.reset();
  const probed = [];
  globalThis.fetch = async (url) => {
    probed.push(String(url));
    if (probe instanceof Error) throw probe;
    return probe;
  };
  h.setHealthImpl(health);
  for (let i = 0; i < times; i += 1) await h.refreshHealth();
  return { name, lights: h.lastLights(), tooltip: h.lastTooltip(), banner: h.bannerState(), probed, setHealthCalls: h.setHealthCalls() };
}

const out = {};
out.real_api = await scenario("real_api", { health: async () => healthy(REAL_HEALTH), probe: reply({ body: REAL_HEALTH }) });
out.cors_refused = await scenario("cors_refused", { health: async () => { throw transportError(); }, probe: new TypeError("Failed to fetch") });
out.static_server_404 = await scenario("static_server_404", {
  health: async () => { throw new h.ApiError("HTTP 404", 404); },
  probe: reply({ status: 404, contentType: "text/html", jsonThrows: true }),
});
out.html_200 = await scenario("html_200", {
  health: async () => { throw new SyntaxError("Unexpected token <"); },
  probe: reply({ contentType: "text/html", jsonThrows: true }),
});
out.other_service_json = await scenario("other_service_json", {
  health: async () => healthy({ service: "something else" }),
  probe: reply({ body: { service: "something else" } }),
});
out.server_error = await scenario("server_error", {
  health: async () => { throw new h.ApiError("HTTP 500", 500); },
  probe: reply({ body: REAL_HEALTH }),
});

// Three checks in a row against the same wrong address: still one banner text.
out.three_failures_in_a_row = await scenario("three_failures", {
  health: async () => { throw transportError(); },
  probe: new TypeError("Failed to fetch"),
  times: 3,
});

// A failure and then a success: the banner goes away.
{
  h.reset();
  globalThis.fetch = async () => reply({ status: 404, contentType: "text/html", jsonThrows: true });
  h.setHealthImpl(async () => { throw new h.ApiError("HTTP 404", 404); });
  await h.refreshHealth();
  const whileWrong = h.bannerState();
  h.setHealthImpl(async () => healthy(REAL_HEALTH));
  await h.refreshHealth();
  out.fixed_afterwards = { whileWrong, afterFix: h.bannerState() };
}

// Two checks overlap: the older one finishes last and must not overwrite the newer answer.
{
  h.reset();
  let releaseOld;
  const oldProbe = new Promise((resolve) => { releaseOld = resolve; });
  globalThis.fetch = async () => { await oldProbe; return reply({ status: 404, contentType: "text/html", jsonThrows: true }); };
  h.setHealthImpl(async () => { throw new h.ApiError("HTTP 404", 404); });
  const older = h.refreshHealth();
  h.setHealthImpl(async () => healthy(REAL_HEALTH));
  const newer = h.refreshHealth();
  await newer;
  releaseOld();
  await older;
  out.overlapping_checks = h.bannerState();
}

console.log(JSON.stringify(out));
