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
    memory: "حافظهٔ تحلیلی",
    health: "سلامت",
    healthStateUp: "سالم",
    healthStateDown: "قطع",
    healthStateUnknown: "نامشخص",
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
    accessRequests: "درخواست‌های دسترسی",
    accessRequestsEmpty: "درخواستی ثبت نشده است.",
    accessRequestStatusOpen: "در انتظار بررسی",
    accessRequestStatusApproved: "تأیید شد",
    accessRequestStatusDenied: "رد شد",
    sessions: "گفتگوها",
    newSession: "+ گفتگوی جدید",
    workspaceEyebrow: "پرسش تحلیلی",
    workspaceTitle: "گفتگوی تحلیلی",
    workspaceSub: "پرسش را به فارسی یا انگلیسی بنویسید؛ SQL تولیدشده پیش از اجرا اعتبارسنجی می‌شود.",
    liveApi: "زندهٔ API",
    themeQuick: "تغییر پوسته",
    askLabel: "پرسش خود را به زبان طبیعی بنویسید",
    askPlaceholder: "مثلاً «معاملات مشتری‌های تالار سیمان را نشان بده»",
    ask: "پرسش",
    interpret: "تفسیر نتیجه",
    samples: "داستان نمونه:",
    startExamples: "یا یکی از این‌ها را بپرسید:",
    transcriptEmptyTitle: "از داده‌ها بپرسید",
    transcriptEmptyHint: "برای شروع، پرسش خود را در پایین بنویسید.",
    transcript: "گفتگوی تحلیلی",
    memoryPanel: "حافظهٔ تحلیلی — اولویت‌های ثابت",
    memoryPanelTitle: "حافظهٔ تحلیلی",
    closeMemory: "بستن پنل حافظه",
    /* ── Dashboard shell ─────────────────────────────────────────────── */
    navDashboard: "داشبورد",
    navConversations: "گفتگوها",
    navNewQuery: "پرسش جدید",
    navAnalytics: "تحلیل و گزارش",
    navConnections: "اتصال‌های داده",
    railSettings: "تنظیمات و بخش‌ها",
    searchPlaceholder: "جست‌وجوی گفتگوها…",
    searchEmpty: "گفتگویی با این نام پیدا نشد.",
    systemStatus: "وضعیت سامانه",
    systemStatusOk: "وضعیت سامانه — همهٔ سرویس‌ها در دسترس",
    systemStatusAlert: "وضعیت سامانه — یک سرویس در دسترس نیست",
    close: "بستن",
    userRole: "تحلیل‌گر داده",
    welcomeEyebrow: "فضای کاری داده",
    analyticsTitle: "عملکرد پرسش‌ها",
    rangeLabel: "بازهٔ زمانی",
    range7: "۷ روز",
    range30: "۳۰ روز",
    rangeAll: "همه",
    kpiQueries: "کل پرسش‌ها",
    kpiSuccess: "نرخ موفقیت",
    kpiLatency: "میانگین زمان اجرا",
    kpiRows: "ردیف‌های بازگشتی",
    kpiConnections: "اتصال فعال",
    chartTotalLabel: "پرسش در بازه",
    chartFoot: "هر نقطه یک گفتگو است؛ بلندی آن شمار پرسش‌های همان گفتگو.",
    chartEmpty: "هنوز گفتگویی در این بازه ثبت نشده است.",
    chartEmptyHint: "اولین پرسش را بپرسید تا نمودار شکل بگیرد.",
    chartTipUnit: "پرسش",
    assistantTitle: "از داده‌های خود چه می‌دانید؟",
    recentTitle: "پرسش‌های اخیر",
    recentEmpty: "هنوز پرسشی در این گفتگو ثبت نشده است.",
    viewAll: "همه",
    activityTitle: "فعالیت اتصال‌ها",
    activityTag: "زنده",
    activityEmpty: "در حال خواندن وضعیت اتصال‌ها…",
    conversationTitle: "گفتگو",
    conversationSub: "پرسش، SQL، نتیجه و تفسیر — همه در یک جریان",
    conversationTurns: "پرسش‌های این گفتگو",
    serviceApi: "سرویس API",
    serviceLlm: "مدل زبانی",
    serviceDb: "اتصال دیتابیس",
    serviceDetail: "رصدشده از /health",
    workspaceConnection: "فضای کاری",
    modeLive: "زنده",
    modeSimulated: "نمایشی",
    untitledSession: "گفتگوی بی‌نام",
    untitledTurn: "پرسش بی‌نام",
    /* ── Conversation navigator (vertical dot timeline) ─────────────────── */
    nav: "ناوبر گفتگو",
    navTurnOk: "پاسخ داده شد",
    navTurnEmpty: "بدون ردیف",
    navTurnFailed: "ناموفق",
    navTurnRunning: "در حال اجرا",
    navTurnUnknown: "نامشخص",
    rowsUnit: "ردیف",
    failedTag: "ناموفق",
    themeSystemLabel: "پوسته: سیستم",
    themeLightLabel: "پوسته: روشن",
    themeDarkLabel: "پوسته: تیره",
    invalidBaseUrlNotice: "نشانی بک‌اند نامعتبر است و نادیده گرفته شد — باید یک نشانی http:// یا https:// معتبر باشد.",
  },
  en: {
    productTitle: "SQL Agent",
    productTagline: "Ask in natural language → SQL → answer, on your infrastructure",
    memory: "Memory",
    health: "Health",
    healthStateUp: "up",
    healthStateDown: "down",
    healthStateUnknown: "unknown",
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
    accessRequests: "Access requests",
    accessRequestsEmpty: "No requests yet.",
    accessRequestStatusOpen: "Pending",
    accessRequestStatusApproved: "Approved",
    accessRequestStatusDenied: "Denied",
    sessions: "Conversations",
    newSession: "+ New",
    workspaceEyebrow: "Analytical question",
    workspaceTitle: "Analysis conversation",
    workspaceSub: "Ask in Persian or English — the generated SQL is validated before it runs.",
    liveApi: "Live API",
    themeQuick: "Toggle theme",
    askLabel: "Ask a question in natural language",
    askPlaceholder: "e.g. “Top cement-ring customers by trade value”",
    ask: "Ask",
    interpret: "Interpret result",
    samples: "Sample stories:",
    startExamples: "Or ask one of these:",
    transcriptEmptyTitle: "Ask your data",
    transcriptEmptyHint: "Ask your question below to get started.",
    transcript: "Conversation",
    memoryPanel: "Analytical memory — standing preferences",
    memoryPanelTitle: "Analytical memory",
    closeMemory: "Close memory panel",
    /* ── Dashboard shell ─────────────────────────────────────────────── */
    navDashboard: "Dashboard",
    navConversations: "Conversations",
    navNewQuery: "New query",
    navAnalytics: "Analytics",
    navConnections: "Connections",
    railSettings: "Settings and sections",
    searchPlaceholder: "Search conversations…",
    searchEmpty: "No conversation matches that name.",
    systemStatus: "System status",
    systemStatusOk: "System status — all services reachable",
    systemStatusAlert: "System status — a service is unreachable",
    close: "Close",
    userRole: "Data analyst",
    welcomeEyebrow: "Data workspace",
    analyticsTitle: "Query performance",
    rangeLabel: "Time range",
    range7: "7 days",
    range30: "30 days",
    rangeAll: "All",
    kpiQueries: "Total queries",
    kpiSuccess: "Success rate",
    kpiLatency: "Avg. runtime",
    kpiRows: "Rows returned",
    kpiConnections: "Active connections",
    chartTotalLabel: "queries in range",
    chartFoot: "Each point is one conversation; its height is how many questions it holds.",
    chartEmpty: "No conversations in this range yet.",
    chartEmptyHint: "Ask the first question to plot the trend.",
    chartTipUnit: "queries",
    assistantTitle: "What would you like to know about your data?",
    recentTitle: "Recent queries",
    recentEmpty: "No questions in this conversation yet.",
    viewAll: "View all",
    activityTitle: "Connection activity",
    activityTag: "live",
    activityEmpty: "Reading connection state…",
    conversationTitle: "Conversation",
    conversationSub: "Question, SQL, result and interpretation — one stream",
    conversationTurns: "Questions in this conversation",
    serviceApi: "API service",
    serviceLlm: "Language model",
    serviceDb: "Database link",
    serviceDetail: "observed from /health",
    workspaceConnection: "Workspace",
    modeLive: "live",
    modeSimulated: "simulated",
    untitledSession: "Untitled conversation",
    untitledTurn: "Untitled question",
    /* ── Conversation navigator (vertical dot timeline) ─────────────────── */
    nav: "Conversation navigator",
    navTurnOk: "answered",
    navTurnEmpty: "no rows",
    navTurnFailed: "failed",
    navTurnRunning: "running",
    navTurnUnknown: "unknown",
    rowsUnit: "rows",
    failedTag: "failed",
    themeSystemLabel: "Theme: system",
    themeLightLabel: "Theme: light",
    themeDarkLabel: "Theme: dark",
    invalidBaseUrlNotice: "Invalid backend address — ignored. Must be a valid http:// or https:// address.",
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

  // The composer keeps `dir="auto"` so a Latin question typed into the
  // Persian UI still reads left-to-right. But `auto` on an EMPTY textarea
  // has no character to detect from and falls back to LTR, which left the
  // caret blinking at the wrong edge of a right-aligned Persian composer
  // until the first keystroke. An explicit starting direction gives it the
  // page's own edge; bidi still orders whatever the analyst types next.
  const composer = document.getElementById("question");
  if (composer) composer.dir = root.dir;

  // Same reason for the top-bar search box: it keeps `dir="auto"` in the
  // markup so a Latin conversation title still reads left-to-right, but an
  // EMPTY field has no character to detect from — the placeholder and the
  // caret then sat on the wrong edge of a right-aligned Persian toolbar.
  const search = document.getElementById("session-search");
  if (search) search.dir = root.dir;

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
