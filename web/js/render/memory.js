/* web/js/render/memory.js — the memory panel (reachable from the topbar)
 * and the client-side mirror of the server's PUT /v2/memory/{key}
 * validation.
 *
 * Two lists, per the spec, and the distinction matters:
 *   - `entries`: what IS remembered (value + clear-one), including an
 *     `applicable: false` entry rendered visibly inactive with its reason
 *     — never hidden (the analyst would lose track of a stored preference)
 *     and never rendered as normal (it is not affecting their numbers).
 *   - `rememberable`: the closed set of fields that CAN be remembered, each
 *     with its own input (a <select> when `options` is non-empty, else a
 *     bounded text field) so an analyst can set a value directly from this
 *     panel, not only via an assumption chip's pin control.
 *
 * `validateMemoryValue` mirrors the server's rules (no newline, no control
 * character, within `max_length`, within `options` when non-empty) so a
 * bad value is refused before the request is made — but it is a
 * convenience only. The server's 422 message, when one comes back, is what
 * actually gets rendered (see main.js's onSetValue handler); this module
 * never invents its own wording to replace that.
 */

"use strict";

// Built from character codes rather than a regex literal with embedded
// control characters (C0 controls + DEL) — keeps the source file itself
// free of raw non-printable bytes, which a literal like /[\x00-\x1f]/
// written out longhand tends to accumulate under copy/paste.
const CONTROL_CHAR_RE = new RegExp(
  "[" +
    String.fromCharCode(0) + "-" + String.fromCharCode(8) +
    String.fromCharCode(11) +
    String.fromCharCode(12) +
    String.fromCharCode(14) + "-" + String.fromCharCode(31) +
    String.fromCharCode(127) +
  "]",
);

/**
 * @param {string} value
 * @param {import("../api.js").RememberableField} [rememberableEntry]
 * @returns {{ ok: boolean, error: string|null }}
 */
export function validateMemoryValue(value, rememberableEntry) {
  const v = value === null || value === undefined ? "" : String(value);

  if (v.length === 0) {
    return { ok: false, error: "مقدار نمی‌تواند خالی باشد." };
  }
  if (v.includes("\n") || v.includes("\r")) {
    return { ok: false, error: "مقدار نمی‌تواند شامل خط جدید باشد." };
  }
  if (CONTROL_CHAR_RE.test(v)) {
    return { ok: false, error: "مقدار نمی‌تواند شامل نویسهٔ کنترلی باشد." };
  }
  const maxLength = rememberableEntry && rememberableEntry.max_length;
  if (typeof maxLength === "number" && v.length > maxLength) {
    return { ok: false, error: `مقدار نباید بیش از ${maxLength.toLocaleString("fa-IR")} نویسه باشد.` };
  }
  const options = rememberableEntry && rememberableEntry.options;
  if (options && options.length > 0 && !options.includes(v)) {
    return { ok: false, error: "مقدار باید یکی از گزینه‌های مجاز باشد." };
  }
  return { ok: true, error: null };
}

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

