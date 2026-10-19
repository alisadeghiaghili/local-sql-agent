# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""What a raw data value looks like, without keeping the value.

Two tools need to tell a *word* from a *value read out of a row*:

* the setup wizard, which may turn the values of a categorical column into
  synonym keys (``--value-synonyms``) and must never take a date, a phone
  number, an address, a masked name or an error message (:func:`unsuitable_value`);
* the preflight (``core.config_doctor``), which counts the synonym keys of an
  ``aliases.yaml`` that look like such values and reports them by *shape*
  (``<date>``, ``<phone>``), never by the value (:func:`value_shape`,
  :func:`shape_label`).

Both rely on the same rules, kept here so a shape the wizard refuses is the
shape the preflight flags.

Public API
----------
SHAPES
    The shape names :func:`value_shape` returns.
value_shape(text) -> str | None
    The shape of *text* when it looks like a raw data value, else ``None``.
shape_label(shape) -> str
    ``"<date>"`` for ``"date"``: how a shape is shown in a message.
unsuitable_value(text, max_length) -> str | None
    Why *text* must not become a synonym key, or ``None``.
identifier_words(identifier) -> list[str]
    The lower-cased words of a CamelCase or snake_case name.
is_categorical_column(table, column) -> bool
    Whether the values of a text column may be considered as synonym keys.
categorical_values(values, max_distinct) -> list[str]
    The values of a low-cardinality column that may become synonym keys.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from core.persian import normalize_for_matching

__all__ = [
    "MAX_DISTINCT_VALUES",
    "MAX_VALUE_LENGTH",
    "SHAPES",
    "VALUES_PER_TABLE",
    "categorical_values",
    "identifier_words",
    "is_categorical_column",
    "shape_label",
    "unsuitable_value",
    "value_shape",
]

#: The shapes :func:`value_shape` can return, most specific first.
SHAPES: tuple[str, ...] = (
    "masked", "email", "url", "ip", "date", "time", "phone", "hash", "digits", "address",
)

#: Longest value a synonym key may be taken from: a name, title or status is
#: short, a sentence or a message is not.
MAX_VALUE_LENGTH = 40

#: A column with more distinct values than this is not categorical: it holds
#: names, notes, identifiers, whatever it is called.
MAX_DISTINCT_VALUES = 50

#: The most synonym keys taken from one table.
VALUES_PER_TABLE = 20

#: A segment of this many characters with digits and several words is an address.
_ADDRESS_LENGTH = 30

#: Words that make a run of words with digits an address (English and Persian).
_ADDRESS_WORD = re.compile(
    r"\b(?:street|st\.|avenue|ave\.?|road|rd\.|boulevard|blvd|lane|suite|apt\.?|floor|unit|plot)\b"
    r"|خیابان|کوچه|بلوار|پلاک|میدان|طبقه|واحد|بزرگراه|خ\.",
    re.IGNORECASE,
)

_MASKED = re.compile(r"(?<![a-z])x{3,}(?![a-z])|\*{2,}|•{2,}|#{3,}|…|\.{4,}")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|www\.)\S+", re.IGNORECASE)
_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?:/\d{1,2})?$")
_IPV6 = re.compile(r"^(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}$", re.IGNORECASE)
_TIME = re.compile(r"^\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:\s?[ap]m)?$", re.IGNORECASE)
_ZONE = r"(?:\s?(?:z|[+-]\d{2}:?\d{2}))?"
_CLOCK = r"(?:[ t]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:\s?[ap]m)?)?"
_DATE_YMD = re.compile(rf"^\d{{4}}[/.-]\d{{1,2}}[/.-]\d{{1,2}}{_CLOCK}{_ZONE}$", re.IGNORECASE)
_DATE_DMY = re.compile(rf"^\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}{_CLOCK}{_ZONE}$", re.IGNORECASE)
_DATE_COMPACT = re.compile(r"^(?:1[34]|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:[01]\d[0-5]\d[0-5]\d)?$")
_MONTH = (
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
)
_DATE_WORDS = re.compile(
    rf"^(?:\d{{1,2}}\s+{_MONTH}\.?,?\s+\d{{2,4}}|{_MONTH}\.?\s+\d{{1,2}},?\s+\d{{2,4}}){_CLOCK}$",
    re.IGNORECASE,
)
_PHONE = re.compile(r"^\+?\(?\d[\d\s().-]{5,}\d$")
_DIGIT_RUN = re.compile(r"\d{8,}")
_HASH = re.compile(
    r"^(?:[0-9a-f]{16,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE,
)
_ERROR_TEXT = re.compile(
    r"\b(?:error|exception|failed|failure|timeout|timed out|invalid|cannot|can't|unable|denied|"
    r"traceback|stack ?trace|null reference|not found|unauthori[sz]ed|forbidden|refused|"
    r"warning|fatal)\b|خطا|ناموفق|نامعتبر|یافت نشد",
    re.IGNORECASE,
)


