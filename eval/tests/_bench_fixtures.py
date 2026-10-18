# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A tiny package in the shape of BIRD mini-dev, built from synthetic data.

Two SQLite databases (``shop``, ``library``), a ``mini_dev_sqlite.json`` with
nine questions and a ``database_description`` folder for ``shop``. Among the
questions are one whose reference fails, one whose reference returns nothing,
and one whose result differs once the data are loaded into another database
(a column that mixes numbers and text is loaded as text, so ``typeof`` changes).

The target stands in for SQL Server: one SQLite file whose schemas are
``ATTACH``ed files (``tests/_inspector_fixtures.py``).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests._inspector_fixtures import Database, build_database

SHOP_SCHEMA = """
CREATE TABLE Customer (ID INTEGER PRIMARY KEY, Name TEXT, City TEXT, Joined DATE);
CREATE TABLE "Order Item" (
    ID INTEGER PRIMARY KEY,
    CustomerID INTEGER REFERENCES Customer(ID),
    Total REAL,
    "Free Qty (K-12)" INTEGER
);
CREATE TABLE Review (ID INTEGER PRIMARY KEY, Rating);
INSERT INTO Customer VALUES (1, 'Ada', 'Paris', '2020-01-02'), (2, 'Bo', 'Oslo', '2021-05-06'),
                            (3, 'Cy', 'Paris', '2022-07-08');
INSERT INTO "Order Item" VALUES (1, 1, 10.5, 1), (2, 1, 4.25, NULL), (3, 2, 7.0, 2);
INSERT INTO Review VALUES (1, 5), (2, 3), (3, 'n/a'), (4, 4.5);
"""

LIBRARY_SCHEMA = """
CREATE TABLE Author (ID INTEGER PRIMARY KEY, Name TEXT);
CREATE TABLE Book (ID INTEGER PRIMARY KEY, AuthorID INTEGER REFERENCES Author(ID), Title TEXT, Pages INTEGER);
INSERT INTO Author VALUES (1, 'Eve'), (2, 'Fay');
INSERT INTO Book VALUES (1, 1, 'Alpha', 300), (2, 1, 'Beta', 120), (3, 2, 'Gamma', 450);
"""

QUESTIONS: list[dict[str, object]] = [
    {"question_id": 0, "db_id": "shop", "question": "How many customers are there?",
     "evidence": "", "SQL": "SELECT COUNT(*) FROM Customer", "difficulty": "simple"},
    {"question_id": 1, "db_id": "shop", "question": "List the names of customers in Paris.",
     "evidence": "Paris refers to City = 'Paris'", "SQL": "SELECT Name FROM Customer WHERE City = 'Paris'",
     "difficulty": "simple"},
    {"question_id": 2, "db_id": "shop", "question": "What is the sum of all order totals?",
     "evidence": "", "SQL": 'SELECT SUM(Total) FROM "Order Item"', "difficulty": "moderate"},
    {"question_id": 3, "db_id": "shop", "question": "What is the customer rank?",
     "evidence": "", "SQL": "SELECT Rank FROM Customer", "difficulty": "simple"},
    {"question_id": 4, "db_id": "shop", "question": "Which customers live in Atlantis?",
     "evidence": "", "SQL": "SELECT Name FROM Customer WHERE City = 'Atlantis'", "difficulty": "simple"},
    {"question_id": 5, "db_id": "shop", "question": "How many ratings are whole numbers?",
     "evidence": "whole number means an integer value",
     "SQL": "SELECT COUNT(*) FROM Review WHERE typeof(Rating) = 'integer'", "difficulty": "challenging"},
    {"question_id": 6, "db_id": "library", "question": "Who wrote the longest book?",
     "evidence": "", "difficulty": "moderate",
     "SQL": "SELECT T1.Name FROM Author AS T1 INNER JOIN Book AS T2 ON T1.ID = T2.AuthorID "
            "ORDER BY T2.Pages DESC LIMIT 1"},
    {"question_id": 7, "db_id": "library", "question": "How many books has each author written?",
     "evidence": "", "SQL": "SELECT AuthorID, COUNT(*) FROM Book GROUP BY AuthorID",
     "difficulty": "moderate"},
    {"question_id": 8, "db_id": "library", "question": "List every title.",
     "evidence": None, "SQL": "SELECT Title FROM Book", "difficulty": "simple"},
]

DESCRIPTION_CSV = (
    "original_column_name,column_name,column_description,data_format,value_description\n"
    "ID,id,customer identifier,integer,\n"
    "Name,name,full name,text,\n"
    "City,city,city of residence,text,e.g. Paris\n"
)


def _make_sqlite(path: Path, script: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(script)
        connection.commit()
    finally:
        connection.close()


def build_package(root: Path, *, nested: bool = True) -> Path:
    """Create the package under *root* (inside ``MINIDEV/`` when *nested*) and return *root*."""
    package = root / "MINIDEV" if nested else root
    package.mkdir(parents=True, exist_ok=True)
    (package / "mini_dev_sqlite.json").write_text(json.dumps(QUESTIONS), encoding="utf-8")
    databases = package / "dev_databases"
    _make_sqlite(databases / "shop" / "shop.sqlite", SHOP_SCHEMA)
    _make_sqlite(databases / "library" / "library.sqlite", LIBRARY_SCHEMA)
    descriptions = databases / "shop" / "database_description"
    descriptions.mkdir(parents=True, exist_ok=True)
    (descriptions / "Customer.csv").write_text(DESCRIPTION_CSV, encoding="utf-8")
    return root


def build_target(root: Path, schemas: tuple[str, ...] = ("shop", "library")) -> Database:
    """An empty SQLite file with one attached, empty file per schema (the stand-in for SQL Server)."""
    return build_database(root, "target", {schema: {} for schema in schemas})
