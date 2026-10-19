// tests/web_ui/run_api_diagnosis.mjs
//
// Node-side half of tests/web_ui/test_web_ui_api_diagnosis.py.
//
// Drives the REAL web/js/diagnosis.js (copied to .mjs by the wrapper) against
// a fake `fetch` and prints, as one JSON line, what it concluded for each
// scenario: the classification, which URLs were probed, the Persian text, how
// often the shared banner was written. The wrapper asserts on that.
//
// Usage: node run_api_diagnosis.mjs <path-to-copied-diagnosis.mjs>

import { pathToFileURL } from "node:url";

const [modulePath] = process.argv.slice(2);
if (!modulePath) {
  console.error("usage: node run_api_diagnosis.mjs <diagnosis.mjs>");
  process.exit(2);
}
const d = await import(pathToFileURL(modulePath).href);

const PAGE = "http://localhost:8077";
const BASE = "http://localhost:8000";
const REAL_HEALTH = { status: "ok", openai: true, database: false, model: "m" };

function response({ status = 200, contentType = "application/json", body = undefined, jsonThrows = false }) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (name) => (name.toLowerCase() === "content-type" ? contentType : null) },
    json: async () => {
      if (jsonThrows) throw new SyntaxError("Unexpected token < in JSON");
      return body;
    },
  };
}

/** A fake fetch that answers every URL with *answer* and remembers the URLs. */
function fakeFetch(answer) {
  const urls = [];
  const f = async (url) => {
    urls.push(url);
    if (answer instanceof Error) throw answer;
    return answer;
  };
  f.urls = urls;
  return f;
}

const NETWORK = { type: "network" };
const HTTP_404 = { type: "http", status: 404 };

async function run(failure, fetchImpl, { base = BASE, page = PAGE } = {}) {
  const diagnosis = await d.diagnoseApiFailure({ baseUrl: base, pageOrigin: page, failure, fetchImpl });
  return { diagnosis, probed: fetchImpl.urls };
}

const out = { cases: {}, messages: {}, mapping: {}, shape: {}, diagnoser: {}, banner: {} };

/* ── Classification: every branch ─────────────────────────────────── */
out.cases.page_origin_equal_static_server = await run(HTTP_404, fakeFetch(response({ status: 404, contentType: "text/html" })), { base: PAGE });
out.cases.page_origin_equal_no_answer = await run(NETWORK, fakeFetch(new TypeError("Failed to fetch")), { base: PAGE });
out.cases.page_origin_equal_but_it_is_the_api = await run(HTTP_404, fakeFetch(response({ body: REAL_HEALTH })), { base: PAGE });
out.cases.health_404 = await run(HTTP_404, fakeFetch(response({ status: 404, contentType: "text/plain" })));
out.cases.health_html = await run(HTTP_404, fakeFetch(response({ contentType: "text/html; charset=utf-8", jsonThrows: true })));
out.cases.health_json_wrong_shape = await run(HTTP_404, fakeFetch(response({ body: { hello: "world" } })));
out.cases.health_json_null = await run(HTTP_404, fakeFetch(response({ body: null })));
out.cases.health_text_plain_200 = await run(HTTP_404, fakeFetch(response({ contentType: "text/plain", jsonThrows: true })));
out.cases.network_type_error = await run(NETWORK, fakeFetch(response({ body: REAL_HEALTH })));
out.cases.real_api_answers_health = await run(HTTP_404, fakeFetch(response({ body: REAL_HEALTH })));
out.cases.payload_failure_on_a_static_server = await run({ type: "payload" }, fakeFetch(response({ contentType: "text/html", jsonThrows: true })));
out.cases.probe_cannot_reach_either = await run(HTTP_404, fakeFetch(new TypeError("Failed to fetch")));
for (const status of [401, 403, 429, 500, 503]) {
  out.cases[`http_${status}`] = await run({ type: "http", status }, fakeFetch(response({ status: 404 })));
}
out.cases.http_405 = await run({ type: "http", status: 405 }, fakeFetch(response({ status: 404 })));
out.cases.no_failure = await run(null, fakeFetch(response({ body: REAL_HEALTH })));

/* ── Mapping an error to a failure ────────────────────────────────── */
const fromError = d.failureFromError;
out.mapping.type_error = fromError(new TypeError("Failed to fetch"));
out.mapping.transport_code = fromError(Object.assign(new Error("x"), { code: "TRANSPORT_ERROR", status: 0 }));
out.mapping.payload_code = fromError(Object.assign(new Error("x"), { code: "UNEXPECTED_PAYLOAD", status: 200 }));
out.mapping.syntax_error = fromError(new SyntaxError("Unexpected token <"));
out.mapping.http = fromError(Object.assign(new Error("x"), { status: 404 }));
out.mapping.unknown = fromError(new Error("something else"));
out.mapping.nothing = fromError(null);