def _digits(text: str) -> int:
    return sum(ch.isdigit() for ch in text)


def value_shape(text: str) -> str | None:
    """The shape of *text* when it looks like a raw data value, else ``None``.

    Persian and Arabic-Indic digits count as digits. A word, a short number
    (a year, ``q1``) and a hyphenated name have no shape: a curated
    vocabulary may hold them. The shapes are those of :data:`SHAPES`.

    Parameters
    ----------
    text:
        A synonym key, a sample value, any short string.

    Returns
    -------
    str | None
        One of :data:`SHAPES`, or ``None``.

    Examples
    --------
    >>> value_shape("1387/03/24"), value_shape("2024-05-01 10:00:00"), value_shape("۱۴۰۲/۰۵/۰۱")
    ('date', 'date', 'date')
    >>> value_shape("01/01/2010"), value_shape("20240131"), value_shape("12:30:05")
    ('date', 'date', 'time')
    >>> value_shape("+1 (555) 010-0100"), value_shape("09120000000")
    ('phone', 'phone')
    >>> value_shape("10.0.12.200"), value_shape("fe80::1"), value_shape("user@example.test")
    ('ip', 'ip', 'email')
    >>> value_shape("ab xxxxx cd"), value_shape("**** 12"), value_shape("1234567890")
    ('masked', 'masked', 'digits')
    >>> value_shape("12 Example Street Building 4 Floor 2")
    'address'
    >>> value_shape("customer"), value_shape("1402"), value_shape("q1"), value_shape("")
    (None, None, None, None)
    """
    folded = normalize_for_matching(text or "")
    if not folded:
        return None
    if _MASKED.search(folded):
        return "masked"
    if _EMAIL.match(folded):
        return "email"
    if _URL.match(folded):
        return "url"
    if _IPV4.match(folded) or (_IPV6.match(folded) and ("::" in folded or re.search(r"[a-f]", folded))):
        return "ip"
    if _TIME.match(folded):
        return "time"
    if (
        _DATE_YMD.match(folded) or _DATE_DMY.match(folded)
        or _DATE_COMPACT.match(folded) or _DATE_WORDS.match(folded)
    ):
        return "date"
    if _PHONE.match(folded):
        count = _digits(folded)
        plain = folded.lstrip("+").isdigit()
        if 7 <= count <= 15 and (not plain or folded.startswith(("+", "09"))):
            return "phone"
    if _HASH.match(folded) and _digits(folded) >= 2:
        return "hash"
    if _DIGIT_RUN.search(folded):
        return "digits"
    words = folded.split()
    if len(words) >= 3 and _digits(folded) and (len(folded) >= _ADDRESS_LENGTH or _ADDRESS_WORD.search(folded)):
        return "address"
    return None


def shape_label(shape: str) -> str:
    """How a shape is written in a message: the value is never shown, only this.

    Examples
    --------
    >>> shape_label("date")
    '<date>'
    """
    return f"<{shape}>"


def unsuitable_value(text: str, max_length: int = MAX_VALUE_LENGTH) -> str | None:
    """Why *text* must not become a synonym key, or ``None`` when it may.

    Everything :func:`value_shape` names is refused, and so are: a value
    longer than *max_length*, a value shorter than two characters, a value
    that is mostly digits or punctuation (a code), and text that reads as an
    error message. What is left is a short word or phrase: a status, a kind,
    a title.

    Parameters
    ----------
    text:
        A distinct value of a text column.
    max_length:
        The longest value accepted.

    Returns
    -------
    str | None
        A short reason (a shape name, ``"long"``, ``"short"``, ``"code"``,
        ``"error"``), or ``None`` for a usable value.

    Examples
    --------
    >>> unsuitable_value("Active"), unsuitable_value("Open Account")
    (None, None)
    >>> unsuitable_value("2024-05-01"), unsuitable_value("A1-77/9"), unsuitable_value("x")
    ('date', 'code', 'short')
    >>> unsuitable_value("Connection timeout while reading the response")
    'long'
    >>> unsuitable_value("Login failed"), unsuitable_value("a" * 41)
    ('error', 'long')
    """
    value = (text or "").strip()
    if len(value) < 2:
        return "short"
    if len(value) > max_length:
        return "long"
    shape = value_shape(value)
    if shape:
        return shape
    letters = sum(ch.isalpha() for ch in value)
    if letters * 2 < len(value.replace(" ", "")):
        return "code"
    if _ERROR_TEXT.search(value):
        return "error"
    return None


# ---------------------------------------------------------------------------
# Which columns may give synonym keys
# ---------------------------------------------------------------------------

