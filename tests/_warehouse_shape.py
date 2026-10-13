# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A warehouse-shaped fixture: two SQLite data sources that look like a real one.

Why it exists
-------------
Every other fixture of the suite is a small, tidy sample schema, and twice the
first real use of a setup tool found a bug the 6,600 tests around it missed
(the schema inspector keyed tables by bare name, and a warehouse that repeats
names across schemas gave a draft with about 70 duplicate keys). This module
builds the shape such a warehouse really has, with invented, neutral names, so
that every setup tool (``database.schema_inspector_cli``,
``scripts/sync_schema.py``, ``scripts/prompt_budget.py``,
``scripts/verify_deployment.py`` and the retrieval layer) is exercised on it in
``tests/test_setup_on_warehouse_shape.py``, and stays green.

The shape
---------
Two data sources, each one SQLite file whose schemas are ``ATTACH``ed
databases (SQLAlchemy reflects an attached database as a schema; see
:func:`attached`). SQLite allows ten attached databases per connection, which
bounds the number of schemas per source.

``wh_large`` (the default source), 262 tables in ten schemas:

* fact schemas ``sales_Fact``, ``orders_Fact``; dimension schemas ``sales_Dim``,
  ``orders_Dim``; staging schemas ``sales_Stg``, ``orders_Stg``, mostly
  near-copies of the fact tables of their domain, a few with different columns;
* ``Replay`` (change-data-capture tables ``<Table>_Inserts`` / ``_Updates`` /
  ``_Deletes``), ``Alerts`` and ``Metadata``; three more CDC triples live in
  ``sales_Fact`` itself;
* ``common``, with the calendar and other shared dimensions.

``wh_small``, 75 tables in seven schemas: ``ledger_Fact``, ``ledger_Dim``,
``ledger_Stg``, ``ref``, ``Metadata``, ``dbo`` (two tables) and ``common``,
which holds ``Calendar`` too: ``common.Calendar`` is in both sources, the one
table the two share.

The same table name occurs in several schemas: ``Invoice`` and ``Payment`` in
four (a fact schema and its staging schema, for two domains), the dimension
``Party`` in three, ``Status`` and ``Region`` in two, and every staging table
that copies a fact table. ``Invoice`` is also a table of ``ledger_Fact`` in the
other source. ``orders_Stg`` spells it ``INVOICE`` (names are compared without
regard to case).

No foreign key is declared. Links exist by naming only: ``<Table>_ID`` pointing
at ``<Table>.ID``, role-prefixed ones (``BuyerParty_ID``), a misspelled one
(``SellerPrty_ID``) and one without the underscore (``SegmentID``). Every table
has an ``ID`` column; some tables are empty; a few hold Persian text, and one
holds values with a double quote and a backslash. One table has a space in its
name (``ref.[Price List]``) and one a dot (``Metadata.[Run.Log]``), the names
that need bracket quoting.

Curated configuration
---------------------
:func:`write_project_config` writes the hand-style ``project_config/`` of a
deployment that curated 34 of these tables: bare keys for names unique in the
warehouse (several of them with a same-named staging table, which a later
``--add-tables`` must not collide with), ``Schema.Table`` keys for shared
names, descriptions, ``db_schema``, a stale column, columns the databases have
and the file lacks, and no ``datasource:`` line for the shared calendar.

Determinism
-----------
Nothing is random: names, columns and rows come from the tables below and a
CRC of the table name, so two builds are byte-identical in content.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

__all__ = [
    "CURATED",
    "GOLDEN_QUESTIONS",
    "LARGE",
    "SMALL",
    "Source",
    "Table",
    "Warehouse",
    "attached",
    "build_warehouse",
    "curated_keys",
    "shared_names",
    "write_golden",
    "write_project_config",
]

#: The default (large) data source, and the small one.
LARGE = "wh_large"
SMALL = "wh_small"

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Table:
    """One table: where it is, its columns and whether it has a primary key."""

    schema: str
    name: str
    kind: str                                   # fact | dim | stg | cdc | alert | meta | common | ref | dbo
    columns: tuple[tuple[str, str], ...]
    primary_key: bool = True

    @property
    def location(self) -> tuple[str, str]:
        """``(schema, table)`` lower-cased, the key of ``read_catalogue``."""
        return (self.schema.lower(), self.name.lower())


@dataclass(frozen=True)
class Source:
    """One data source: a main SQLite file plus one attached file per schema."""

    name: str
    main: Path
    schemas: dict[str, Path]
    tables: tuple[Table, ...]

    @property
    def url(self) -> str:
        """The SQLAlchemy URL of the main file (the attached files are not in it)."""
        return f"sqlite:///{self.main.as_posix()}"

    def count(self, schema: str) -> int:
        """Tables in *schema*."""
        return sum(1 for t in self.tables if t.schema == schema)

    def by_schema(self) -> dict[str, int]:
        """``{schema: table count}``."""
        return dict(Counter(t.schema for t in self.tables))


