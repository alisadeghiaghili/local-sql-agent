/* web/js/diagnosis.js — what is wrong with the address the page calls?
 *
 * Shared by the analyst page (web/js/main.js) and the admin panel
 * (web/admin/main.js). Both used to answer a failed call with one message
 * per call: thirteen identical cards on the admin panel, a tooltip nobody
 * hovers on the analyst status lights. Two real incidents (6.14.0) show
 * what that hid:
 *
 *   - All three status lights red while the server log held only
 *     `OPTIONS /health` ... `400`. The server allowed
 *     http://172.16.101.42:8077 and the page had been opened as
 *     http://localhost:8077. A browser reports a refused cross-origin call
 *     to the page as the same opaque TypeError an unreachable host gives.
 *   - Every admin card said 404 and the API log had no /admin request at
 *     all. The address saved in this browser pointed at the static file
 *     server, which answers 404 to everything it does not serve.
 *
 * diagnoseApiFailure() turns ONE failed call into one of three answers:
 *
 *   wrong_target         the base address is the page's own server, or a
 *                        server that answers GET {base}/health with a 404,
 *                        an HTML page, or JSON that is not the API's health
 *                        response: the address is not the API.
 *   cors_or_unreachable  the call never got a response (fetch threw its
 *                        TypeError). A browser does not let a page tell a
 *                        refused cross-origin call from an unreachable
 *                        host, so the answer names both, with both remedies.
 *   other                anything else: the caller keeps its own message
 *                        for that status (401, 403 and 500 are the API
 *                        answering, and say so themselves).
 *
 * Limits, stated so nobody reads more into an answer than it holds:
 *   - cors_or_unreachable is a guess between two causes, by construction.
 *   - wrong_target only looks at /health. A server that serves the API's
 *     /health shape but answers other routes with 404 is "other".
 *   - A same-origin API (the page and the API behind one proxy) is only
 *     called wrong_target when its /health is not the API's.
 *   - Only 404, 405 and 501 are probed. A static file server answers those
 *     to a request it does not serve; 401, 403, 429 and 5xx can only come
 *     from something that understood the request.
 *
 * No import, no DOM access beyond the element handed to the banner helpers,
 * and `fetch` only through the injected `fetchImpl` (default: the global),
 * so each function runs under Node.
 */

"use strict";

export const KIND_WRONG_TARGET = "wrong_target";
export const KIND_CORS_OR_UNREACHABLE = "cors_or_unreachable";
export const KIND_OTHER = "other";

/** `err.code` of a call that got no response at all (api.js, admin.js). */
export const CODE_TRANSPORT_ERROR = "TRANSPORT_ERROR";
/** `err.code` of a call that got a response whose body is not JSON. */
export const CODE_UNEXPECTED_PAYLOAD = "UNEXPECTED_PAYLOAD";

/** What a card says instead of repeating the page-level message. */
export const CARD_UNREACHABLE_TEXT = "به API نرسید؛ پیام بالای صفحه را ببینید.";

/** Statuses a static file server answers to a request it does not serve. */
const PROBE_STATUSES = new Set([404, 405, 501]);

const HEALTH_STATUSES = ["ok", "degraded", "down"];

/** Whether *body* has the shape of the API's `GET /health` response
 * (api/models.py `HealthResponse`: `status`, `openai`, `database`). */
export function isApiHealthShape(body) {
  return (
    body !== null && typeof body === "object" && !Array.isArray(body) &&
    HEALTH_STATUSES.includes(body.status) &&
    typeof body.openai === "boolean" &&
    typeof body.database === "boolean"
  );
}

/** Reduces a thrown error to `{type: "network" | "payload" | "http", status?}`,
 * or `null` when it is none of those (nothing to diagnose). */
export function failureFromError(err) {
  if (!err) return null;
  if (err.code === CODE_TRANSPORT_ERROR || err instanceof TypeError) return { type: "network" };
  if (err.code === CODE_UNEXPECTED_PAYLOAD || err instanceof SyntaxError) return { type: "payload" };
  if (typeof err.status === "number" && err.status > 0) return { type: "http", status: err.status };
  return null;
}

function originOf(address) {
  try {
    return new URL(address).origin;
  } catch {
    return null;
  }
}

/** `GET {baseUrl}/health` and says whether what answered is the API.
 * `reason` is "ok" | "404" | "html" | "shape" | "network". */