#: A column whose name has one of these words holds a short label of a kind,
#: the only values worth being a synonym for a table.
_LABEL_WORDS = frozenset({
    "name", "title", "kind", "status", "type", "category", "class", "label", "caption", "group",
})

#: Words of a column name that make its values personal or contact data. Long
#: ones are looked for inside the name (``fullname``, ``mobileno``), short
#: ones only as a whole word, so ``zip`` does not match ``zipper`` and ``ip``
#: does not match ``description``.
_PERSON_LONG = (
    "family", "surname", "firstname", "lastname", "fullname", "nickname", "father", "mother", "spouse",
    "birth", "national", "passport", "mobile", "phone", "address", "email", "contact", "username",
    "login", "password", "person", "postal", "iban", "account", "credit",
)
_PERSON_SHORT = frozenset({
    "first", "last", "full", "sur", "nick", "fname", "lname", "user", "tel", "fax", "mail", "nid",
    "ssn", "ip", "url", "zip", "card", "pwd", "pass", "cell", "sex", "gender", "age",
})

#: Words of a table name that make it a table of people: its ``Name`` and
#: ``Title`` columns are people's names. Its ``Status`` and ``Type`` are not.
_PERSON_TABLE = (
    "person", "people", "user", "customer", "client", "employee", "staff", "member", "contact", "patient",
    "student", "investor", "owner", "holder", "personnel", "worker", "individual", "party",
)


def identifier_words(identifier: str) -> list[str]:
    """The lower-cased words of a CamelCase or snake_case identifier.

    >>> identifier_words("BankName"), identifier_words("action_type_ID"), identifier_words("ISINCode")
    (['bank', 'name'], ['action', 'type', 'id'], ['isin', 'code'])
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", identifier or "")
    return [w.lower() for w in re.split(r"[^A-Za-z0-9\u0600-\u06ff]+", spaced) if w]


def is_categorical_column(table: str, column: str) -> bool:
    """Whether the values of a text column may be considered as synonym keys.

    The column must be named like a label (a word such as ``name``,
    ``title``, ``kind``, ``status``, ``type``, ``category``). It must not be
    named like personal or contact data (``FirstName``, ``Family``,
    ``Mobile``, ``Phone``, ``Address``, ``Email``, ``NationalCode``, ...), and
    a ``Name`` or ``Title`` column of a table of people (``Customer``,
    ``Employee``, ``User``, ...) is out as well. A bare ``Name`` of a lookup
    table (``Bank.Name``) is in: it holds the vocabulary a question uses.

    Parameters
    ----------
    table:
        The table's bare name.
    column:
        The column's name.

    Returns
    -------
    bool
        ``True`` when the column's distinct values are worth reading.

    Examples
    --------
    >>> is_categorical_column("Bank", "Name"), is_categorical_column("Trade", "StatusTitle")
    (True, True)
    >>> is_categorical_column("Bank", "Description"), is_categorical_column("Trade", "Amount")
    (False, False)
    >>> is_categorical_column("Person", "FullName"), is_categorical_column("Bank", "ContactName")
    (False, False)
    >>> is_categorical_column("Customer", "Name"), is_categorical_column("Customer", "Status")
    (False, True)
    >>> is_categorical_column("Bank", "Family"), is_categorical_column("Bank", "Mobile_Name")
    (False, False)
    """
    words = identifier_words(column)
    squashed = "".join(words)
    if not _LABEL_WORDS & set(words):
        return False
    if _PERSON_SHORT & set(words) or any(term in squashed for term in _PERSON_LONG):
        return False
    people = "".join(identifier_words(table))
    if any(term in people for term in _PERSON_TABLE) and {"name", "title"} & set(words):
        return False
    return True


def categorical_values(values: Iterable[str], max_distinct: int = MAX_DISTINCT_VALUES) -> list[str]:
    """The values of a low-cardinality column that may become synonym keys.

    Parameters
    ----------
    values:
        The distinct values read from the column (read at most
        ``max_distinct + 1``: one more tells that the column is not categorical).
    max_distinct:
        More distinct values than this and the column is not categorical:
        nothing is returned.

    Returns
    -------
    list[str]
        The values :func:`unsuitable_value` accepts, in the order given,
        without repeats (case ignored).

    Examples
    --------
    >>> categorical_values(["Open", "Closed", "2024-05-01", "Login failed", "ab xxxxx cd", "open"])
    ['Open', 'Closed']
    >>> categorical_values([str(n) + " kind" for n in range(60)], 50)
    []
    """
    distinct = list(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()))
    if len(distinct) > max_distinct:
        return []
    kept: dict[str, str] = {}
    for value in distinct:
        if unsuitable_value(value) is None:
            kept.setdefault(value.casefold(), value)
    return list(kept.values())