@dataclass(frozen=True)
class Warehouse:
    """Both data sources and the directory they were built in."""

    root: Path
    sources: dict[str, Source]

    def engine(self, name: str) -> Engine:
        """A new engine on source *name*; use it inside :func:`attached`."""
        return create_engine(self.sources[name].url)

    def table(self, source: str, schema: str, name: str) -> Table:
        """The table ``schema.name`` of *source*."""
        return next(t for t in self.sources[source].tables if (t.schema, t.name) == (schema, name))


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_NOUNS = (
    "Invoice", "Payment", "Shipment", "Return", "Adjustment", "Refund", "Quote",
    "Contract", "Reservation", "Allocation", "Settlement", "Transfer", "Accrual",
    "Receipt", "Charge", "Credit", "Debit", "Rebate", "Commission", "Fee",
    "Deposit", "Withdrawal", "Booking", "Delivery", "Dispatch", "Pickup", "Claim",
    "Penalty", "Discount", "Surcharge", "Tax", "Levy", "Tariff",            # 33: sales_Fact
    "Premium", "Deductible", "Voucher", "Coupon", "Subscription", "Renewal",
    "Cancellation", "Upgrade", "Downgrade", "Referral", "Lead", "Opportunity",
    "Campaign", "Visit", "Inquiry", "Complaint", "Ticket", "Task", "Audit",
    "Inspection", "Certificate", "License", "Permit", "Registration", "Enrollment",
    "Amendment", "Waiver",
)                                                                            # 27: orders_Fact

_SALES_FACT = _NOUNS[:33]
_SALES_CDC = ("Adjustment", "Refund", "Credit")            # three CDC triples inside sales_Fact
_ORDERS_FACT = (
    "Invoice", "Payment", *_NOUNS[33:],
    "Order", "OrderLine", "OrderEvent", "OrderNote", "OrderHold", "OrderSplit",
    "OrderMerge", "OrderAudit", "OrderTotal", "OrderRoute", "OrderBackfill",
)
_STG_HELPERS = (
    "LoadControl", "LoadError", "RowReject", "BatchState", "SourceFile", "KeyMap",
    "Checkpoint", "Watermark", "StageLineage", "DeadLetter",
)
_REPLAY_BASES = (*_NOUNS[:4], *_NOUNS[6:12])

_SALES_DIM = (
    "Party", "Status", "Region", "Category", "Segment", "Product", "Brand", "Channel",
    "Branch", "Agent", "Employee", "Territory", "Promotion", "Warehouse", "Carrier",
    "PaymentMethod", "ShipMethod", "Reason", "Priority", "Tier", "Season", "Customer",
)
_ORDERS_DIM = (
    "Party", "Status", "Region", "OrderType", "OrderSource", "FulfillmentCenter",
    "Courier", "Vendor", "Buyer", "Seller", "Contact", "Address", "Country", "City",
    "PostalZone", "Incoterm", "Lane", "Dock",
)
_COMMON_LARGE = (
    "Calendar", "Party", "Currency", "Unit", "TimeBucket", "Weekday", "Quarter", "Month",
)
_ALERTS = (
    "Alert", "AlertRule", "AlertRecipient", "AlertChannel", "AlertHistory",
    "AlertSuppression", "Threshold", "Escalation", "Notification", "Subscriber",
    "Digest", "Template", "Schedule", "Heartbeat",
)
_METADATA_LARGE = (
    "LoadBatch", "LoadStep", "SchemaVersion", "TableCatalog", "ColumnCatalog", "JobRun",
    "JobStep", "DataSource", "Lineage", "QualityCheck", "QualityResult", "Run.Log",
)

_LEDGER_FACT = (
    "Invoice", "Journal", "JournalLine", "Posting", "Balance", "TrialBalance",
    "Reconciliation", "Allocation", "Accrual", "Revaluation", "Closing", "Provision",
    "WriteOff", "Remittance", "Statement", "Dunning", "Collection", "Payable",
    "Receivable", "Asset", "Depreciation",
)
_LEDGER_CDC = ("Journal",)
_LEDGER_DIM = (
    "Account", "AccountGroup", "CostCenter", "Currency", "Entity", "Department",
    "Project", "FiscalPeriod", "Ledger", "TaxCode", "Vendor", "PaymentTerm",
)
_COMMON_SMALL = ("Calendar", "Holiday", "TimeZone")
_REF = ("Price List", "Rating", "Industry", "Language", "Sector", "Benchmark", "Indicator", "Curve")
_METADATA_SMALL = (
    "ExportBatch", "ExportStep", "ExportWatermark", "SchemaRevision", "ExportError",
    "Mapping", "Setting",
)
_DBO = ("AppSetting", "Migration")

