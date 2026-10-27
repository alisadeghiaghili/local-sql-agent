# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Which numbers of a result are money, for the wording of the answer summary.

The summary prompt used to say that every monetary value is in Rials and that
amounts are always written with the unit Rial/ریال. A model told that attaches
the unit to every number it sees: a total traded *volume* came back as
"33,493,503,232 Rial". The rule is now "Rial only for money", and this module
tells the model, per result column, which side of the rule each column is on,
decided here and not left to the model whenever the schema or the column name
settles it.

How a column is classified
--------------------------
In this order, the first that applies wins:

1. ``column_units:`` of ``schema.yaml`` (:func:`schema_data.registry.get_column_units`):
   ``rial`` is money, ``none`` is not. The operator's word.
2. The column name: a currency word (``Rial``, ``ریال``) is money; then a word
   for a count, quantity, volume, weight, percentage, identifier, code or date
   is not money; then a word for a price, value, amount, cost, fee or the like
   (``قیمت``, ``ارزش``, ``مبلغ``, ``کارمزد``) is money. A count word beats a
   money word on purpose: ``FeeCount`` is a number of fees.
3. Otherwise the column is *unknown* and the model decides: Rial only if the
   numbers are clearly an amount of money.

Only columns that hold numbers are listed; a text or date column never gets a
currency unit.

