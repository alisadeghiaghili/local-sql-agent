"""Generate sample.db for local testing.

Usage (from repo root)::

    python scripts/create_db.py

Creates ``sample.db`` in the current working directory.
The file is excluded from git via .gitignore.
"""

from __future__ import annotations

import os
import sqlite3

DB_PATH = os.getenv("SQLITE_DB_PATH", "sample.db")


def create_sample_db(path: str = DB_PATH) -> None:
    conn = sqlite3.connect(path)
    c    = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS Artist (
            ArtistId INTEGER PRIMARY KEY,
            Name     TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS Album (
            AlbumId  INTEGER PRIMARY KEY,
            Title    TEXT    NOT NULL,
            ArtistId INTEGER NOT NULL,
            FOREIGN KEY (ArtistId) REFERENCES Artist(ArtistId)
        );
        CREATE TABLE IF NOT EXISTS Track (
            TrackId      INTEGER PRIMARY KEY,
            Name         TEXT    NOT NULL,
            AlbumId      INTEGER NOT NULL,
            Composer     TEXT,
            Milliseconds INTEGER,
            Bytes        INTEGER,
            UnitPrice    REAL    NOT NULL,
            FOREIGN KEY (AlbumId) REFERENCES Album(AlbumId)
        );
    """)
    artists = [(1, "AC/DC"), (2, "Accept"), (3, "Aerosmith")]
    albums  = [(1, "For Those About To Rock", 1), (2, "Balls to the Wall", 2), (3, "Rocks", 3)]
    tracks  = [
        (1, "For Those About To Rock", 1, "Young",    343719, 11170334, 0.99),
        (2, "Put The Finger On You",   1, "Young",    205662,  6713451, 0.99),
        (3, "Balls to the Wall",       2, None,       342562, 10011559, 0.99),
        (4, "Fast As a Shark",         2, "Kaufmann", 230619,  3990994, 0.99),
        (5, "Dream On",                3, "Tyler",    280863,  9306432, 0.99),
    ]
    c.executemany("INSERT OR IGNORE INTO Artist VALUES (?, ?)",                artists)
    c.executemany("INSERT OR IGNORE INTO Album  VALUES (?, ?, ?)",             albums)
    c.executemany("INSERT OR IGNORE INTO Track  VALUES (?, ?, ?, ?, ?, ?, ?)", tracks)
    conn.commit()
    conn.close()
    print(f"✅  sample.db created at: {os.path.abspath(path)}")


if __name__ == "__main__":
    create_sample_db()