export async function probeHealth(baseUrl, { fetchImpl, timeoutMs = 4000 } = {}) {
  const doFetch = fetchImpl || ((...args) => globalThis.fetch(...args));
  let res;
  try {
    res = await doFetch(`${String(baseUrl).replace(/\/+$/, "")}/health`, {
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch {
    return { isApi: false, reason: "network" };
  }
  const contentType = String((res.headers && res.headers.get && res.headers.get("content-type")) || "").toLowerCase();
  if (res.status === 404) return { isApi: false, reason: "404" };
  if (contentType.includes("html")) return { isApi: false, reason: "html" };
  let body = null;
  try {
    body = await res.json();
  } catch { /* not JSON: judged below */ }
  return isApiHealthShape(body) ? { isApi: true, reason: "ok" } : { isApi: false, reason: "shape" };
}

/** Classifies one failed call. *failure* is failureFromError()'s result.
 * Never throws. Returns `{kind, baseUrl, pageOrigin, why}`; `why` is
 * "same_origin" | "404" | "html" | "shape" for wrong_target, else null. */
export async function diagnoseApiFailure({ baseUrl, pageOrigin, failure, fetchImpl, timeoutMs }) {
  const answer = (kind, why = null) => ({ kind, baseUrl, pageOrigin, why });
  if (!failure) return answer(KIND_OTHER);
  if (failure.type === "http" && !PROBE_STATUSES.has(failure.status)) return answer(KIND_OTHER);

  const sameOrigin = !!pageOrigin && originOf(baseUrl) === pageOrigin;
  if (failure.type === "network" && !sameOrigin) return answer(KIND_CORS_OR_UNREACHABLE);

  const probe = await probeHealth(baseUrl, { fetchImpl, timeoutMs });
  if (probe.isApi) return answer(KIND_OTHER);
  if (sameOrigin) return answer(KIND_WRONG_TARGET, "same_origin");
  if (probe.reason === "network") return answer(KIND_OTHER);
  return answer(KIND_WRONG_TARGET, probe.reason);
}

const WHY_WRONG_TARGET = {
  404: "پاسخ /health آن 404 است",
  html: "پاسخ /health آن صفحهٔ HTML است",
  shape: "پاسخ /health آن شکل پاسخ API را ندارد",
};

/** The Persian text of a diagnosis, or `null` for "other" (the caller keeps
 * its own message). *surface* is "admin" (the page has an address bar at
 * the top) or "analyst" (the address is deployment configuration). Literal
 * values (addresses, CORS_ALLOWED_ORIGINS) are the only non-Persian words;
 * the .env line stands on a line of its own so it can be copied whole. */
export function describeDiagnosis(diagnosis, surface = "admin") {
  if (!diagnosis) return null;
  const { baseUrl, pageOrigin } = diagnosis;

  if (diagnosis.kind === KIND_WRONG_TARGET) {
    const where = surface === "admin" ? "که در نوار بالای صفحه است" : "که این صفحه به آن وصل می‌شود";
    const first = diagnosis.why === "same_origin"
      ? `نشانیِ API ${where} (${baseUrl}) همان سرورِ خودِ این صفحه است (صفحه از ${pageOrigin} باز شده)، نه API.`
      : `نشانیِ API ${where} (${baseUrl}) به API نمی‌رسد: سرورِ آن پاسخ می‌دهد ولی API نیست `
        + `(${WHY_WRONG_TARGET[diagnosis.why] || WHY_WRONG_TARGET.shape}). صفحه از ${pageOrigin} باز شده است.`;
    const second = surface === "admin"
      ? "نشانیِ خودِ API را در همان نوار وارد کنید و «اتصال» را بزنید، مثلاً http://<host>:8000"
      : "نشانیِ خودِ API را (مثلاً http://<host>:8000) در نوار بالای پنل مدیریت وارد کنید — همان‌جا ذخیره می‌شود "
        + "و برای این صفحه هم اعمال می‌شود — یا در web/js/config.js (مقدار DEFAULT_BASE_URL) بگذارید.";
    return `${first}\n${second}`;
  }

  if (diagnosis.kind === KIND_CORS_OR_UNREACHABLE) {
    return [
      `اتصال به API در ${baseUrl} برقرار نشد. مرورگر علت را به صفحه نمی‌گوید؛ دو حالت محتمل است:`,
      `۱) سرور این صفحه را مجاز نکرده است: نشانیِ این صفحه (${pageOrigin}) در CORS_ALLOWED_ORIGINS سرور نیست. `
        + "آن را به همین فهرست در فایل .env سرور اضافه کنید (نشانی‌ها با ویرگول جدا می‌شوند) و سرور را ری‌استارت کنید؛ "
        + "یا صفحه را دقیقاً با یکی از نشانی‌های مجاز باز کنید (پروتکل، نام سرور و پورت باید هر سه یکی باشند):",
      `CORS_ALLOWED_ORIGINS=${pageOrigin}`,
      `۲) API بالا نیست: بررسی کنید API روی ${baseUrl} اجرا شده است.`,
    ].join("\n");
  }
  return null;
}

/** One diagnosis per round of calls, however many fail. Thirteen cards
 * failing together ask the same question ("is this address the API?"), so
 * the answer is computed once and shared until reset() — the next round,
 * or a change of address (the key includes the address). */
export function createDiagnoser({ getBaseUrl, getPageOrigin, fetchImpl, timeoutMs } = {}) {
  const memo = new Map();
  return {
    diagnose(err) {
      const failure = failureFromError(err);
      const baseUrl = getBaseUrl();
      const pageOrigin = (getPageOrigin && getPageOrigin()) || "";
      if (!failure || (failure.type === "http" && !PROBE_STATUSES.has(failure.status))) {
        return Promise.resolve({ kind: KIND_OTHER, baseUrl, pageOrigin, why: null });
      }
      const key = `${baseUrl}|${failure.type === "network" ? "network" : "probe"}`;
      if (!memo.has(key)) {
        memo.set(key, diagnoseApiFailure({ baseUrl, pageOrigin, failure, fetchImpl, timeoutMs }));
      }
      return memo.get(key);
    },
    reset() {
      memo.clear();
    },
  };
}

/** Shows *diagnosis* in the page-level banner *el*; hides it for "other".
 * Setting the same text again changes nothing, so a round of failures
 * leaves one banner, not one per card. Returns whether it is shown. */
export function showDiagnosisBanner(el, diagnosis, surface) {
  const text = describeDiagnosis(diagnosis, surface);
  if (!el) return false;
  if (!text) {
    el.hidden = true;
    return false;
  }
  if (el.textContent !== text) el.textContent = text;
  el.hidden = false;
  return true;
}

export function hideDiagnosisBanner(el) {
  if (el) el.hidden = true;
}