Nothing here reads a row value except to see whether it is a number, and
nothing here can fail the summary: a ``schema.yaml`` that cannot be read means
the name rule alone.
"""

from __future__ import annotations

import logging
import numbers
import re
from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MONEY",
    "NOT_MONEY",
    "UNKNOWN",
    "classify_column",
    "column_hints",
    "declared_units",
    "numeric_columns",
]

#: The three answers :func:`classify_column` gives.
MONEY = "money"
NOT_MONEY = "not-money"
UNKNOWN = "unknown"

#: Words of a column name that name the currency itself.
_CURRENCY_WORDS = frozenset({"rial", "rials", "irr", "ریال"})

#: Words for a number that is not an amount of money. Latin words are compared
#: in lower case; ``s`` of a plural is dropped before the comparison.
_NOT_MONEY_WORDS = frozenset({
    "count", "cnt", "qty", "quantity", "number", "num", "volume", "vol",
    "weight", "wt", "percent", "pct", "percentage", "ratio",
    "id", "uid", "code", "date", "year", "month", "day", "rank",
    "حجم", "تعداد", "مقدار", "وزن", "درصد", "نسبت",
    "کد", "شناسه", "شماره", "تاریخ", "سال", "ماه", "روز", "رتبه",
})

#: Words for an amount of money.
_MONEY_WORDS = frozenset({
    "price", "value", "amount", "amt", "cost", "fee", "payment", "salary",
    "wage", "revenue", "income", "profit", "balance", "debt", "turnover",
    "commission", "tax", "budget", "worth",
    "ارزش", "قیمت", "مبلغ", "کارمزد", "هزینه", "بها", "درآمد", "سود", "بدهی", "مانده",
})

_CAMEL_BREAK = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_DIGITS = re.compile(r"\d+")
_SEPARATORS = re.compile(r"[\s_\-./‌]+")

#: Persian endings that leave the word itself: plural ``ها``/``های`` and the
#: adjective ``ی`` (``ریالی`` is "in Rials").
_PERSIAN_ENDINGS = ("ها", "های", "ی")


def _words(name: str) -> list[str]:
    """The words of a column name, lower case: split at ``_``, spaces, ZWNJ and camel-case humps.

    >>> _words("TotalTradeVolume"), _words("trade_value"), _words("RingID")
    (['total', 'trade', 'volume'], ['trade', 'value'], ['ring', 'id'])
    >>> _words("حجم معاملات"), _words("ارزش‌ها")
    (['حجم', 'معاملات'], ['ارزش', 'ها'])
    """
    spaced = _DIGITS.sub(" ", _CAMEL_BREAK.sub(" ", name))
    return [word.lower() for word in _SEPARATORS.split(spaced) if word]


def _is_one_of(word: str, vocabulary: frozenset[str]) -> bool:
    """Whether *word* is in *vocabulary*, allowing an English plural or a Persian ending.

    >>> _is_one_of("prices", _MONEY_WORDS), _is_one_of("ریالی", _CURRENCY_WORDS)
    (True, True)
    >>> _is_one_of("valid", _MONEY_WORDS), _is_one_of("paid", _NOT_MONEY_WORDS)
    (False, False)
    """
    if word in vocabulary:
        return True
    if word.endswith("s") and word[:-1] in vocabulary:
        return True
    return any(word.endswith(end) and word[: -len(end)] in vocabulary for end in _PERSIAN_ENDINGS)


def classify_column(name: str, declared: Mapping[str, str] | None = None) -> str:
    """Whether the numbers of result column *name* are money.

    Parameters
    ----------
    name:
        The column's name in the result (an alias, as the query wrote it).
    declared:
        ``{lower-case column name: "rial" | "none"}`` from ``schema.yaml``
        (:func:`declared_units`); ``None`` or empty for none.

    Returns
    -------
    str
        :data:`MONEY`, :data:`NOT_MONEY` or :data:`UNKNOWN`.

    Examples
    --------
    >>> classify_column("TotalVolume"), classify_column("TotalValue"), classify_column("Total")
    ('not-money', 'money', 'unknown')
    >>> classify_column("حجم کل معاملات"), classify_column("ارزش معاملات")
    ('not-money', 'money')
    >>> classify_column("VolumeInRial"), classify_column("FeeCount"), classify_column("RingID")
    ('money', 'not-money', 'not-money')
    >>> classify_column("TotalValue", {"totalvalue": "none"})
    'not-money'
    >>> classify_column("Total", {"total": "rial"})
    'money'
    """
    unit = (declared or {}).get(name.lower())
    if unit == "rial":
        return MONEY
    if unit == "none":
        return NOT_MONEY
    words = _words(name)
    if any(_is_one_of(word, _CURRENCY_WORDS) for word in words):
        return MONEY
    if any(_is_one_of(word, _NOT_MONEY_WORDS) for word in words):
        return NOT_MONEY
    if any(_is_one_of(word, _MONEY_WORDS) for word in words):
        return MONEY
    return UNKNOWN


def declared_units() -> dict[str, str]:
    """``column_units:`` of the loaded ``schema.yaml``; ``{}`` when there is none to read.

    Never raises: a deployment without a readable ``schema.yaml`` still gets a
    summary, worded from the column names alone.
    """
    try:
        from schema_data.registry import get_column_units

        return dict(get_column_units())
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.debug("No column units from schema.yaml (%s); using column names only", exc)
        return {}


def numeric_columns(rows: Sequence[Mapping[str, object]]) -> list[str]:
    """The columns, in the order the rows have them, that hold a number in at least one row.

    A boolean is not a number here.

    >>> numeric_columns([{"a": "x", "b": 1, "c": True}, {"a": "y", "b": None, "d": 2.5}])
    ['b', 'd']
    """
    found: dict[str, None] = {}
    for row in rows:
        for key, value in row.items():
            if isinstance(value, numbers.Number) and not isinstance(value, bool):
                found.setdefault(str(key), None)
    return list(found)


def column_hints(rows: Sequence[Mapping[str, object]], declared: Mapping[str, str] | None = None) -> str:
    """The prompt lines that say which numeric columns of *rows* are money.

    Parameters
    ----------
    rows:
        The rows shown to the model.
    declared:
        See :func:`classify_column`.

    Returns
    -------
    str
        A short block ending in a newline, or ``""`` when no column holds a number.

    Examples
    --------
    >>> print(column_hints([{"Name": "A", "TotalVolume": 5, "TotalValue": 9, "Total": 3}]), end="")
    Which numeric columns of the result are money (decided from the schema or the column name):
    - Money, in Rials (write Rial/ریال): TotalValue
    - Not money, plain numbers (no Rial/ریال, no other currency): TotalVolume
    - Not known (write Rial/ریال only if the numbers are clearly an amount of money): Total
    >>> column_hints([{"Name": "A"}])
    ''
    """
    groups: dict[str, list[str]] = {MONEY: [], NOT_MONEY: [], UNKNOWN: []}
    for name in numeric_columns(rows):
        groups[classify_column(name, declared)].append(name)
    lines = [
        (MONEY, "- Money, in Rials (write Rial/ریال): "),
        (NOT_MONEY, "- Not money, plain numbers (no Rial/ریال, no other currency): "),
        (UNKNOWN, "- Not known (write Rial/ریال only if the numbers are clearly an amount of money): "),
    ]
    body = [label + ", ".join(groups[kind]) for kind, label in lines if groups[kind]]
    if not body:
        return ""
    header = "Which numeric columns of the result are money (decided from the schema or the column name):"
    return "\n".join([header, *body]) + "\n"


