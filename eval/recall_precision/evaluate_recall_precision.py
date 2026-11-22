# SPDX-License-Identifier: BUSL-1.1
"""Recall / Precision benchmark for the NLQ interpretation pipeline.

A *requirement-level* evaluation, distinct from ``eval/recall.py`` (table
*selection* recall) and from the result-fingerprint gate (executed rows match
a reference).  Per question we ask:

* **Recall**    - of the requirements the question actually needs (metric,
  entities, filters, date conditions, groupings, exclusions, ordering, top-N),
  what fraction did the system correctly extract?
* **Precision** - of the requirements the system extracted, what fraction is
  justified by the question?  Missing requirements never count against
  precision; unsupported extras never count against recall.

Two layers are scored.  The headline is **Layer B** (full production pipeline:
real model + real warehouse, generated SQL parsed back into requirements).
**Layer A** is the deterministic pre-LLM retrieval / filter-extraction
component (no model), reported as a diagnostic.

Nothing here changes production behaviour: it only *calls* the existing
:func:`eval.pipeline.make_production_pipeline` and
:func:`retrieval.context_retriever.ContextRetriever.retrieve`.

Run from the repository root::

    PYTHONIOENCODING=utf-8 py eval/recall_precision/evaluate_recall_precision.py --live
    PYTHONIOENCODING=utf-8 py eval/recall_precision/evaluate_recall_precision.py
    ... --live --limit 8 --no-cache
    ... --live --ids RP-001,RP-002

Outputs (next to this file): ``recall_precision_results.csv``,
``recall_precision_run.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import sqlglot
from sqlglot import exp

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
OUT_DIR = Path(__file__).resolve().parent
CASES_FILE = OUT_DIR / "recall_precision_test_cases.json"
CACHE_FILE = OUT_DIR / ".layer_b_cache.json"
RESULTS_CSV = OUT_DIR / "recall_precision_results.csv"
RUN_JSON = OUT_DIR / "recall_precision_run.json"

# schema (normalised) -> datasource, for the cross-source mis-route detection
DATA = {
    "auction_fact": "Auction_DM", "auction_dim": "Auction_DM",
    "general_dim": "Auction_DM", "future_fact": "Future_DM",
    "future_dim": "Future_DM", "cdcmarket_fact": "Future_DM",
    "cdcmarket_dim": "Future_DM", "option_fact": "Future_DM",
}
FACT_SCHEMAS = ("auction_fact", "future_fact", "cdcmarket_fact", "option_fact")

_PERSIAN = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def norm(text: str) -> str:
    if text is None:
        return ""
    return " ".join(str(text).translate(_PERSIAN).split()).lower()


def _col(table, col):
    if table is None:
        return norm(col)
    return f"{norm(table)}.{norm(col)}"


def _agg_key(fn, col_expr, table):
    if fn == "count":
        c = norm(col_expr)
        return "count(*)" if c in ("", "*", "(*)") else f"count({_col(table, c)})"
    return f"{fn}({_col(table, col_expr)})"


# ---------------------------------------------------------------------------
# Layer B: parse generated T-SQL into a structured requirement set
# ---------------------------------------------------------------------------

def parse_sql_requirements(sql):
    out = {
        "tables": [], "facts": [], "ds": set(), "metrics": [],
        "filters": [], "exclusions": [], "grouping": [], "ordering": [],
        "top": None, "error": None,
    }
    if not sql or not sql.strip():
        out["error"] = "no_sql"
        return out
    try:
        tree = sqlglot.parse_one(sql, read="tsql")
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"parse:{type(exc).__name__}"
        return out

    alias_to_table = {}
    for tbl in tree.find_all(exp.Table):
        schema = None
        db = tbl.args.get("db")
        if db is not None:
            schema = norm(str(db.this))
        raw = norm(tbl.name)
        key = f"{schema}.{raw}" if schema else raw
        out["tables"].append(key)
        if schema:
            ds = DATA.get(schema)
            if ds:
                out["ds"].add(ds)
                if schema in FACT_SCHEMAS:
                    out["facts"].append(raw)
        alias_to_table[tbl.alias_or_name] = key

    for fn in tree.find_all(exp.AggFunc):
        name = type(fn).__name__.lower()
        if name not in ("sum", "avg", "count", "max", "min"):
            continue
        this = fn.this
        if isinstance(this, exp.Star) or this is None:
            if name == "count":
                out["metrics"].append("count(*)")
            continue
        if isinstance(this, exp.Column):
            col, table = this.name, alias_to_table.get(this.table)
        else:
            col, table = this.sql(), None
        out["metrics"].append(_agg_key(name, col, table))

    limit = tree.args.get("limit")
    if limit is not None:
        try:
            out["top"] = int(limit.expression.this)
        except Exception:  # noqa: BLE001
            out["top"] = None
    if tree.args.get("offset") is not None:
        out["top"] = None

    where = tree.args.get("where")
    if where is not None:
        for node in _walk_where(where.this):
            kind, key, val = _classify_predicate(node, alias_to_table)
            if kind == "eq":
                out["filters"].append(f"{key}={val}")
            elif kind == "in":
                out["filters"].append(f"{key} in {{{val}}}")
            elif kind == "neq":
                out["exclusions"].append(f"{key}<> {val}")
            elif kind == "not_in":
                out["exclusions"].append(f"{key} not in {{{val}}}")
            elif kind in ("lt", "lte", "gt", "gte"):
                out["filters"].append(f"{key} {kind} {val}")
            elif kind == "between":
                out["filters"].append(f"{key} between {val[0]} and {val[1]}")
            elif kind == "like":
                out["filters"].append(f"{key} like {val}")

    grp = tree.args.get("group")
    if grp is not None:
        for g in grp.expressions:
            col = g.name if isinstance(g, exp.Column) else g.sql()
            table = alias_to_table.get(g.table) if isinstance(g, exp.Column) else None
            out["grouping"].append(_col(table, col))
    order = tree.args.get("order")
    if order is not None:
        for o in order.expressions:
            e = o.this
            col = e.name if isinstance(e, exp.Column) else (e.alias or e.sql())
            table = alias_to_table.get(e.table) if isinstance(e, exp.Column) else None
            direction = "desc" if o.args.get("desc", False) else "asc"
            out["ordering"].append(f"{_col(table, col)} {direction}")

    out["ds"] = sorted(out["ds"])
    out["facts"].sort()
    out["metrics"].sort()
    return out


def _walk_where(node):
    if isinstance(node, exp.And):
        yield from _walk_where(node.this)
        yield from _walk_where(node.expression)
    elif isinstance(node, exp.Paren):
        yield from _walk_where(node.this)
    else:
        yield node


def _lit(node):
    if node is None:
        return ""
    if isinstance(node, exp.Literal):
        return norm(str(node.this))
    if isinstance(node, exp.Neg):
        return "-" + _lit(node.this)
    return norm(node.sql())


def _classify_predicate(node, a2t):
    def col_of(n):
        if not isinstance(n, exp.Column):
            return None, None
        return a2t.get(n.table), n.name

    if isinstance(node, exp.EQ):
        lt, lc = col_of(node.left)
        rt, rc = col_of(node.right)
        if lc is None and rc is not None:
            return "eq", _col(rt, rc), _lit(node.left)
        if lc is not None:
            return "eq", _col(lt, lc), _lit(node.right)
        return None, None, None
    if isinstance(node, exp.NEQ):
        lt, lc = col_of(node.left)
        rt, rc = col_of(node.right)
        if lc is None and rc is not None:
            return "neq", _col(rt, rc), _lit(node.left)
        if lc is not None:
            return "neq", _col(lt, lc), _lit(node.right)
        return None, None, None
    if isinstance(node, exp.In):
        lt, lc = col_of(node.this)
        if lc is None:
            return None, None, None
        vals = ", ".join(_lit(v) for v in node.expressions)
        comp = bool(node.args.get("complement", False))
        return ("not_in" if comp else "in"), _col(lt, lc), vals
    if isinstance(node, (exp.LT, exp.LTE)):
        lt, lc = col_of(node.left)
        rt, rc = col_of(node.right)
        op = "lt" if isinstance(node, exp.LT) else "lte"
        if lc is not None:
            return op, _col(lt, lc), _lit(node.right)
        if rc is not None:
            return op, _col(rt, rc), _lit(node.left)
        return None, None, None
    if isinstance(node, (exp.GT, exp.GTE)):
        lt, lc = col_of(node.left)
        rt, rc = col_of(node.right)
        op = "gt" if isinstance(node, exp.GT) else "gte"
        if lc is not None:
            return op, _col(lt, lc), _lit(node.right)
        if rc is not None:
            return op, _col(rt, rc), _lit(node.left)
        return None, None, None
    if isinstance(node, exp.Between):
        lt, lc = col_of(node.this)
        if lc is not None:
            return "between", _col(lt, lc), (_lit(node.args.get("low")), _lit(node.args.get("high")))
        return None, None, None
    if isinstance(node, exp.Like):
        lt, lc = col_of(node.this)
        if lc is not None:
            return "like", _col(lt, lc), _lit(node.expression)
        return None, None, None
    return None, None, None


# ---------------------------------------------------------------------------
# Layer A: deterministic pre-LLM retrieval context (no model)
# ---------------------------------------------------------------------------

def layer_a_context(question: str):
    from retrieval.dimension_vocabulary import set_background_refresh_enabled
    from retrieval.context_retriever import ContextRetriever
    set_background_refresh_enabled(False)
    ctx = ContextRetriever.retrieve(question)
    return {
        "entities": list(ctx.entities),
        "facts": list(ctx.facts),
        "filters": dict(ctx.filters),
        "selected_tables": list(getattr(ctx, "selected_tables", [])),
        "warnings": list(getattr(ctx, "warnings", [])),
    }


# ---------------------------------------------------------------------------
# Layer B: drive the production pipeline once per question
# ---------------------------------------------------------------------------

def layer_b_generate(system_prompt, question):
    """Return (sql, info).  sql is None when no SQL was produced."""
    from eval.pipeline import make_production_pipeline
    from eval.runner import SourceTrace, PipelineFailure
    from database.executor import execute_sql
    from retrieval.dimension_vocabulary import set_background_refresh_enabled
    set_background_refresh_enabled(False)
    trace = SourceTrace()
    generate, _ = make_production_pipeline(system_prompt, trace, execute_sql)
    info = {"corrections": 0, "row_cap": None, "status": "ok"}
    try:
        sql = generate(question)
        info["corrections"] = trace.corrections
        info["row_cap"] = trace.row_cap
        return sql, info
    except PipelineFailure as pf:
        info["status"] = pf.kind
        return (pf.sql or ""), info
    except ValueError as ve:
        if str(ve) == "OUT_OF_SCOPE":
            info["status"] = "out_of_scope"
        else:
            info["status"] = "value_error"
        return (getattr(ve, "candidate_sql", None) or ""), info
    except Exception as exc:  # noqa: BLE001
        info["status"] = f"{type(exc).__name__}"
        return (getattr(exc, "candidate_sql", None) or ""), info


# ---------------------------------------------------------------------------
# Ground-truth item -> comparable form
# ---------------------------------------------------------------------------

def _split_pred(s):
    """Split a normalised predicate into (column, value).

    ``s`` is already lowercased/normalised, e.g.
    ``auction_dim.ring.name = n'تالار سیمان'`` ->
    (``auction_dim.ring.name``, ``تالار سیمان``); a bare column
    (``general_dim.date.persianyear``) -> (that, ``""``); an IN predicate
    keeps its value set.  Surrounding ``n''`` / ``''`` quotes and set braces
    are stripped so Layer A (bare value) and Layer B (quoted) compare on the
    same value.
    """
    s = s.strip()
    m = re.match(r"^([a-z0-9_]+(?:\.[a-z0-9_]+)*)\s*(.*)$", s, re.S)
    if not m:
        return s, ""
    col, val = m.group(1), m.group(2).lstrip()
    val = re.sub(r"^(<>|<=|>=|=|<|>)\s*", "", val)          # symbolic operators
    val = re.sub(r"^(not\s+in|in|between|like)\s*", "", val)  # word operators
    if val.startswith("not in"):
        val = val[6:].lstrip()
    val = val.strip()
    if val.startswith("{"):
        val = val[1:]
    if val.endswith("}"):
        val = val[:-1]
    if val.startswith("(") and val.endswith(")"):
        val = val[1:-1]
    # strip optional n-prefix + surrounding quotes:  n'...' / '...' / "..."
    if len(val) >= 2 and val[0] in "'\"" and val[-1] in "'\"":
        val = val[1:-1]
    if len(val) >= 3 and val[0] == "n" and val[1] in "'\"" and val[-1] in "'\"":
        val = val[2:-1]
    return col, val.strip()


def expected_items(case):
    """Expected requirement items.

    Each is (type, ref, allow, label, optional, match) where ``match`` is:
      * ``"value"``    - default; compare column + value (filters, absolute
        dates, metrics).  Value comparison is quote-insensitive, so Layer A's
        bare value matches Layer B's ``n'...'``.
      * ``"column"``   - compare the column only (IN-list filters, relative
        date conditions, exclusions — whose exact value wording is
        model-dependent but whose *column* is the point).
      * ``"direction"``- ordering items: compare only the sort direction
        (``desc``/``asc``), since the model orders by an aggregate alias.
      * ``"group"``    - grouping items: compare the grouped column.
    """
    items = []
    for it in case.get("expected", {}).get("items", []):
        items.append((
            it["type"],
            norm(it["ref"]),
            [norm(a) for a in it.get("allow", [])],
            it.get("label", it["ref"]),
            bool(it.get("optional", False)),
            it.get("match", "exact"),
        ))
    return items


def _item_matches(exp_item, etype, eref):
    itype, ref, allow, _label, _opt, match = exp_item
    if etype != itype:
        return False
    if match == "column":
        # compare the column only (relative dates, IN-lists, exclusions)
        return _split_pred(eref)[0] == _split_pred(ref)[0] or eref in allow
    if match == "direction":
        # ordering items "<col> desc": the model orders by an aggregate alias,
        # so only the sort direction is a reliable signal
        return eref.split()[-1] == ref.split()[-1]
    if match == "group":
        # grouping: compare the grouped column (or an allowed alternative)
        return _split_pred(eref)[0] == _split_pred(ref)[0] or eref in allow
    if match == "entity":
        # table/entity: exact or an allowed synonym (e.g. Contract~CustomerContract)
        return eref == ref or eref in allow
    if itype == "top":
        # a top-N requirement: "*any*" matches any explicit TOP the model wrote
        return ref == "*any*" or eref == ref or eref in allow
    # "value": compare column + value (the default for filters, dates, metrics)
    ecol, eval_ = _split_pred(eref)
    rcol, rval = _split_pred(ref)
    # count(id) and count(*) count the same rows — treat both as equivalent.
    if itype == "metric" and rcol == "count" and ecol == "count":
        return True
    if rcol == "*":
        return ecol == "*"
    if ecol != rcol:
        return False
    return eval_ == rval or eref in allow or ref in allow or eval_ in allow


_DATE_COLS = ("persianyear", "persiandate", "persianmonthname",
              "persianseasonname", "persiandayofweek", "realdate")


def layer_b_items(parsed, case=None, default_top_n=1000):
    """Requirement items extracted from generated SQL (Layer B).

    The row-cap ``TOP`` only counts as an extracted requirement when the
    question actually asks for a top-N (a case with a ``top`` expected item):
    the pipeline auto-injects a default cap (``default_top_n``) on every
    un-capped statement as a guardrail, which is not an interpretation.
    """
    items = []
    for f in parsed["facts"]:
        items.append(("entity", norm(f)))
    for t in parsed["tables"]:
        bare = t.split(".")[-1]
        if bare not in parsed["facts"] and bare != "date":
            items.append(("entity", bare))
    for m in parsed["metrics"]:
        items.append(("metric", m))
    for f in parsed["filters"]:
        items.append(("date" if any(c in f for c in _DATE_COLS) else "filter", f))
    for e in parsed["exclusions"]:
        items.append(("exclusion", e))
    for g in parsed["grouping"]:
        items.append(("grouping", g))
    for o in parsed["ordering"]:
        items.append(("ordering", o))
    wants_top = case is not None and any(i["type"] == "top"
                                         for i in case.get("expected", {}).get("items", []))
    if wants_top and parsed["top"] is not None:
        items.append(("top", str(parsed["top"])))
    return items


_LAYERA_FILTER_MAP = {
    "ring": ("auction_dim.ring.name={v}", "filter"),
    "persianyear": ("general_dim.date.persianyear={v}", "date"),
    "persiandate": ("general_dim.date.persiandate={v}", "date"),
    "persianmonthname": ("general_dim.date.persianmonthname={v}", "date"),
    "persianseasonname": ("general_dim.date.persianseasonname={v}", "date"),
    "persiandayofweek": ("general_dim.date.persiandayofweek={v}", "date"),
}


def layer_a_items(case, ctx, ranking_assumptions):
    """Requirement items from the deterministic retrieval context (Layer A).

    Layer A is a *diagnostic of the pre-LLM value extraction only*: the ring
    filter, the Persian-calendar date facets, and (for ranking questions) the
    deterministic measure assumption.  Entity/table selection is the model's
    job and is scored in Layer B, so it is deliberately not part of Layer A.
    """
    items = []
    for key, value in ctx["filters"].items():
        template, itype = _LAYERA_FILTER_MAP.get(norm_key(key), (None, None))
        if template:
            items.append((itype, template.format(v=value)))
    for a in ranking_assumptions:
        if a.get("field") != "measure":
            continue
        label = a.get("value", "")
        if "تعداد" in label:
            items.append(("metric", "count(*)"))
        elif "حجم" in label or "وزن" in label:
            items.append(("metric", "sum(hallmatchingweight)"))
        else:
            items.append(("metric", "sum(value)"))
    return items


def norm_key(key: str) -> str:
    return str(key).translate(_PERSIAN).strip().lower()


def ranking_assumptions_for(question, filters):
    """Deterministic measure/ring/period assumptions for ranking questions."""
    try:
        from session.ambiguity import assumptions_for_fresh
        assumptions, _clar, _amb = assumptions_for_fresh(question, dict(filters))
        return [{"field": a.field, "value": a.value, "source": a.source} for a in assumptions]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Matching + metrics
# ---------------------------------------------------------------------------

def _types_scored_for(layer):
    if layer == "A":
        # Layer A = the deterministic pre-LLM extraction only: it resolves the
        # ring filter and the Persian-calendar date facets.  Choosing the
        # metric/aggregation column is the model's job (driven by business
        # rules), so it is deliberately not part of Layer A.
        return {"filter", "date"}
    return {"entity", "filter", "date", "metric", "grouping", "ordering", "top", "exclusion"}


def score_case(case, extracted, layer):
    """Greedy 1:1 match of expected items against extracted items.

    ``layer`` ("A" or "B") selects which item *types* count: Layer A
    (deterministic retrieval) is judged on entity/filter/date/metric only — a
    missing grouping, ordering, top-N or exclusion is not counted against it,
    because that layer does not produce them.  Optional items count in recall
    only when matched; one the system *did* extract is never an "extra".
    """
    scored_types = _types_scored_for(layer)
    expected = [i for i in expected_items(case) if i[0] in scored_types]
    pool = [(t, r) for t, r in extracted if t in scored_types]
    matched, missing, opt_matched = [], [], []

    def try_match(item, bucket):
        for i, (etype, eref) in enumerate(pool):
            if _item_matches(item, etype, eref):
                pool.pop(i)
                bucket.append(item[3])
                return True
        return False

    for item in [i for i in expected if not i[4]]:
        if not try_match(item, matched):
            missing.append(item[3])
    for item in [i for i in expected if i[4]]:
        try_match(item, opt_matched)  # matched optional items drop out of the pool

    # recall numerator = required items matched only (optional extras excluded)
    extra = [f"{t}:{r}" for t, r in pool]
    total_req = len(matched) + len(missing)
    total_ext = len(matched) + len(opt_matched) + len(extra)
    recall = (len(matched) / total_req) if total_req else None
    precision = (len(matched) / total_ext) if total_ext else (None if total_req else 1.0)
    return {
        "matched": matched, "missing": missing, "extra": extra,
        "opt_matched": opt_matched,
        "recall": recall, "precision": precision,
        "n_required": total_req, "n_extracted": total_ext, "n_matched": len(matched),
    }


def aggregate(rows, key):
    vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else None


def _fmt(v):
    return "n/a" if v is None else f"{v:.4f}"


# ---------------------------------------------------------------------------
# Run + report
# ---------------------------------------------------------------------------

def load_cases(path=CASES_FILE):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def load_cache():
    if CACHE_FILE.exists():
        with open(CACHE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1)


_RANK_CUES = ("برتر", "بیشترین", "کمترین", "بالاترین")


def run_case(case, live, system_prompt, cache):
    cid = case["id"]
    q = case["question"]
    row = {
        "id": cid, "category": case["category"], "question": q,
        "note": case.get("note", ""),
        "expected_ref": " | ".join(
            i.get("label", i["ref"]) for i in case.get("expected", {}).get("items", [])),
    }

    # Layer A (deterministic) - always available
    ctx = layer_a_context(q)
    rank_assump = []
    if any(c in norm(q) for c in _RANK_CUES):
        rank_assump = ranking_assumptions_for(q, ctx["filters"])
    a_items = layer_a_items(case, ctx, rank_assump)
    a = score_case(case, a_items, "A")
    row.update({
        "A_extracted": " | ".join(f"{t}:{r}" for t, r in a_items) or "-",
        "A_missing": " | ".join(a["missing"]) or "-",
        "A_extra": " | ".join(a["extra"]) or "-",
        "A_recall": _fmt(a["recall"]), "A_precision": _fmt(a["precision"]),
        "A_matched_n": a["n_matched"], "A_required_n": a["n_required"],
        "A_extracted_n": a["n_extracted"],
        "A_recall_raw": a["recall"], "A_precision_raw": a["precision"],
    })

    if not live:
        row["B_status"] = "not_run"
        return row, cache

    if cid in cache and cache[cid].get("question") == q:
        b = cache[cid]
    else:
        t0 = time.time()
        sql, info = layer_b_generate(system_prompt, q)
        b = {"question": q, "sql": sql, "status": info["status"],
             "corrections": info["corrections"], "seconds": round(time.time() - t0, 1)}
        cache[cid] = b
        save_cache(cache)

    row["B_status"] = b["status"]
    row["B_corrections"] = b.get("corrections", "")
    if b.get("sql"):
        parsed = parse_sql_requirements(b["sql"])
        row["B_misroute"] = "yes" if "Future_DM" in parsed["ds"] else "no"
        b_items = layer_b_items(parsed, case=case)
        bb = score_case(case, b_items, "B")
        row.update({
            "B_sql": " ".join(b["sql"].split())[:400],
            "B_extracted": " | ".join(f"{t}:{r}" for t, r in b_items) or "-",
            "B_missing": " | ".join(bb["missing"]) or "-",
            "B_extra": " | ".join(bb["extra"]) or "-",
            "B_recall": _fmt(bb["recall"]), "B_precision": _fmt(bb["precision"]),
            "B_matched_n": bb["n_matched"], "B_required_n": bb["n_required"],
            "B_extracted_n": bb["n_extracted"],
            "B_recall_raw": bb["recall"], "B_precision_raw": bb["precision"],
        })
    else:
        row["B_sql"] = "-"
        row.update({
            "B_extracted": "-", "B_missing": "no SQL produced", "B_extra": "-",
            "B_recall": "0.0000", "B_precision": "0.0000",
            "B_matched_n": 0, "B_required_n": sum(1 for i in expected_items(case) if not i[4]),
            "B_extracted_n": 0, "B_recall_raw": 0.0, "B_precision_raw": 0.0,
        })
    return row, cache


def summarize(rows):
    out = {"total": len(rows), "by_category": {}}
    for r in rows:
        d = out["by_category"].setdefault(r["category"], {"n": 0, "B_R": 0, "B_P": 0, "A_R": 0, "A_P": 0})
        d["n"] += 1
        for k in ("B_recall_raw", "B_precision_raw", "A_recall_raw", "A_precision_raw"):
            if isinstance(r.get(k), (int, float)):
                d[{"B_recall_raw": "B_R", "B_precision_raw": "B_P",
                   "A_recall_raw": "A_R", "A_precision_raw": "A_P"}[k]] += r[k]
    for d in out["by_category"].values():
        for k in ("B_R", "B_P", "A_R", "A_P"):
            d[k] = d[k] / d["n"] if d["n"] else None

    def micro(pfx):
        m = sum(r.get(f"{pfx}_matched_n", 0) for r in rows if r.get(f"{pfx}_status", "ok") != "not_run")
        req = sum(r.get(f"{pfx}_required_n", 0) for r in rows)
        ext = sum(r.get(f"{pfx}_extracted_n", 0) for r in rows)
        miss = req - m
        return {
            "macro_recall": aggregate(rows, f"{pfx}_recall_raw"),
            "macro_precision": aggregate(rows, f"{pfx}_precision_raw"),
            "micro_recall": (m / (m + miss)) if (m + miss) else None,
            "micro_precision": (m / ext) if ext else None,
        }
    out["A"] = micro("A")
    out["B"] = micro("B")
    out["B"]["misroutes"] = sum(1 for r in rows if r.get("B_misroute") == "yes")
    out["B"]["statuses"] = {}
    for r in rows:
        st = r.get("B_status", "")
        if st and st != "ok":
            out["B"]["statuses"][st] = out["B"]["statuses"].get(st, 0) + 1
    return out


def write_csv(rows):
    import csv
    cols = ["id", "category", "question", "expected_ref", "B_status", "B_misroute", "B_sql",
            "B_extracted", "B_missing", "B_extra", "B_recall", "B_precision",
            "A_extracted", "A_missing", "A_extra", "A_recall", "A_precision", "note"]
    with open(RESULTS_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    ap = argparse.ArgumentParser(description="NLQ recall/precision benchmark")
    ap.add_argument("--live", action="store_true", help="run Layer B (full model pipeline)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--cases", default=str(CASES_FILE))
    args = ap.parse_args()

    cases = load_cases(Path(args.cases))
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[:args.limit]

    cache = {} if args.no_cache else load_cache()
    system_prompt = None
    if args.live:
        from eval.cli import _load_system_prompt
        system_prompt = _load_system_prompt()

    rows = []
    for i, case in enumerate(cases, 1):
        row, cache = run_case(case, args.live, system_prompt, cache)
        rows.append(row)
        bstat = f"B:{row.get('B_status','')} " if args.live else ""
        print(f"[{i:3d}/{len(cases)}] {case['id']} {bstat} "
              f"B_R={row.get('B_recall','-')} B_P={row.get('B_precision','-')} "
              f"A_R={row['A_recall']} A_P={row['A_precision']}", flush=True)

    write_csv(rows)
    summary = summarize(rows)
    with open(RUN_JSON, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "rows": rows}, fh, ensure_ascii=False, indent=1)
    print("\n=== SUMMARY ===")
    for layer in ("A", "B"):
        d = summary[layer]
        extra = f" misroutes={d.get('misroutes')}" if layer == "B" else ""
        print(f"Layer {layer}: macro R={_fmt(d['macro_recall'])} P={_fmt(d['macro_precision'])} "
              f"micro R={_fmt(d['micro_recall'])} P={_fmt(d['micro_precision'])}{extra}")
    print(f"Wrote {RESULTS_CSV} and {RUN_JSON}")


if __name__ == "__main__":
    main()
