/* web/js/data.js
 *
 * Synthetic, privacy-safe scenario data for SIMULATED mode. Nothing here is
 * real customer, contract, or price data — names and figures are generated
 * deterministically so the demo is stable across reloads (same approach as
 * the single-shot demo's data.js, extended to the Turn shape from
 * docs/api-contract-v2.md).
 *
 * The exported SCENARIO is one continuous conversation (one session) built
 * to exercise every UI state called for in the brief:
 *   t1  fresh, unambiguous, 100-of-342 rows displayed (truncated)
 *   t2  refines t1 — session-inherited ring, §2 "policy" scope assumption,
 *       and a refinement_scan_cap warning (must be impossible to miss)
 *   t3  the SAME question asked without the "among those" cue — basis
 *       "fresh", is_ambiguous true, default-sourced assumptions + offers
 *   t4  guard.verdict "rejected" (a destructive statement got blocked)
 *   t5  LLM transport failure (attempts: 3, endpoint_status: 0)
 *   t6  retry of t5's question, succeeds, with one self-correction round
 *
 * t1 -> t2 also carries the required prefix-cache contrast: t1 is a cold
 * cache (large prefill_ms), t2 is warm (tiny prefill_ms) in the same
 * session, right next to each other in the transcript.
 */

"use strict";

/* ── Deterministic PRNG (mulberry32) — identical technique to the old demo,
 * so results never change between reloads. */
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffle(arr, rand) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

const PREFIXES = ["شرکت", "بازرگانی", "گروه صنعتی", "هلدینگ", "تعاونی تولیدی", "کارخانجات", "مجتمع تولیدی", "شرکت بازرگانی"];
const CORES = [
  "سیمان کرمان", "پارس بتن", "الوند", "آریا سیمان", "مهر بنا", "کیان سازه", "سیمان شرق", "سپاهان بنا",
  "نوین بنا", "توسعه معادن غرب", "سیمان دورود", "راه و ساختمان پویا", "صنایع ساختمانی البرز", "سیمان هگمتان",
  "بتن آذرخش", "سیمان فارس", "ساختمانی خزر", "سیمان ایلام", "صنایع معدنی زاگرس", "سیمان شاهرود",
  "بنای سبز", "سیمان کردستان", "صنایع سیمان جنوب", "ساختمانی امید", "سیمان تهران", "بتن‌ریز پارسیان",
  "سیمان بوشهر", "صنایع سنگ و سیمان", "ساختمانی ارگ", "سیمان یزد",
];

function buildCustomerPool() {
  const rand = mulberry32(342);
  const combos = [];
  for (const p of PREFIXES) for (const c of CORES) combos.push(`${p} ${c}`);
  return shuffle(combos, rand);
}
const CUSTOMER_POOL = buildCustomerPool();

/* ── Turn 1: cement-ring customer transactions (fresh, unambiguous) ──── */
const T1_TOTAL_MATCHED = 342;
const T1_DISPLAY_CAP = 100;

const t1Rows = (() => {
  const rand = mulberry32(1001);
  const rows = CUSTOMER_POOL.slice(0, T1_DISPLAY_CAP).map((name) => {
    const txCount = 3 + Math.floor(rand() * 40);
    const avgTicket = 8_000_000_000 + rand() * 55_000_000_000;
    const totalValue = Math.round((txCount * avgTicket) / 1e7) * 1e7;
    return { CustomerName: name, TransactionCount: txCount, TotalValue: totalValue };
  });
  rows.sort((a, b) => b.TotalValue - a.TotalValue);
  return rows;
})();

/* ── Turn 2: top 10 by traded volume, among ALL 342 matching rows (not
 * just the 100 shown in t1) — two customers here never appeared in t1's
 * displayed page at all, which is exactly why §2's "Reading A" (rank
 * within the displayed rows) would have been wrong. */
const t2Rows = (() => {
  const rand = mulberry32(2002);
  const names = [
    ...CUSTOMER_POOL.slice(0, 8),
    "بازرگانی سیمان جنوب‌شرق", // not among t1's top-100-by-value rows
    "شرکت تعاونی سیمان مرزی", // not among t1's top-100-by-value rows either
  ];
  const picks = shuffle(names, rand).slice(0, 10);
  const rows = picks.map((name) => ({
    CustomerName: name,
    TotalVolume: Math.round((1200 + rand() * 8600)) * 10, // metric tons
  }));
  rows.sort((a, b) => b.TotalVolume - a.TotalVolume);
  return rows;
})();
const T2_SCAN_CAP = 10000;
const T2_SCAN_MATCHED = 14382; // exceeds the cap -> triggers the §2 warning

