# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Issue a new API key for Phase 8 authentication.

Usage (from repo root)::

    python scripts/issue_api_key.py --id analyst-1 --name "Jane Analyst"
    python scripts/issue_api_key.py --id readonly-broker --name "Broker Desk" \\
        --denied-column NationalID --denied-column Phone
    python scripts/issue_api_key.py --id ops-1 --name "Ops Admin" --admin

Prints the raw key to stdout **exactly once** — it is not recoverable
afterwards, because only its SHA-256 hex digest is ever configured or
stored (see ``security/auth.py``'s module docstring for why plain
SHA-256, not bcrypt/argon2, is the correct primitive here). Also prints
the ``API_KEYS_JSON`` entry to paste into that variable's array.

The raw key is never written to a file or to any log — only to this
process's own stdout, for the operator to copy immediately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys

#: Minimum raw key length -- mirrors security.auth.MIN_KEY_LENGTH. Not
#: imported from there to keep this a standalone script with no
#: application import (and therefore no config.py / dotenv / database
#: dependency chain) required just to mint a key.
MIN_KEY_LENGTH = 32


def issue_key(min_length: int = MIN_KEY_LENGTH) -> str:
    """Return a fresh, high-entropy raw API key.

    ``secrets.token_urlsafe(32)`` produces 43 URL-safe base64 characters
    from 32 random bytes (256 bits of entropy) — comfortably over
    *min_length*, which exists as a floor this function asserts against
    rather than a target it tunes towards.
    """
    raw = secrets.token_urlsafe(32)
    assert len(raw) >= min_length, (
        f"generated key is only {len(raw)} chars, below the {min_length}-char "
        "minimum -- this should not happen with token_urlsafe(32); refusing "
        "to issue a weak key"
    )
    return raw


def build_entry(
    principal_id: str,
    name: str,
    raw_key: str,
    denied_columns: list[str] | None = None,
    admin: bool = False,
) -> dict:
    """The ``API_KEYS_JSON`` array entry for *raw_key* -- never the raw key itself.

    *admin* grants the admin panel's phase 1 read-only observability
    capability (``docs/admin-panel-architecture.md`` §2;
    ``security.auth.Principal.is_admin``) -- omitted from the entry
    entirely when ``False`` (the default), matching *denied_columns*'s own
    "absent means false/none" convention rather than writing out an
    explicit ``"admin": false`` on every ordinary analyst key.
    """
    entry: dict = {
        "id": principal_id,
        "name": name,
        "key_sha256": hashlib.sha256(raw_key.encode("utf-8")).hexdigest(),
    }
    if denied_columns:
        entry["denied_columns"] = denied_columns
    if admin:
        entry["admin"] = True
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Issue a new Phase 8 API key. Prints the raw key once.",
    )
    parser.add_argument("--id", required=True, help="Stable principal id (e.g. 'analyst-1')")
    parser.add_argument("--name", required=True, help="Human-readable label for logs/audit")
    parser.add_argument(
        "--denied-column",
        action="append",
        dest="denied_columns",
        default=[],
        help="Column name this principal must never see. Repeatable.",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help=(
            "Grant the admin panel's phase 1 read-only observability "
            "capability (docs/admin-panel-architecture.md). Omitted by "
            "default -- an ordinary analyst key gets no admin surface."
        ),
    )
    args = parser.parse_args(argv)

    raw_key = issue_key()
    entry = build_entry(args.id, args.name, raw_key, args.denied_columns, admin=args.admin)

    print("Raw API key (copy this now -- it will not be shown again):")
    print(f"  {raw_key}")
    print()
    print("API_KEYS_JSON entry to add to that variable's array:")
    print(f"  {json.dumps(entry, ensure_ascii=False)}")
    print()
    print(
        "This process never wrote the raw key to a file or log -- only to "
        "this terminal. Store it in your secrets manager, not in git."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