#: Dimensions a fact table of each domain points at, by ``<Dim>_ID`` columns.
_DIM_REFS = {
    "sales": ("Customer", "Product", "Branch", "Channel", "Category"),
    "orders": ("Vendor", "Courier", "Country", "Region", "Status"),
    "ledger": ("Account", "CostCenter", "Entity", "Currency", "Department"),
}

#: Persian sample text (generic words), and values with characters that must be quoted.
_PERSIAN = ("شرکت نمونه", "منطقه شمالی", "دسته‌بندی یک", "تهران", "خدمات مشتری")
_AWKWARD = ('Pipe 5" steel', "Path\\to", "Plain")
_PERSIAN_TABLES = {("common", "Party"), ("sales_Dim", "Region"), ("orders_Dim", "Region")}
_AWKWARD_TABLES = {("sales_Dim", "Category")}


# ---------------------------------------------------------------------------
# Table definitions
# ---------------------------------------------------------------------------

def _dim_columns(name: str) -> tuple[tuple[str, str], ...]:
    columns = [("ID", "INTEGER"), ("Name", "NVARCHAR(100)"), ("Code", "NVARCHAR(20)"), ("IsActive", "BOOLEAN")]
    if name == "Category":
        columns.append(("ParentCategoryID", "INTEGER"))
    if name == "Calendar":
        columns = [
            ("ID", "INTEGER"), ("DateValue", "DATE"), ("Year", "INTEGER"), ("Month", "INTEGER"),
            ("Quarter", "INTEGER"), ("DayName", "NVARCHAR(20)"), ("IsHoliday", "BOOLEAN"),
        ]
    elif len(name) % 2:
        columns.append(("Description", "NVARCHAR(200)"))
    return tuple(columns)


def _fact_columns(domain: str, name: str, index: int) -> tuple[tuple[str, str], ...]:
    refs = _DIM_REFS[domain]
    wanted = 2 + index % 3
    chosen = [refs[(index + step) % len(refs)] for step in range(wanted)]
    columns = [("ID", "INTEGER"), ("Calendar_ID", "INTEGER")]
    columns += [(f"{ref}_ID", "INTEGER") for ref in chosen]
    if name == "Invoice":
        columns += [
            ("BuyerParty_ID", "INTEGER"), ("SellerParty_ID", "INTEGER"),
            ("SellerPrty_ID", "INTEGER"),               # misspelled on purpose
        ]
    if name == "Shipment":
        columns.append(("SegmentID", "INTEGER"))        # no underscore
    columns += [("Amount", "DECIMAL(18,2)"), ("Quantity", "INTEGER")]
    if index % 2:
        columns.append(("UnitPrice", "DECIMAL(18,4)"))
    if index % 3 != 1:
        columns.append(("Note", "NVARCHAR(200)"))
    columns += [("Status", "NVARCHAR(20)"), ("CreatedAt", "DATETIME")]
    return tuple(columns)


def _staging_columns(fact: Table, position: int) -> tuple[tuple[str, str], ...]:
    """A near-copy of *fact*; every fourth one differs the way a raw load does."""
    columns = list(fact.columns)
    if position % 4 == 3:
        columns = [
            ("AmountRaw", "NVARCHAR(40)") if n == "Amount" else (n, t)
            for n, t in columns if n not in ("Note", "Calendar_ID")
        ]
    return (*columns, ("LoadBatch_ID", "INTEGER"), ("RowHash", "NVARCHAR(64)"), ("IsValid", "BOOLEAN"))


def _cdc_columns(base: Table, kind: str) -> tuple[tuple[str, str], ...]:
    keep = base.columns[:4] if kind == "Deletes" else base.columns
    return (*keep, ("ChangeType", "NVARCHAR(10)"), ("ChangedAt", "DATETIME"), ("SeqNo", "INTEGER"))


def _simple(schema: str, names: Sequence[str], kind: str, columns: tuple[tuple[str, str], ...]) -> list[Table]:
    return [Table(schema, name, kind, columns) for name in names]