/* ── Turn 3: same question, no session cue, asked fresh -> ambiguous ── */
const t3Rows = (() => {
  const rand = mulberry32(3003);
  const ringNames = ["تالار سیمان", "تالار فلزات", "تالار پتروشیمی", "تالار کشاورزی", "تالار صادراتی"];
  const names = shuffle(CUSTOMER_POOL.slice(40, 90), rand).slice(0, 10);
  const rows = names.map((name) => ({
    CustomerName: name,
    Ring: ringNames[Math.floor(rand() * ringNames.length)],
    TotalValue: Math.round((30_000_000_000 + rand() * 900_000_000_000) / 1e7) * 1e7,
  }));
  rows.sort((a, b) => b.TotalValue - a.TotalValue);
  return rows;
})();

/* ── Turn 6: retry of t5's question, succeeds ─────────────────────────── */
const MONTHS_FA = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"];
const t6Rows = (() => {
  const rand = mulberry32(6006);
  return MONTHS_FA.map((m) => ({
    Month: m,
    AvgWeightTons: Math.round((180 + rand() * 260) * 10) / 10,
  }));
})();

/* ── Shared LLM constants ─────────────────────────────────────────────
 * static_prefix_tokens: per docs/api-contract-v2.md §6, recorded per skill
 * version so prefix_cache_hit stays meaningful. Rule used below, matching
 * the contract exactly: prefix_cache_hit = prompt_tokens < prefix*0.5. */
const STATIC_PREFIX_TOKENS = 4600;
const MODEL_NAME = "qwen2.5-coder:7b-instruct-q4_K_M";

function llm({
  attempts = 1, endpointStatus = 200, finishReason = "stop", structuredOutput = true,
  promptTokens, completionTokens, prefillMs, decodeMs, corrections = 0,
}) {
  const totalMs = prefillMs + decodeMs;
  return {
    backend: "ollama",
    model: MODEL_NAME,
    endpoint_status: endpointStatus,
    attempts,
    finish_reason: finishReason,
    structured_output: structuredOutput,
    prompt_tokens: promptTokens,
    completion_tokens: completionTokens,
    prefill_ms: prefillMs,
    decode_ms: decodeMs,
    total_ms: totalMs,
    tokens_per_second: decodeMs > 0 ? Math.round((completionTokens / (decodeMs / 1000)) * 10) / 10 : 0,
    prefix_cache_hit: promptTokens < STATIC_PREFIX_TOKENS * 0.5,
    temperature: 0.0,
    seed: 7,
    corrections,
  };
}