/* ── The shape of the API's own /health ───────────────────────────── */
out.shape.real = d.isApiHealthShape(REAL_HEALTH);
out.shape.degraded_without_model = d.isApiHealthShape({ status: "degraded", openai: false, database: false });
out.shape.empty_object = d.isApiHealthShape({});
out.shape.wrong_status = d.isApiHealthShape({ status: "up", openai: true, database: true });
out.shape.string_flags = d.isApiHealthShape({ status: "ok", openai: "true", database: true });
out.shape.array = d.isApiHealthShape([REAL_HEALTH]);
out.shape.null = d.isApiHealthShape(null);

/* ── The Persian texts ────────────────────────────────────────────── */
const sameOrigin = (await run(HTTP_404, fakeFetch(response({ status: 404 })), { base: PAGE })).diagnosis;
const notApi = (await run(HTTP_404, fakeFetch(response({ status: 404 })))).diagnosis;
const notApiHtml = (await run(HTTP_404, fakeFetch(response({ contentType: "text/html", jsonThrows: true })))).diagnosis;
const notApiShape = (await run(HTTP_404, fakeFetch(response({ body: {} })))).diagnosis;
const corsOrUnreachable = (await run(NETWORK, fakeFetch(new TypeError("x")))).diagnosis;
out.messages.wrong_target_same_origin_admin = d.describeDiagnosis(sameOrigin, "admin");
out.messages.wrong_target_same_origin_analyst = d.describeDiagnosis(sameOrigin, "analyst");
out.messages.wrong_target_404_admin = d.describeDiagnosis(notApi, "admin");
out.messages.wrong_target_html_admin = d.describeDiagnosis(notApiHtml, "admin");
out.messages.wrong_target_shape_admin = d.describeDiagnosis(notApiShape, "admin");
out.messages.cors_or_unreachable_admin = d.describeDiagnosis(corsOrUnreachable, "admin");
out.messages.cors_or_unreachable_analyst = d.describeDiagnosis(corsOrUnreachable, "analyst");
out.messages.other = d.describeDiagnosis({ kind: d.KIND_OTHER, baseUrl: BASE, pageOrigin: PAGE }, "admin");
out.messages.card = d.CARD_UNREACHABLE_TEXT;

/* ── The shared diagnoser: one probe for a whole round ────────────── */
{
  const f = fakeFetch(response({ status: 404 }));
  const diagnoser = d.createDiagnoser({ getBaseUrl: () => BASE, getPageOrigin: () => PAGE, fetchImpl: f });
  const err = Object.assign(new Error("HTTP 404"), { status: 404 });
  const round = await Promise.all(Array.from({ length: 13 }, () => diagnoser.diagnose(err)));
  out.diagnoser.thirteen_cards_kinds = [...new Set(round.map((x) => x.kind))];
  out.diagnoser.thirteen_cards_probes = f.urls.length;
  await diagnoser.diagnose(err);
  out.diagnoser.same_round_again_probes = f.urls.length;
  diagnoser.reset();
  await diagnoser.diagnose(err);
  out.diagnoser.after_reset_probes = f.urls.length;
  const forbidden = await diagnoser.diagnose(Object.assign(new Error("no"), { status: 403 }));
  out.diagnoser.forbidden_kind = forbidden.kind;
  out.diagnoser.forbidden_probes = f.urls.length;
}
{
  let base = BASE;
  const f = fakeFetch(response({ status: 404 }));
  const diagnoser = d.createDiagnoser({ getBaseUrl: () => base, getPageOrigin: () => PAGE, fetchImpl: f });
  const err = Object.assign(new Error("HTTP 404"), { status: 404 });
  await diagnoser.diagnose(err);
  base = "http://localhost:9000";
  await diagnoser.diagnose(err);
  out.diagnoser.a_new_address_is_a_new_question = f.urls;
}

/* ── The banner: written once, hidden when there is nothing to say ── */
{
  let writes = 0;
  let text = "";
  const el = {
    hidden: true,
    get textContent() { return text; },
    set textContent(v) { writes += 1; text = v; },
  };
  for (let i = 0; i < 13; i += 1) d.showDiagnosisBanner(el, notApi, "admin");
  out.banner.after_thirteen_calls = { writes, hidden: el.hidden, text };
  d.showDiagnosisBanner(el, { kind: d.KIND_OTHER, baseUrl: BASE, pageOrigin: PAGE }, "admin");
  out.banner.after_other = { hidden: el.hidden };
  d.showDiagnosisBanner(el, corsOrUnreachable, "admin");
  d.hideDiagnosisBanner(el);
  out.banner.after_hide = { hidden: el.hidden };
  out.banner.no_element_is_fine = d.showDiagnosisBanner(null, notApi, "admin");
}

console.log(JSON.stringify(out));