def _plan_large() -> list[Table]:
    tables: list[Table] = []
    sales_fact = [
        Table("sales_Fact", name, "fact", _fact_columns("sales", name, i), primary_key=i % 7 != 6)
        for i, name in enumerate(_SALES_FACT)
    ]
    tables += sales_fact
    for base in _SALES_CDC:
        source = next(t for t in sales_fact if t.name == base)
        for kind in ("Inserts", "Updates", "Deletes"):
            tables.append(Table("sales_Fact", f"{base}_{kind}", "cdc", _cdc_columns(source, kind), primary_key=False))
    tables += [Table("sales_Dim", n, "dim", _dim_columns(n)) for n in _SALES_DIM]
    tables += [
        Table("sales_Stg", f.name, "stg", _staging_columns(f, i), primary_key=False)
        for i, f in enumerate(sales_fact[:30])
    ]
    tables += [
        Table("sales_Stg", n, "stg", (("ID", "INTEGER"), ("Message", "NVARCHAR(400)"), ("LoggedAt", "DATETIME")), primary_key=False)
        for n in _STG_HELPERS
    ]

    orders_fact = [
        Table("orders_Fact", name, "fact", _fact_columns("orders", name, i))
        for i, name in enumerate(_ORDERS_FACT)
    ]
    tables += orders_fact
    tables += [Table("orders_Dim", n, "dim", _dim_columns(n)) for n in _ORDERS_DIM]
    for i, fact in enumerate(orders_fact[:32]):
        name = "INVOICE" if fact.name == "Invoice" else fact.name
        tables.append(Table("orders_Stg", name, "stg", _staging_columns(fact, i), primary_key=False))
    tables += [
        Table("orders_Stg", n, "stg", (("ID", "INTEGER"), ("Message", "NVARCHAR(400)"), ("LoggedAt", "DATETIME")), primary_key=False)
        for n in _STG_HELPERS[:4]
    ]

    by_name = {t.name: t for t in sales_fact}
    for base in _REPLAY_BASES:
        for kind in ("Inserts", "Updates", "Deletes"):
            tables.append(Table("Replay", f"{base}_{kind}", "cdc", _cdc_columns(by_name[base], kind), primary_key=False))
    alert_columns = (
        ("ID", "INTEGER"), ("Name", "NVARCHAR(100)"), ("Severity", "INTEGER"),
        ("Message", "NVARCHAR(400)"), ("RaisedAt", "DATETIME"), ("IsAck", "BOOLEAN"),
    )
    tables += _simple("Alerts", _ALERTS, "alert", alert_columns)
    meta_columns = (("ID", "INTEGER"), ("Name", "NVARCHAR(100)"), ("Value", "NVARCHAR(200)"), ("UpdatedAt", "DATETIME"))
    tables += _simple("Metadata", _METADATA_LARGE, "meta", meta_columns)
    tables += [Table("common", n, "common", _dim_columns(n)) for n in _COMMON_LARGE]
    return tables


def _plan_small() -> list[Table]:
    tables: list[Table] = []
    ledger_fact = [
        Table("ledger_Fact", name, "fact", _fact_columns("ledger", name, i))
        for i, name in enumerate(_LEDGER_FACT)
    ]
    tables += ledger_fact
    for base in _LEDGER_CDC:
        source = next(t for t in ledger_fact if t.name == base)
        for kind in ("Inserts", "Updates", "Deletes"):
            tables.append(Table("ledger_Fact", f"{base}_{kind}", "cdc", _cdc_columns(source, kind), primary_key=False))
    tables += [Table("ledger_Dim", n, "dim", _dim_columns(n)) for n in _LEDGER_DIM]
    tables += [
        Table("ledger_Stg", f.name, "stg", _staging_columns(f, i), primary_key=False)
        for i, f in enumerate(ledger_fact[:17])
    ]
    tables += [
        Table("ledger_Stg", n, "stg", (("ID", "INTEGER"), ("Message", "NVARCHAR(400)"), ("LoggedAt", "DATETIME")), primary_key=False)
        for n in _STG_HELPERS[:2]
    ]
    tables += [Table("common", n, "common", _dim_columns(n)) for n in _COMMON_SMALL]
    tables += [Table("ref", n, "ref", _dim_columns(n)) for n in _REF]
    meta_columns = (("ID", "INTEGER"), ("Name", "NVARCHAR(100)"), ("Value", "NVARCHAR(200)"), ("UpdatedAt", "DATETIME"))
    tables += _simple("Metadata", _METADATA_SMALL, "meta", meta_columns)
    tables += _simple("dbo", _DBO, "dbo", meta_columns)
    return tables


# ---------------------------------------------------------------------------
# Rows and files
# ---------------------------------------------------------------------------

def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row_count(table: Table) -> int:
    """0 to 4 rows: about one table in five is empty, the shared calendar and the Persian ones never."""
    if (table.schema, table.name) in _PERSIAN_TABLES | _AWKWARD_TABLES or table.name == "Calendar":
        return 3
    crc = zlib.crc32(f"{table.schema}.{table.name}".encode("utf-8"))
    return 0 if crc % 5 == 0 else 1 + crc % 4


def _value(table: Table, column: str, sql_type: str, row: int) -> object:
    upper = sql_type.upper()
    key = (table.schema, table.name)
    if column == "ID":
        return row + 1
    if column == "Name" and key in _PERSIAN_TABLES:
        return _PERSIAN[row % len(_PERSIAN)]
    if column == "Name" and key in _AWKWARD_TABLES:
        return _AWKWARD[row % len(_AWKWARD)]
    if upper.startswith("INTEGER"):
        return row % 3 + 1
    if upper.startswith("DECIMAL"):
        return round(10.5 * (row + 1), 2)
    if upper.startswith("BOOLEAN"):
        return row % 2
    if upper in ("DATE", "DATETIME"):
        return f"2024-01-{row + 1:02d}" + (" 00:00:00" if upper == "DATETIME" else "")
    if column == "Status":
        return ("Open", "Closed", "Held")[row % 3]
    if column == "Code":
        return f"C{row + 1:03d}"
    if column == "RowHash":
        return f"{zlib.crc32(f'{table.name}{row}'.encode('utf-8')):08x}"
    return f"{column.lower()} {row + 1}"