/* ── The scenario ──────────────────────────────────────────────────── */
const SCENARIO = {
  session_id: "s_demo_1404",
  title: "گفتگوی نمونه — تحلیل معاملات تالار سیمان",
  turns: [
    {
      turn_id: "t_01",
      session_id: "s_demo_1404",
      index: 1,
      question: "معاملات مشتری‌های تالار سیمان را نشان بده",
      resolved_question: "فهرست مشتریان تالار سیمان به همراه تعداد و ارزش کل معاملات هرکدام، در تمام دوره‌های موجود",
      basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
      sql:
        "SELECT TOP 100 c.Name AS CustomerName,\n" +
        "       COUNT(*) AS TransactionCount,\n" +
        "       SUM(ct.TotalPrice) AS TotalValue\n" +
        "FROM [Auction_Fact].[CustomerContract] ct\n" +
        "JOIN [Auction_Dim].[Customer] c ON ct.BuyerCustomer_ID = c.ID\n" +
        "JOIN [Auction_Dim].[Ring] r ON ct.Ring_ID = r.ID\n" +
        "WHERE r.Name = N'تالار سیمان'\n" +
        "GROUP BY c.Name\n" +
        "ORDER BY TotalValue DESC",
      ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
      guard: { verdict: "allowed", rule: null, injected_top: 100, tables_touched: ["CustomerContract", "Customer", "Ring"] },
      result: {
        columns: [
          { name: "CustomerName", type: "string" },
          { name: "TransactionCount", type: "number" },
          { name: "TotalValue", type: "number" },
        ],
        rows: t1Rows,
        row_count: t1Rows.length,
        truncated: true,
      },
      interpretation:
        `این پرس‌وجو ${T1_TOTAL_MATCHED.toLocaleString("fa-IR")} مشتری فعال در تالار سیمان را شناسایی کرد؛ ` +
        `${T1_DISPLAY_CAP} مورد برتر بر اساس ارزش کل معامله در جدول زیر نمایش داده شده است. ` +
        `مشتری نخست بیش از دو برابر مشتری دهم ارزش معامله داشته است.`,
      tier: "T2",
      warnings: [],
      llm: llm({ promptTokens: 4680, completionTokens: 132, prefillMs: 2210, decodeMs: 430 }),
      timings: { total_ms: 2760, plan_ms: 4, prompt_ms: 12, llm_ms: 2640, guard_ms: 5, execute_ms: 95, interpret_ms: 4 },
      error: null,
    },

    {
      turn_id: "t_02",
      session_id: "s_demo_1404",
      index: 2,
      question: "از بین آن‌ها ۱۰ مشتری برتر به لحاظ حجم معامله",
      resolved_question: "برای معاملات تالار سیمان، ۱۰ مشتری با بیشترین حجم معامله (بر حسب تن) در میان همهٔ ۳۴۲ مشتری منطبق — نه فقط ۱۰۰ ردیف نمایش‌داده‌شده در پرسش قبل",
      basis: {
        kind: "refines", refines_turn_id: "t_01", composition: "cte",
        inherited: ["ring=تالار سیمان"],
      },
      sql:
        "WITH _prev AS (\n" +
        "    SELECT c.Name AS CustomerName, ct.HallMatchingWeight AS Volume\n" +
        "    FROM [Auction_Fact].[CustomerContract] ct\n" +
        "    JOIN [Auction_Dim].[Customer] c ON ct.BuyerCustomer_ID = c.ID\n" +
        "    JOIN [Auction_Dim].[Ring] r ON ct.Ring_ID = r.ID\n" +
        "    WHERE r.Name = N'تالار سیمان'\n" +
        ")\n" +
        "SELECT TOP 10 CustomerName, SUM(Volume) AS TotalVolume\n" +
        "FROM _prev\n" +
        "GROUP BY CustomerName\n" +
        "ORDER BY TotalVolume DESC",
      ambiguity: {
        is_ambiguous: true,
        assumptions: [
          { field: "measure", value: "حجم معامله (HallMatchingWeight)", source: "question", editable: true },
          { field: "scope", value: "همهٔ ۳۴۲ سطر منطبق با فیلتر قبلی، نه فقط ۱۰۰ سطر نمایش‌داده‌شدهٔ پرسش قبل", source: "policy", editable: false },
          { field: "ring", value: "تالار سیمان", source: "session", editable: true },
        ],
        clarifications: [],
      },
      guard: { verdict: "allowed", rule: null, injected_top: 10, tables_touched: ["CustomerContract", "Customer", "Ring"] },
      result: {
        columns: [
          { name: "CustomerName", type: "string" },
          { name: "TotalVolume", type: "number" },
        ],
        rows: t2Rows,
        row_count: t2Rows.length,
        truncated: false,
      },
      interpretation:
        "۱۰ مشتری برتر تالار سیمان بر اساس حجم معامله (تن) محاسبه شد. دو مورد از این ده مشتری در ۱۰۰ ردیف " +
        "نمایش‌داده‌شدهٔ پرسش قبلی (که بر اساس ارزش ریالی مرتب شده بود) اصلاً دیده نمی‌شدند — دقیقاً همان ریسکی " +
        "که محاسبه بر پایهٔ سطرهای نمایشی به‌جای کل دادهٔ منطبق ایجاد می‌کند.",
      tier: "T2",
      warnings: [
        "اسکن پایهٔ این بازپالایش به دلیل محدودیت ایمنی (refinement_scan_cap = ۱۰٬۰۰۰ ردیف) متوقف شد؛ " +
        "تعداد واقعی سطرهای منطبق ۱۴٬۳۸۲ بود. ۱۰ مشتری برتر واقعی ممکن است با نتیجهٔ زیر متفاوت باشد.",
      ],
      llm: llm({ promptTokens: 512, completionTokens: 96, prefillMs: 42, decodeMs: 610 }),
      timings: { total_ms: 745, plan_ms: 3, prompt_ms: 5, llm_ms: 652, guard_ms: 4, execute_ms: 78, interpret_ms: 3 },
      error: null,
    },

    {
      turn_id: "t_03",
      session_id: "s_demo_1404",
      index: 3,
      question: "۱۰ مشتری برتر را نشان بده",
      resolved_question: "۱۰ مشتری برتر از میان همهٔ تالارها، بر اساس ارزش ریالی معامله، در سال جاری (۱۴۰۴)",
      basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
      sql:
        "SELECT TOP 10 c.Name AS CustomerName, r.Name AS Ring,\n" +
        "       SUM(ct.TotalPrice) AS TotalValue\n" +
        "FROM [Auction_Fact].[CustomerContract] ct\n" +
        "JOIN [Auction_Dim].[Customer] c ON ct.BuyerCustomer_ID = c.ID\n" +
        "JOIN [Auction_Dim].[Ring] r ON ct.Ring_ID = r.ID\n" +
        "JOIN [Auction_Dim].[Date] d ON ct.Date_ID = d.ID\n" +
        "WHERE d.PersianYear = 1404\n" +
        "GROUP BY c.Name, r.Name\n" +
        "ORDER BY TotalValue DESC",
      ambiguity: {
        is_ambiguous: true,
        assumptions: [
          { field: "measure", value: "ارزش ریالی معامله", source: "default", editable: true },
          { field: "period", value: "سال جاری (۱۴۰۴)", source: "default", editable: true },
          { field: "ring", value: "همهٔ تالارها", source: "default", editable: true },
        ],
        clarifications: [
          {
            field: "measure",
            prompt: "«برتر» بر اساس کدام معیار؟",
            options: ["ارزش ریالی معامله", "حجم معامله", "تعداد قرارداد"],
          },
          {
            field: "ring",
            prompt: "منظور کدام تالار است؟",
            options: ["همهٔ تالارها", "تالار سیمان", "تالار فلزات", "تالار پتروشیمی"],
          },
        ],
      },
      guard: { verdict: "allowed", rule: null, injected_top: 10, tables_touched: ["CustomerContract", "Customer", "Ring", "Date"] },
      result: {
        columns: [
          { name: "CustomerName", type: "string" },
          { name: "Ring", type: "string" },
          { name: "TotalValue", type: "number" },
        ],
        rows: t3Rows,
        row_count: t3Rows.length,
        truncated: false,
      },
      interpretation:
        "این پرسش هیچ اشاره‌ای به «آن‌ها» یا زمینهٔ پرسش قبلی نداشت، بنابراین به‌جای وراثت از نشست، با مفروضات " +
        "پیش‌فرض (سیستم‌سطح) پاسخ داده شد: ارزش ریالی، سال جاری، همهٔ تالارها. اگر منظور شما همان زمینهٔ " +
        "پرسش‌های قبلی (تالار سیمان) بود، از پیشنهادهای زیر یا ویرایش مفروضات استفاده کنید.",
      tier: "T2",
      warnings: [],
      llm: llm({ promptTokens: 498, completionTokens: 210, prefillMs: 35, decodeMs: 580 }),
      timings: { total_ms: 668, plan_ms: 4, prompt_ms: 6, llm_ms: 615, guard_ms: 4, execute_ms: 41, interpret_ms: 2 },
      error: null,
    },

    {
      turn_id: "t_04",
      session_id: "s_demo_1404",
      index: 4,
      question: "جدول مشتریان تالار سیمان رو حذف کن و رکوردهای امسال رو پاک کن",
      resolved_question: "درخواست حذف داده از جدول CustomerContract (عملیات نوشتنی)",
      basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
      sql:
        "DELETE FROM [Auction_Fact].[CustomerContract]\n" +
        "WHERE Ring_ID = (SELECT ID FROM [Auction_Dim].[Ring] WHERE Name = N'تالار سیمان')",
      ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
      guard: {
        verdict: "rejected",
        rule: "readonly-only: فقط عبارت‌های SELECT مجاز است؛ DELETE/UPDATE/INSERT/DDL مسدود می‌شوند.",
        injected_top: null,
        tables_touched: ["CustomerContract"],
      },
      result: { columns: [], rows: [], row_count: 0, truncated: false },
      interpretation: null,
      tier: "T2",
      warnings: [
        "پرس‌وجوی تولیدشده شامل دستور تغییردهندهٔ داده (DELETE) بود و پیش از اجرا توسط لایهٔ نگهبانی امنیتی رد شد. هیچ داده‌ای تغییر نکرد.",
      ],
      llm: llm({ promptTokens: 540, completionTokens: 140, prefillMs: 40, decodeMs: 520 }),
      timings: { total_ms: 566, plan_ms: 3, prompt_ms: 5, llm_ms: 560, guard_ms: 6, execute_ms: 0, interpret_ms: 0 },
      error: null,
    },

    {
      turn_id: "t_05",
      session_id: "s_demo_1404",
      index: 5,
      question: "میانگین وزن معامله‌شده در تالار فلزات به تفکیک ماه در سال ۱۴۰۴ چقدر است؟",
      resolved_question: null,
      basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
      sql: null,
      ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
      guard: null,
      result: null,
      interpretation: null,
      tier: null,
      warnings: [],
      llm: llm({ attempts: 3, endpointStatus: 0, finishReason: "error", structuredOutput: false, promptTokens: 0, completionTokens: 0, prefillMs: 0, decodeMs: 0 }),
      timings: { total_ms: 9840, plan_ms: 4, prompt_ms: 6, llm_ms: 9800, guard_ms: 0, execute_ms: 0, interpret_ms: 0 },
      error: { code: "LLM_UNAVAILABLE", message: "سرویس مدل زبانی محلی پس از ۳ تلاش پاسخ نداد (HTTP status 0 — اتصال برقرار نشد)." },
    },

    {
      turn_id: "t_06",
      session_id: "s_demo_1404",
      index: 6,
      question: "همون سوال قبلی رو دوباره امتحان کن",
      resolved_question: "میانگین وزن معامله‌شده (تن) در تالار فلزات، به تفکیک ماه، در سال ۱۴۰۴",
      basis: { kind: "refines", refines_turn_id: "t_05", composition: "none", inherited: ["ring=تالار فلزات", "period=1404"] },
      sql:
        "SELECT d.PersianMonthName AS Month,\n" +
        "       AVG(ct.HallMatchingWeight) AS AvgWeightTons\n" +
        "FROM [Auction_Fact].[CustomerContract] ct\n" +
        "JOIN [Auction_Dim].[Ring] r ON ct.Ring_ID = r.ID\n" +
        "JOIN [Auction_Dim].[Date] d ON ct.Date_ID = d.ID\n" +
        "WHERE r.Name = N'تالار فلزات' AND d.PersianYear = 1404\n" +
        "GROUP BY d.PersianMonthName, d.PersianMonthNumber\n" +
        "ORDER BY d.PersianMonthNumber",
      ambiguity: {
        is_ambiguous: true,
        assumptions: [
          { field: "ring", value: "تالار فلزات", source: "session", editable: true },
          { field: "period", value: "۱۴۰۴", source: "session", editable: true },
        ],
        clarifications: [],
      },
      guard: { verdict: "allowed", rule: null, injected_top: null, tables_touched: ["CustomerContract", "Ring", "Date"] },
      result: {
        columns: [
          { name: "Month", type: "string" },
          { name: "AvgWeightTons", type: "number" },
        ],
        rows: t6Rows,
        row_count: t6Rows.length,
        truncated: false,
      },
      interpretation:
        "میانگین وزن معاملات تالار فلزات در سال ۱۴۰۴ در بازهٔ ۱۸۰ تا ۴۴۰ تن نوسان داشت، با اوج در نیمهٔ دوم سال. " +
        "یک دور خودتصحیحی برای هم‌ترازی نام ستون AvgWeightTons با نوع دادهٔ عددی طی تولید SQL انجام شد.",
      tier: "T2",
      warnings: [],
      llm: llm({ promptTokens: 560, completionTokens: 260, prefillMs: 44, decodeMs: 900, corrections: 1 }),
      timings: { total_ms: 990, plan_ms: 4, prompt_ms: 6, llm_ms: 944, guard_ms: 5, execute_ms: 32, interpret_ms: 3 },
      error: null,
    },
  ],
};

/* Keyword hints used by SIMULATED mode to match free-typed questions back
 * to a scripted turn (best effort only — see main.js). Kept separate from
 * the Turn objects themselves since it's a demo-mode-only concern, not
 * part of the v2 contract shape. */
const SCENARIO_MATCH_HINTS = {
  t_01: ["تالار سیمان", "معاملات مشتری", "سیمان"],
  t_02: ["از بین آن", "۱۰ مشتری برتر", "حجم معامله", "برتر به لحاظ حجم"],
  t_03: ["۱۰ مشتری برتر را نشان بده", "مشتری برتر"],
  t_04: ["حذف کن", "پاک کن", "delete", "drop"],
  t_05: ["میانگین وزن", "تفکیک ماه", "تالار فلزات"],
  t_06: ["دوباره امتحان کن", "سوال قبلی"],
};

export { SCENARIO, SCENARIO_MATCH_HINTS, STATIC_PREFIX_TOKENS };
