# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A fake catalogue for the schema-sync tests: no database, no engine.

``tbl()`` builds the :class:`database.catalogue.TableInfo` a source's
``INFORMATION_SCHEMA`` would give; ``catalogue()`` keys them the way
:func:`database.catalogue.read_catalogue` does.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from database.catalogue import ColumnInfo, ForeignKey, TableInfo


def tbl(
    schema: str,
    name: str,
    columns: Sequence[tuple[str, str]],
    *,
    pk: Sequence[str] = (),
    fks: Iterable[tuple[Sequence[str], str, str, Sequence[str]]] = (),
    view: bool = False,
    nullable: bool = True,
) -> TableInfo:
    """One table. ``fks`` items are ``(columns, ref_schema, ref_table, ref_columns)``."""
    return TableInfo(
        schema=schema,
        name=name,
        is_view=view,
        columns=tuple(ColumnInfo(n, t, nullable) for n, t in columns),
        primary_key=tuple(pk),
        foreign_keys=tuple(
            ForeignKey(
                name=f"FK_{name}_{n}",
                columns=tuple(cols),
                ref_location=(rs.lower(), rt.lower()),
                ref_columns=tuple(rcols),
            )
            for n, (cols, rs, rt, rcols) in enumerate(fks)
        ),
    )


def catalogue(*tables: TableInfo) -> dict[tuple[str, str], TableInfo]:
    """``{(schema, table): info}`` for one source."""
    return {t.location: t for t in tables}
