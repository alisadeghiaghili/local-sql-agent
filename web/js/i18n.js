/* web/js/i18n.js — product chrome strings (fa default, en secondary).
 *
 * The engine accepts Persian and English questions either way; this file
 * only covers the shell the analyst clicks. Full transcript copy stays
 * Persian-first until a dedicated pass translates render/*.
 *
 * Usage:
 *   import { t, applyLang, getLang, setLang } from "./i18n.js";
 *   applyLang("en");
 *   t("apiKey") → "API key"
 *
 * Markup: put data-i18n="key" on an element; applyLang fills textContent.
 * Placeholders use data-i18n-attr="placeholder:askPlaceholder".
 */

"use strict";

const STORAGE_LANG_KEY = "lsa-web-lang";

/** @type {Record<"fa"|"en", Record<string, string>>} */
const STRINGS = {
  fa: {
    productTitle: "عامل هوشمند SQL",
    productTagline: "پرسش به زبان فارسی ← SQL ← پاسخ، روی زیرساخت خودتان",
    modeGroup: "حالت اجرا",
    modeSimulated: "نمایشی",
    modeLive: "زندهٔ API",
    memory: "حافظهٔ تحلیلی",
    health: "سلامت",
    userMenu: "حساب",
    theme: "پوسته",
    themeSystem: "سیستم",
    themeLight: "روشن",
    themeDark: "تیره",
    language: "زبان",
    langFa: "فارسی",
    langEn: "English",
    apiKey: "کلید API",
    apiKeyPlaceholder: "کلید API خود را وارد کنید",
    apiKeySave: "ذخیره",
    apiKeyChange: "تغییر کلید",
    apiKeyClear: "حذف کلید",
    apiKeySaved: "کلید: ذخیره شده",
    apiKeyUnset: "کلید: تنظیم نشده",
    apiKeyRejected: "کلید: رد شد",
    apiKeyStoredNotice: "کلید API ذخیره شد — این کلید فقط در همین مرورگر نگه‌داری می‌شود.",
    apiKeyClearedNotice: "کلید API حذف شد. برای پرسیدن سؤال در حالت زندهٔ API باید دوباره یک کلید وارد کنید.",
    sessions: "گفتگوها",
    newSession: "+ گفتگوی جدید",
    askLabel: "پرسش خود را به زبان طبیعی بنویسید",
    askPlaceholder: "مثلاً «معاملات مشتری‌های تالار سیمان را نشان بده»",
    ask: "پرسش",
    interpret: "تفسیر نتیجه",
    interpretHint: "خلاصهٔ ساده‌شده، با فرستادن حداکثر ۲۰ ردیف نتیجه به مدل زبانی",
    samples: "داستان نمونه:",
    transcriptEmptyHint: "برای شروع، پرسش خود را در پایین بنویسید.",
    transcript: "گفتگوی تحلیلی",
    memoryPanel: "حافظهٔ تحلیلی — اولویت‌های ثابت",
    closeMemory: "بستن پنل حافظه",
    footLiveChecking: "حالت زندهٔ API — در حال بررسی اتصال به بک‌اند...",
    themeSystemLabel: "پوسته: سیستم",
    themeLightLabel: "پوسته: روشن",
    themeDarkLabel: "پوسته: تیره",
  },
  en: {
    productTitle: "SQL Agent",
    productTagline: "Ask in natural language → SQL → answer, on your infrastructure",
    modeGroup: "Run mode",
    modeSimulated: "Demo",
    modeLive: "Live API",
    memory: "Memory",
    health: "Health",
    userMenu: "Account",
    theme: "Theme",
    themeSystem: "System",
    themeLight: "Light",
    themeDark: "Dark",
    language: "Language",
    langFa: "فارسی",
    langEn: "English",
    apiKey: "API key",
    apiKeyPlaceholder: "Enter your API key",
    apiKeySave: "Save",
    apiKeyChange: "Change key",
    apiKeyClear: "Remove key",
    apiKeySaved: "Key: saved",
    apiKeyUnset: "Key: not set",
    apiKeyRejected: "Key: rejected",
    apiKeyStoredNotice: "API key saved — it stays only in this browser.",
    apiKeyClearedNotice: "API key removed. Enter a key again to ask live questions.",
    sessions: "Conversations",
    newSession: "+ New",
    askLabel: "Ask a question in natural language",
    askPlaceholder: "e.g. “Top cement-ring customers by trade value”",
    ask: "Ask",
    interpret: "Interpret result",
    interpretHint: "Plain-language summary; up to 20 result rows go to the model",
    samples: "Sample stories:",
    transcriptEmptyHint: "Ask your question below to get started.",
    transcript: "Conversation",
    memoryPanel: "Analytical memory — standing preferences",
    closeMemory: "Close memory panel",
    footLiveChecking: "Live API — checking backend…",
    themeSystemLabel: "Theme: system",
    themeLightLabel: "Theme: light",
    themeDarkLabel: "Theme: dark",
  },
};

let current = "fa";

/**
 * @returns {"fa"|"en"}
 */
export function getLang() {
  return current;
}

/**
 * @param {string} key
 * @returns {string}
 */
export function t(key) {
  const table = STRINGS[current] || STRINGS.fa;
  return table[key] ?? STRINGS.fa[key] ?? key;
}

/**
 * Persist and apply chrome language (html lang/dir + [data-i18n]).
 *
 * @param {"fa"|"en"} lang
 * @returns {void}
 */
export function setLang(lang) {
  current = lang === "en" ? "en" : "fa";
  try {
    localStorage.setItem(STORAGE_LANG_KEY, current);
  } catch { /* private mode — apply for this session only */ }
  applyLang(current);
}

/**
 * Read persisted language, default fa.
 *
 * @returns {"fa"|"en"}
 */
export function loadLang() {
  try {
    const raw = localStorage.getItem(STORAGE_LANG_KEY);
    if (raw === "en" || raw === "fa") current = raw;
  } catch { /* ignore */ }
  return current;
}

/**
 * Set document direction and fill data-i18n nodes.
 *
 * @param {"fa"|"en"} [lang]
 * @returns {void}
 */
export function applyLang(lang) {
  if (lang) current = lang === "en" ? "en" : "fa";
  const root = document.documentElement;
  root.lang = current;
  root.dir = current === "fa" ? "rtl" : "ltr";

  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    if (key) el.textContent = t(key);
  });
  document.querySelectorAll("[data-i18n-attr]").forEach((el) => {
    // "placeholder:key" or "aria-label:key"; comma-separated for several
    const spec = el.getAttribute("data-i18n-attr") || "";
    spec.split(",").forEach((part) => {
      const sep = part.indexOf(":");
      if (sep < 1) return;
      const attr = part.slice(0, sep).trim();
      const key = part.slice(sep + 1).trim();
      if (attr && key) el.setAttribute(attr, t(key));
    });
  });
}
