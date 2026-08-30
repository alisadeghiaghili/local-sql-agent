/* web/js/render/llm-status.js — the LLM status strip (contract §6).
 *
 * Design intent (see AGENTS brief): encode state in form as well as number.
 * finish_reason/endpoint_status get a coloured pill so a schema_violation or
 * a 503 reads at a glance; attempts > 1 is flagged as a retry; the
 * prefill/decode split renders as a proportional bar, not just two numbers;
 * prefix_cache_hit gets its own obvious badge. On error the block still
 * renders whatever fields it has — a 3-attempt transport failure
 * (endpoint_status 0) must look visibly different from a 200 +
 * schema_violation.
 */

"use strict";

function statusPillForFinishReason(reason) {
  switch (reason) {
    case "stop": return { cls: "good", label: "پایان طبیعی (stop)" };
    case "length": return { cls: "warn", label: "قطع به‌دلیل محدودیت طول (length)" };
    case "schema_violation": return { cls: "critical", label: "نقض ساختار خروجی (schema_violation)" };
    case "error": return { cls: "critical", label: "خطا (error)" };
    default: return { cls: "neutral", label: reason ? String(reason) : "نامشخص" };
  }
}

function statusPillForEndpoint(status) {
  if (status === 200) return { cls: "good", label: "HTTP 200" };
  if (status === 0) return { cls: "critical", label: "بدون پاسخ (HTTP 0)" };
  if (status >= 500) return { cls: "critical", label: `HTTP ${status}` };
  if (status >= 400) return { cls: "warn", label: `HTTP ${status}` };
  return { cls: "neutral", label: `HTTP ${status}` };
}

function pillEl(cls, label) {
  const span = document.createElement("span");
  span.className = `status-pill ${cls}`;
  span.innerHTML = `<span class="dot"></span>`;
  span.append(label);
  return span;
}

function statLabelValue(label, value) {
  const box = document.createElement("div");
  const l = document.createElement("div");
  l.className = "llm-stat-label";
  l.textContent = label;
  const v = document.createElement("div");
  v.className = "llm-stat-value";
  v.textContent = value;
  box.appendChild(l);
  box.appendChild(v);
  return box;
}