def _write_schema_file(path: Path, tables: Sequence[Table]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        for table in tables:
            parts = []
            for column, sql_type in table.columns:
                flag = " PRIMARY KEY" if column == "ID" and table.primary_key else ""
                parts.append(f"{_quote(column)} {sql_type}{flag}")
            connection.execute(f"CREATE TABLE {_quote(table.name)} ({', '.join(parts)})")
            rows = [
                tuple(_value(table, column, sql_type, row) for column, sql_type in table.columns)
                for row in range(_row_count(table))
            ]
            if rows:
                marks = ", ".join("?" for _ in table.columns)
                connection.executemany(f"INSERT INTO {_quote(table.name)} VALUES ({marks})", rows)
        connection.commit()
    finally:
        connection.close()


def build_warehouse(root: Path) -> Warehouse:
    """Build both data sources under *root* and return them.

    Parameters
    ----------
    root:
        An empty directory (the session's ``tmp_path_factory`` directory).

    Returns
    -------
    Warehouse
        With ``wh_large`` and ``wh_small``; about a second of work, no
        randomness.
    """
    sources: dict[str, Source] = {}
    for name, tables in ((LARGE, _plan_large()), (SMALL, _plan_small())):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        grouped: dict[str, list[Table]] = defaultdict(list)
        for table in tables:
            grouped[table.schema].append(table)
        main = directory / "main.db"
        sqlite3.connect(main).close()
        schemas = {schema: directory / f"{schema}.db" for schema in grouped}
        for schema, group in grouped.items():
            _write_schema_file(schemas[schema], group)
        sources[name] = Source(name=name, main=main, schemas=schemas, tables=tuple(tables))
    return Warehouse(root=root, sources=sources)


@contextmanager
def attached(warehouse: Warehouse) -> Iterator[None]:
    """Make every SQLite connection to a source's main file see its schemas.

    ``ATTACH`` is per connection and a URL cannot carry it, while the tools
    under test build their own engines from a URL. For the length of the
    ``with`` block a ``connect`` listener on :class:`sqlalchemy.engine.Engine`
    attaches the source's schema files to any new connection whose main
    database is one of the warehouse's files. Nothing else is touched.
    """
    wanted = {
        os.path.normcase(os.path.realpath(source.main)): source for source in warehouse.sources.values()
    }

    def attach(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        for _, name, file in dbapi_connection.execute("PRAGMA database_list").fetchall():
            if name != "main":
                continue
            source = wanted.get(os.path.normcase(os.path.realpath(file))) if file else None
            if source is None:
                return
            for schema, path in source.schemas.items():
                dbapi_connection.execute(f"ATTACH DATABASE ? AS {_quote(schema)}", (str(path),))
            return

    event.listen(Engine, "connect", attach)
    try:
        yield
    finally:
        event.remove(Engine, "connect", attach)


# ---------------------------------------------------------------------------
# Shape queries
# ---------------------------------------------------------------------------

def shared_names(source: Source) -> dict[str, tuple[str, ...]]:
    """``{lower-cased name: schemas}`` for every table name that occurs in several schemas of *source*."""
    schemas: dict[str, set[str]] = defaultdict(set)
    for table in source.tables:
        schemas[table.name.lower()].add(table.schema)
    return {name: tuple(sorted(found)) for name, found in sorted(schemas.items()) if len(found) > 1}


# ---------------------------------------------------------------------------
# The curated project_config/
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Curated:
    """One table of the hand-written ``schema.yaml``.

    ``key`` is the key as written; ``source`` the data source the table is in;
    ``datasource`` the ``datasource:`` line the author wrote (``None`` for
    none); ``stale`` a column the author listed that the database does not
    have; ``keep`` how many of the table's real columns the author listed.
    """

    key: str
    source: str
    schema: str
    table: str
    description: str
    datasource: str | None = None
    keep: int = 4
    stale: str = ""
    resolvable: tuple[str, ...] = ()


CURATED: tuple[Curated, ...] = (
    # ---- shared dimension ----
    Curated("common.Calendar", LARGE, "common", "Calendar",
            "common.Calendar — calendar, one row per day; in both databases (تقویم)", keep=5),
    # ---- dimensions of the large source, bare keys: the names are unique ----
    Curated("Customer", LARGE, "sales_Dim", "Customer", "sales_Dim.Customer — customers (مشتری)",
            keep=4, stale="Legacy_Code", resolvable=("Name",)),
    Curated("Product", LARGE, "sales_Dim", "Product", "sales_Dim.Product — products"),
    Curated("Segment", LARGE, "sales_Dim", "Segment", "sales_Dim.Segment — customer segments"),
    Curated("Category", LARGE, "sales_Dim", "Category", "sales_Dim.Category — product categories", keep=3),
    Curated("Branch", LARGE, "sales_Dim", "Branch", "sales_Dim.Branch — branches"),
    # ---- dimensions whose name is shared: qualified keys ----
    Curated("sales_Dim.Party", LARGE, "sales_Dim", "Party", "sales_Dim.Party — parties of the sales domain"),
    Curated("common.Party", LARGE, "common", "Party", "common.Party — parties shared by every domain (طرف حساب)", keep=3),
    Curated("sales_Dim.Region", LARGE, "sales_Dim", "Region", "sales_Dim.Region — sales regions (منطقه)"),
    Curated("orders_Dim.Region", LARGE, "orders_Dim", "Region", "orders_Dim.Region — delivery regions"),
    Curated("orders_Dim.Vendor", LARGE, "orders_Dim", "Vendor", "orders_Dim.Vendor — vendors"),
    # ---- facts: shared names are qualified ----
    Curated("sales_Fact.Invoice", LARGE, "sales_Fact", "Invoice", "sales_Fact.Invoice — invoices of the sales domain (فاکتور)", keep=6, stale="Legacy_Total"),
    Curated("orders_Fact.Invoice", LARGE, "orders_Fact", "Invoice", "orders_Fact.Invoice — invoices of the orders domain", keep=5),
    Curated("sales_Fact.Payment", LARGE, "sales_Fact", "Payment", "sales_Fact.Payment — payments received (پرداخت)"),
    Curated("orders_Fact.Payment", LARGE, "orders_Fact", "Payment", "orders_Fact.Payment — payments made"),
    # ---- facts with a bare key although a staging table has the same name ----
    Curated("Shipment", LARGE, "sales_Fact", "Shipment", "sales_Fact.Shipment — shipments", keep=5),
    Curated("Quote", LARGE, "sales_Fact", "Quote", "sales_Fact.Quote — quotes"),
    Curated("Contract", LARGE, "sales_Fact", "Contract", "sales_Fact.Contract — contracts (قرارداد)"),
    Curated("Refund_Inserts", LARGE, "sales_Fact", "Refund_Inserts", "sales_Fact.Refund_Inserts — inserted refunds, change capture"),
    Curated("Order", LARGE, "orders_Fact", "Order", "orders_Fact.Order — orders (سفارش)"),
    Curated("OrderLine", LARGE, "orders_Fact", "OrderLine", "orders_Fact.OrderLine — order lines"),
    # ---- operational schemas ----
    Curated("LoadBatch", LARGE, "Metadata", "LoadBatch", "Metadata.LoadBatch — load batches"),
    Curated("[Run.Log]", LARGE, "Metadata", "Run.Log", "Metadata.[Run.Log] — run log; the name has a dot"),
    Curated("Alert", LARGE, "Alerts", "Alert", "Alerts.Alert — raised alerts"),
    # ---- the small source ----
    Curated("ledger_Fact.Invoice", SMALL, "ledger_Fact", "Invoice", "ledger_Fact.Invoice — ledger invoices", datasource=SMALL, keep=5),
    Curated("Journal", SMALL, "ledger_Fact", "Journal", "ledger_Fact.Journal — journal entries", datasource=SMALL),
    Curated("JournalLine", SMALL, "ledger_Fact", "JournalLine", "ledger_Fact.JournalLine — journal lines", datasource=SMALL),
    Curated("Posting", SMALL, "ledger_Fact", "Posting", "ledger_Fact.Posting — postings"),
    Curated("Account", SMALL, "ledger_Dim", "Account", "ledger_Dim.Account — ledger accounts (حساب)", datasource=SMALL),
    Curated("CostCenter", SMALL, "ledger_Dim", "CostCenter", "ledger_Dim.CostCenter — cost centres"),
    Curated("ledger_Dim.Vendor", SMALL, "ledger_Dim", "Vendor", "ledger_Dim.Vendor — vendors of the ledger", datasource=SMALL),
    Curated("Holiday", SMALL, "common", "Holiday", "common.Holiday — public holidays", datasource=SMALL),
    Curated("[Price List]", SMALL, "ref", "Price List", "ref.[Price List] — price lists; the name has a space"),
    Curated("AppSetting", SMALL, "dbo", "AppSetting", "dbo.AppSetting — application settings"),
)


def curated_keys() -> tuple[str, ...]:
    """The ``schema.yaml`` keys of :data:`CURATED`, in file order."""
    return tuple(entry.key for entry in CURATED)


_RELATIONSHIPS = (
    ("sales_Fact.Invoice", "Customer",
     "JOIN [sales_Dim].[Customer] ON [sales_Fact].[Invoice].[Customer_ID] = [sales_Dim].[Customer].[ID]"),
    ("sales_Fact.Invoice", "common.Calendar",
     "JOIN [common].[Calendar] ON [sales_Fact].[Invoice].[Calendar_ID] = [common].[Calendar].[ID]"),
    ("Order", "OrderLine",
     "JOIN [orders_Fact].[OrderLine] ON [orders_Fact].[Order].[ID] = [orders_Fact].[OrderLine].[ID]"),
)


def _yaml_key(key: str) -> str:
    return json.dumps(key, ensure_ascii=False) if key.startswith("[") else key


def _schema_yaml(warehouse: Warehouse) -> str:
    lines = [
        "# Warehouse schema, curated by hand for the two databases.",
        "# Bare keys for names that are unique in the warehouse, Schema.Table for the rest.",
        "",
        "tables:",
    ]
    section = ""
    for entry in CURATED:
        title = {LARGE: "large database", SMALL: "small database"}[entry.source]
        if title != section:
            section = title
            lines += ["", f"  # ---- {title} ----"]
        table = warehouse.table(entry.source, entry.schema, entry.table)
        lines.append(f"  {_yaml_key(entry.key)}:")
        if not entry.key.startswith(entry.schema + "."):
            lines.append(f"    db_schema: {entry.schema}")
        if entry.datasource:
            lines.append(f"    datasource: {entry.datasource}")
        lines.append(f"    description: {json.dumps(entry.description, ensure_ascii=False)}")
        lines.append("    columns:")
        listed = [c for c, _ in table.columns[: entry.keep]]
        for column in listed:
            text = "Primary key" if column == "ID" else (
                f"FK to {column[:-3]}" if column.endswith("_ID") else f"{column} of the row"
            )
            lines.append(f"      {column}: {json.dumps(text, ensure_ascii=False)}")
        if entry.stale:
            lines.append(f"      {entry.stale}: \"dropped in an earlier release\"   # hand-written, no longer in the database")
        if entry.resolvable:
            lines.append(f"    resolvable_columns: [{', '.join(entry.resolvable)}]")
        lines.append("")
    lines.append("relationships:")
    for source, target, sql in _RELATIONSHIPS:
        lines += [f"  - from_table: {_yaml_key(source)}", f"    to_table: {_yaml_key(target)}", f"    join_sql: {json.dumps(sql)}"]
    return "\n".join(lines) + "\n"


_DATASOURCES = f"""\
default: {LARGE}

datasources:
  {LARGE}:
    url_env: DB_URL_WH_LARGE
    description: Large warehouse with the sales and orders domains
    keywords: [sales, orders, invoice, payment, shipment]
  {SMALL}:
    url_env: DB_URL_WH_SMALL
    description: Small ledger database
    keywords: [ledger, journal, account, posting]
"""

_ALIASES = """\
ring_aliases:
  "Region North":
    - "north"
    - "northern region"
    - "شمال"
  "Region South":
    - "south"
    - "southern region"
  "Region East":
    - "east"
    - "eastern region"

synonyms:
  "annual": ["year", "calendar"]
  "monthly": ["month", "calendar"]
  "buyer": ["customer", "party"]
  "seller": ["vendor", "party"]
"""

# An entity is named by the schema.yaml key of its table: retrieval returns the
# entity name as the table to put in the prompt.
_ENTITIES = """\
entities:
  Customer:
    aliases: ["customer", "customers", "client", "مشتری"]
    table: "Customer"

  orders_Dim.Vendor:
    aliases: ["vendor", "vendors", "supplier"]
    table: "Vendor"
    schema_name: "orders_Dim"

  Account:
    aliases: ["account", "ledger account", "حساب"]
    table: "Account"
"""

_RETRIEVAL_HINTS = """\
fact_tables:
  - "sales_Fact.Invoice"
  - "orders_Fact.Invoice"
  - "orders_Fact.Payment"
  - "Journal"

always_include:
  common.Calendar:
    - "year"
    - "month"
    - "quarter"
    - "annual"
    - "monthly"
    - "calendar"
  Customer:
    - "customer"
    - "customers"
    - "client"
  Account:
    - "account"
    - "ledger"

fact_patterns:
  sales_Fact.Invoice:
    - "invoice"
    - "sales invoice"
  orders_Fact.Payment:
    - "payment"
    - "payments"
  Journal:
    - "journal"
    - "posting"
"""

_EXAMPLES = """\
examples:
  - tags: ["customer", "count"]
    question: "How many customers exist?"
    sql: "SELECT COUNT(*) AS CustomerCount FROM [sales_Dim].[Customer]"

  - tags: ["invoice", "sum"]
    question: "What is the total invoice amount?"
    sql: "SELECT SUM(Amount) AS TotalAmount FROM [sales_Fact].[Invoice]"
"""

_METRICS = """\
metrics:
  total_invoiced:
    aliases: ["total invoiced", "invoice total"]
    expression: "SUM(Invoice.Amount)"

  total_quantity:
    aliases: ["total quantity", "volume"]
    expression: "SUM(Invoice.Quantity)"
"""

_BUSINESS_RULES = """\
rules:
  invoices:
    rule_text: |
      Invoice questions use sales_Fact.Invoice unless the question is about orders.
      Invoice value = SUM(Invoice.Amount)

  calendar:
    rule_text: |
      Date filters use common.Calendar (Year, Month, Quarter).
"""


def write_project_config(destination: Path, warehouse: Warehouse) -> Path:
    """Write the curated ``project_config/`` into *destination*.

    ``schema.yaml``, ``datasources.yaml`` and ``aliases.yaml`` are the ones the
    tests care about; the other required files are adapted small versions of
    ``project_config.example/``'s, and ``session_policy.yaml``,
    ``memory_policy.yaml`` and ``system_prompt.md`` are copied from it.

    Parameters
    ----------
    destination:
        The directory to fill; created when missing.
    warehouse:
        The built warehouse the curated tables are taken from.

    Returns
    -------
    Path
        *destination*.
    """
    destination.mkdir(parents=True, exist_ok=True)
    texts = {
        "schema.yaml": _schema_yaml(warehouse),
        "datasources.yaml": _DATASOURCES,
        "aliases.yaml": _ALIASES,
        "entities.yaml": _ENTITIES,
        "retrieval_hints.yaml": _RETRIEVAL_HINTS,
        "examples.yaml": _EXAMPLES,
        "metrics.yaml": _METRICS,
        "business_rules.yaml": _BUSINESS_RULES,
    }
    for name, text in texts.items():
        (destination / name).write_text(text, encoding="utf-8", newline="\n")
    example = _REPO_ROOT / "project_config.example"
    for name in ("session_policy.yaml", "memory_policy.yaml", "system_prompt.md"):
        shutil.copyfile(example / name, destination / name)
    return destination


#: ``(id, question, expected_sql, datasource)``: questions with the tables they read.
GOLDEN_QUESTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("customers_count", "How many customers are there?",
     "SELECT COUNT(*) FROM [Customer]", LARGE),
    ("sales_invoice_by_customer", "Total sales invoice amount per customer",
     "SELECT c.Name, SUM(i.Amount) FROM [sales_Fact].[Invoice] i JOIN [Customer] c ON i.Customer_ID = c.ID GROUP BY c.Name", LARGE),
    ("orders_invoice_count", "How many invoices did the orders domain issue?",
     "SELECT COUNT(*) FROM [orders_Fact].[Invoice]", LARGE),
    ("shipments_count", "Count the shipments",
     "SELECT COUNT(*) FROM [Shipment]", LARGE),
    ("payments_by_vendor", "Payments made per vendor",
     "SELECT v.Name, SUM(p.Amount) FROM [orders_Fact].[Payment] p JOIN [orders_Dim].[Vendor] v ON p.Vendor_ID = v.ID GROUP BY v.Name", LARGE),
    ("journals_by_account", "How many journal entries per account?",
     "SELECT a.Name, COUNT(*) FROM [Journal] j JOIN [Account] a ON j.Account_ID = a.ID GROUP BY a.Name", SMALL),
    ("invoices_per_year", "Number of sales invoices per calendar year",
     "SELECT d.Year, COUNT(*) FROM [sales_Fact].[Invoice] i JOIN [common].[Calendar] d ON i.Calendar_ID = d.ID GROUP BY d.Year", LARGE),
    ("open_alerts", "List the raised alerts",
     "SELECT * FROM [Alerts].[Alert]", LARGE),
    ("price_lists", "Show the price lists",
     "SELECT * FROM [ref].[Price List]", SMALL),
    ("order_count_fa", "تعداد سفارش‌ها چقدر است؟",
     "SELECT COUNT(*) FROM [Order]", LARGE),
    ("payments_total_fa", "مجموع پرداخت‌های سفارش‌ها",
     "SELECT SUM(Amount) FROM [orders_Fact].[Payment]", LARGE),
    ("holidays_count", "How many public holidays are listed?",
     "SELECT COUNT(*) FROM [Holiday]", SMALL),
)


def write_golden(path: Path) -> Path:
    """Write :data:`GOLDEN_QUESTIONS` as a ``golden.jsonl`` file and return *path*."""
    lines = [
        json.dumps(
            {"id": case_id, "question": question, "expected_sql": sql, "datasource": source,
             "tags": ["warehouse_shape"], "expect": "success"},
            ensure_ascii=False,
        )
        for case_id, question, sql, source in GOLDEN_QUESTIONS
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path
