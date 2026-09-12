// tests/web_ui/run_chart_form_choice.mjs
//
// Node-side half of tests/web_ui/test_web_ui_chart_form_choice.py.
//
// Drives the REAL web/js/render/chart.js (via a caller-supplied path to a
// copy whose internal import specifiers were rewritten to .mjs -- see the
// Python half for why the rename is necessary) and asserts the one thing
// nothing in this project checked: that the CHART TYPE follows the data's
// job.
//
// Why this file exists
// --------------------
// A real result -- ten customers ranked by traded volume -- rendered as a
// LINE chart with company names along the x axis, under the headline
// "مقدار ... رو به کاهش بود" ("the amount was declining"). Nothing was
// declining. The rows were sorted by value, so a ranking drew the shape of
// a downward trend, and the headline then described that shape as if time
// had passed.
//
// The cause is in chooseFramings(): it offers a "line" framing first and
// unconditionally, and its own stated reason is
//
//     "سنجه در طول یک توالی است"   ("the measure is along a sequence")
//
// -- an assertion the function never tests. Company names are not a
// sequence. A line between them asserts continuity and order that the data
// does not have, which is the single most consequential chart mistake
// available: it does not mislabel the answer, it invents a different one.
//
// The same unchecked assumption produces the "نیمهٔ دوم/اول" (second half
// vs first half) figure. Halves are a property of an ordered series. Ten
// customers have no first half.
//
// What counts as a sequence
// -------------------------
// The label column's declared type, plus its name, are the only signals
// available client-side -- session/models.py ships `columns[].type` from
// session/engine.py::_infer_type's frozen vocabulary
// ("number" | "string" | "boolean" | "datetime"). A "datetime" label is a
// sequence. A named date dimension (Date / Month / Year / سال / ماه /
// تاریخ, per project_config's Date dimension) is a sequence. A customer,
// broker, ring or commodity name is not, and "string" alone must never be
// read as one -- that is exactly the assumption that produced the bug.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const chartMjsPath = process.argv[2];
if (!chartMjsPath) {
  console.error("usage: node run_chart_form_choice.mjs <chart.mjs path>");
  process.exit(2);
}

const { chooseFramings, chooseFocus } = await import(pathToFileURL(chartMjsPath).href);

const kinds = (framings) => framings.filter((f) => !f.rejected).map((f) => f.kind);

/* ── 1. Categorical labels must not be offered as a line ───────────── */
{
  // The exact shape of the reported defect: a ranking, already sorted
  // descending by the measure, which is what made it *look* like a trend.
  const ranking = [
    { CustomerName: "گروه صنعتی سیمان یزد", TotalVolume: 95350 },
    { CustomerName: "گروه صنعتی ساختمانی خزر", TotalVolume: 81200 },
    { CustomerName: "کارخانجات سیمان شاهرود", TotalVolume: 40900 },
    { CustomerName: "سیمان تهران", TotalVolume: 33100 },
    { CustomerName: "سیمان آبیک", TotalVolume: 21400 },
  ];
  const offered = kinds(chooseFramings(ranking, "CustomerName", "TotalVolume"));

  assert.ok(
    !offered.includes("line"),
    `a line framing was offered for categorical labels (customer names): ${JSON.stringify(offered)}. ` +
    "A line asserts order and continuity between adjacent points. Company " +
    "names have neither, and because the rows arrive sorted by value the " +
    "line draws a trend that does not exist",
  );
  assert.ok(
    offered.includes("bar"),
    "a ranking must still be offered as a bar chart -- rank is what this data is about",
  );
  assert.ok(
    !offered.includes("split-bar"),
    "the first-half/second-half framing was offered for categorical labels. " +
    "Halves are a property of an ordered series; ten customers have no first half",
  );
  console.log("[ok] categorical labels are not offered as a line or a half-vs-half split");
}