/** @param {import("../api.js").LlmStatus|null|undefined} llm */
export function renderLlmStatus(llm) {
  const strip = document.createElement("div");
  strip.className = "llm-strip";

  const head = document.createElement("div");
  head.className = "llm-strip-head";
  head.tabIndex = 0;
  head.setAttribute("role", "button");
  head.setAttribute("aria-expanded", "false");

  const title = document.createElement("span");
  title.className = "llm-title";
  title.textContent = "وضعیت مدل زبانی";
  head.appendChild(title);

  if (!llm) {
    const none = document.createElement("span");
    none.className = "status-pill neutral";
    none.textContent = "بدون داده";
    head.appendChild(none);
    strip.appendChild(head);
    return strip;
  }

  const endpointInfo = statusPillForEndpoint(llm.endpoint_status);
  head.appendChild(pillEl(endpointInfo.cls, endpointInfo.label));

  const finishInfo = statusPillForFinishReason(llm.finish_reason);
  head.appendChild(pillEl(finishInfo.cls, finishInfo.label));

  if (llm.attempts > 1) {
    const flag = document.createElement("span");
    flag.className = "attempts-flag";
    flag.textContent = `${llm.attempts.toLocaleString("fa-IR")} تلاش (retry)`;
    head.appendChild(flag);
  }

  // Only meaningful once a prompt actually reached the model — on a total
  // transport failure (0 prompt tokens, nothing ever sent) the derived
  // prefix_cache_hit formula still evaluates true (0 < threshold), which
  // would render a green "cache HIT" badge next to a "no response" pill.
  // That is misleading, not merely unhelpful, so it's suppressed here.
  if (llm.prompt_tokens > 0) {
    const cacheBadge = document.createElement("span");
    cacheBadge.className = `cache-badge ${llm.prefix_cache_hit ? "hit" : "miss"}`;
    cacheBadge.textContent = llm.prefix_cache_hit ? "⚡ prefix cache HIT" : "prefix cache MISS";
    head.appendChild(cacheBadge);
  }

  if (llm.tokens_per_second) {
    const tps = document.createElement("span");
    tps.className = "meta-tag";
    tps.dir = "ltr";
    tps.textContent = `${llm.tokens_per_second} tok/s`;
    head.appendChild(tps);
  }

  const toggleIcon = document.createElement("span");
  toggleIcon.className = "llm-toggle-icon";
  toggleIcon.setAttribute("aria-hidden", "true");
  toggleIcon.textContent = "▾";
  head.appendChild(toggleIcon);

  const body = document.createElement("div");
  body.className = "llm-strip-body";

  // Prefill / decode proportional bar — the whole point of this block.
  if (llm.prefill_ms > 0 || llm.decode_ms > 0) {
    const total = Math.max(1, llm.prefill_ms + llm.decode_ms);
    const prefillPct = Math.round((llm.prefill_ms / total) * 1000) / 10;
    const decodePct = Math.round((llm.decode_ms / total) * 1000) / 10;

    const barRow = document.createElement("div");
    barRow.className = "prefill-bar-row";
    const label = document.createElement("div");
    label.className = "prefill-bar-label";
    label.innerHTML = `<span>prefill ${llm.prefill_ms} ms</span><span>decode ${llm.decode_ms} ms</span>`;
    barRow.appendChild(label);

    const bar = document.createElement("div");
    bar.className = "prefill-bar";
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", `prefill ${prefillPct}%, decode ${decodePct}%`);
    const prefillSeg = document.createElement("div");
    prefillSeg.className = "prefill-bar-prefill";
    prefillSeg.style.width = `${prefillPct}%`;
    const decodeSeg = document.createElement("div");
    decodeSeg.className = "prefill-bar-decode";
    decodeSeg.style.width = `${decodePct}%`;
    bar.appendChild(prefillSeg);
    bar.appendChild(decodeSeg);
    barRow.appendChild(bar);

    const legend = document.createElement("div");
    legend.className = "prefill-legend";
    legend.innerHTML =
      `<span><span class="legend-swatch prefill"></span>prefill (prompt eval)</span>` +
      `<span><span class="legend-swatch decode"></span>decode (generation)</span>`;
    barRow.appendChild(legend);

    body.appendChild(barRow);
  } else {
    const note = document.createElement("div");
    note.className = "llm-empty-note";
    note.textContent = "هیچ توکنی پردازش نشد — درخواست پیش از رسیدن به مرحلهٔ استنتاج شکست خورد.";
    body.appendChild(note);
  }

  const grid = document.createElement("div");
  grid.className = "llm-grid";
  grid.appendChild(statLabelValue("model", llm.model || "—"));
  grid.appendChild(statLabelValue("backend", llm.backend || "—"));
  grid.appendChild(statLabelValue("prompt tokens", llm.prompt_tokens.toLocaleString("en-US")));
  grid.appendChild(statLabelValue("completion tokens", llm.completion_tokens.toLocaleString("en-US")));
  grid.appendChild(statLabelValue("tokens/sec", String(llm.tokens_per_second)));
  grid.appendChild(statLabelValue("total ms", llm.total_ms.toLocaleString("en-US")));
  grid.appendChild(statLabelValue("temperature", String(llm.temperature)));
  grid.appendChild(statLabelValue("seed", String(llm.seed)));
  grid.appendChild(statLabelValue("structured output", llm.structured_output ? "بله" : "خیر"));
  grid.appendChild(statLabelValue("corrections", llm.corrections.toLocaleString("fa-IR")));
  grid.appendChild(statLabelValue("attempts", llm.attempts.toLocaleString("fa-IR")));
  grid.appendChild(statLabelValue("endpoint status", String(llm.endpoint_status)));
  body.appendChild(grid);

  strip.appendChild(head);
  strip.appendChild(body);

  const toggle = () => {
    const expanded = strip.classList.toggle("expanded");
    head.setAttribute("aria-expanded", String(expanded));
  };
  head.addEventListener("click", toggle);
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  });

  return strip;
}