function renderRememberedSection(entries, handlers) {
  const section = el("section", "memory-section");
  section.appendChild(el("h3", "memory-section-title", "آنچه به خاطر سپرده شده"));

  const list = entries || [];
  if (list.length === 0) {
    section.appendChild(el(
      "p", "memory-empty",
      "هنوز چیزی به خاطر سپرده نشده — از کنار هر مفروضهٔ قابل‌ویرایش، «📌 به خاطر بسپار» را بزنید، " +
      "یا یکی از فیلدهای زیر را مستقیماً تنظیم کنید.",
    ));
    return section;
  }

  for (const entry of list) {
    const isApplicable = entry.applicable !== false;
    const row = el("div", "memory-entry" + (isApplicable ? "" : " inactive"));
    row.dataset.applicable = String(isApplicable);
    row.dataset.key = entry.key;

    const main = el("div", "memory-entry-main");
    main.appendChild(el("span", "memory-entry-field", entry.field || entry.key));
    main.appendChild(el("span", "memory-entry-value", entry.value));
    row.appendChild(main);

    if (!isApplicable) {
      row.appendChild(el(
        "span", "memory-entry-inactive-note",
        "غیرفعال — ستونی که این مورد را محدود می‌کرد دیگر برای شما قابل‌مشاهده نیست. مقدار همچنان ذخیره است اما اعمال نمی‌شود.",
      ));
    }

    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "memory-clear-btn";
    clearBtn.textContent = "حذف";
    clearBtn.setAttribute("aria-label", `حذف مقدار به‌خاطرسپرده‌شدهٔ ${entry.field || entry.key}`);
    clearBtn.addEventListener("click", () => handlers.onClearOne && handlers.onClearOne(entry.key));
    row.appendChild(clearBtn);

    section.appendChild(row);
  }

  const clearAllBtn = document.createElement("button");
  clearAllBtn.type = "button";
  clearAllBtn.className = "memory-clear-all-btn";
  clearAllBtn.textContent = "پاک کردن همهٔ حافظه";
  clearAllBtn.addEventListener("click", () => {
    const confirmFn = handlers.confirmClearAll ||
      ((typeof window !== "undefined" && window.confirm)
        ? () => window.confirm("همهٔ مقادیر به‌خاطرسپرده‌شده برای همیشه حذف شوند؟")
        : () => true);
    if (confirmFn() && handlers.onClearAll) handlers.onClearAll();
  });
  section.appendChild(clearAllBtn);

  return section;
}

function renderRememberableSection(rememberable, handlers) {
  const section = el("section", "memory-section");
  section.appendChild(el("h3", "memory-section-title", "آنچه قابل‌به‌خاطرسپردن است"));

  const list = rememberable || [];
  if (list.length === 0) {
    section.appendChild(el("p", "memory-empty", "برای این استقرار هیچ فیلد قابل‌به‌خاطرسپردنی تعریف نشده است."));
    return section;
  }

  for (const r of list) {
    const row = el("div", "memory-rememberable-row");
    row.dataset.key = r.key;
    row.appendChild(el("span", "memory-rememberable-field", r.field || r.key));

    let control;
    if (r.options && r.options.length > 0) {
      control = document.createElement("select");
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "— انتخاب کنید —";
      control.appendChild(blank);
      for (const opt of r.options) {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = opt;
        control.appendChild(o);
      }
    } else {
      control = document.createElement("input");
      control.type = "text";
      if (r.max_length) control.maxLength = r.max_length;
    }
    control.className = "memory-rememberable-input";
    control.setAttribute("aria-label", `مقدار برای ${r.field || r.key}`);
    row.appendChild(control);

    const errorEl = el("span", "memory-rememberable-error");
    row.appendChild(errorEl);

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "memory-save-btn";
    saveBtn.textContent = "ذخیره";
    saveBtn.addEventListener("click", () => {
      const val = control.value;
      const check = validateMemoryValue(val, r);
      if (!check.ok) {
        errorEl.textContent = check.error;
        return;
      }
      errorEl.textContent = "";
      if (handlers.onSetValue) handlers.onSetValue(r.key, val, errorEl);
    });
    row.appendChild(saveBtn);

    section.appendChild(row);
  }
  return section;
}

/**
 * @param {{ entries: import("../api.js").MemoryEntry[], rememberable: import("../api.js").RememberableField[] }} memory
 * @param {{
 *   onSetValue?: (key: string, value: string, errorEl: HTMLElement) => void,
 *   onClearOne?: (key: string) => void,
 *   onClearAll?: () => void,
 *   confirmClearAll?: () => boolean,
 * }} [handlers]
 */
export function renderMemoryPanel(memory, handlers = {}) {
  const wrap = el("div", "memory-panel-body");
  wrap.appendChild(renderRememberedSection(memory && memory.entries, handlers));
  wrap.appendChild(renderRememberableSection(memory && memory.rememberable, handlers));
  return wrap;
}