/* ── 2. A real sequence still gets its line ────────────────────────── */
{
  // Guarding the fix against over-correction: removing the line entirely
  // would be as wrong as offering it always.
  const monthly = [
    { Month: "1403/01", Volume: 10 },
    { Month: "1403/02", Volume: 22 },
    { Month: "1403/03", Volume: 31 },
    { Month: "1403/04", Volume: 44 },
    { Month: "1403/05", Volume: 58 },
  ];
  const offered = kinds(chooseFramings(monthly, "Month", "Volume"));
  assert.ok(
    offered.includes("line"),
    `a named time dimension ("Month") lost its line framing: ${JSON.stringify(offered)}. ` +
    "A sequence is exactly where a line belongs",
  );
  console.log("[ok] a named time dimension still gets a line framing");
}

/* ── 3. A declared datetime column is a sequence whatever it is called ─ */
{
  const rows = [
    { when: "2026-01-01", v: 5 },
    { when: "2026-02-01", v: 9 },
    { when: "2026-03-01", v: 14 },
  ];
  const offered = kinds(chooseFramings(rows, "when", "v", { labelType: "datetime" }));
  assert.ok(
    offered.includes("line"),
    "a column declared type=datetime must count as a sequence even when its " +
    "name matches no known date word -- the declared type is the stronger signal",
  );
  console.log("[ok] a declared datetime label counts as a sequence");
}

/* ── 4. The headline must not describe a trend over categories ─────── */
{
  const ranking = [
    { name: "الف", v: 900 },
    { name: "ب", v: 500 },
    { name: "ج", v: 120 },
  ];
  const framings = chooseFramings(ranking, "name", "v");
  const trendWords = ["صعودی", "کاهش", "روند"];
  for (const f of framings.filter((x) => !x.rejected)) {
    for (const w of trendWords) {
      assert.ok(
        !String(f.headline || "").includes(w),
        `framing "${f.kind}" describes categorical data with trend language ` +
        `("${w}"): ${JSON.stringify(f.headline)}. The number was right and the ` +
        "sentence was wrong, which is worse than no sentence",
      );
    }
  }
  console.log("[ok] no offered framing describes categorical data as a trend");
}

/* ── 5. Focus/context emphasis is not silently dead ────────────────── */
{
  // When the focus lands on the FIRST point, the old split produced a
  // one-element "context" array, the `length > 1` guard skipped the context
  // polyline entirely, and the whole line rendered in the focus colour. For
  // a descending ranking the focus IS always the first point, so the
  // emphasis encoding was structurally dead for that entire class of
  // result -- the colour and weight work landed and could never be seen.
  //
  // Asserted on chooseFocus + the split arithmetic rather than on pixels:
  // whichever way the fix goes (re-split, or deliberately accept a uniform
  // line and carry emphasis on the point and label alone), it must be a
  // decision the code states, not an accident of an off-by-one.
  const descending = [
    { d: "d0", v: 50 }, { d: "d1", v: 40 }, { d: "d2", v: 30 },
    { d: "d3", v: 20 }, { d: "d4", v: 10 },
  ];
  const focus = chooseFocus(descending, "d", "v", "line");
  assert.notEqual(focus.rule, "none", "chooseFocus must name a real rule");

  if (focus.index === 0) {
    const segStart = Math.max(0, focus.index - 1);
    const contextLength = segStart + 1;
    assert.ok(
      contextLength > 1 || focus.uniformLine === true,
      "the focus sits on the first point, so the context segment is a single " +
      "point and no context polyline is drawn -- the whole line renders in the " +
      "focus colour and the emphasis encoding does nothing. Either split so " +
      "both segments exist, or mark the framing as deliberately uniform " +
      "(focus.uniformLine === true) so the choice is stated rather than " +
      "produced by an off-by-one",
    );
  }
  console.log("[ok] focus/context emphasis is either drawn or declared uniform");
}

console.log("ALL_SCENARIOS_PASSED");
