# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Issue a new API key for Phase 8 authentication.

Usage (from repo root)::

    python -m scripts.issue_api_key --id analyst-1 --name "Jane Analyst"
    python -m scripts.issue_api_key --id readonly-broker --name "Broker Desk" \\
        --denied-column NationalID --denied-column Phone
    python -m scripts.issue_api_key --id admin-1 --name "Ops Admin" --full-admin

Prints the raw key to stdout **exactly once** — it is not recoverable
afterwards, because only its SHA-256 hex digest is ever configured or
stored (see ``security/auth.py``'s module docstring for why plain
SHA-256, not bcrypt/argon2, is the correct primitive here). Also prints
the ``API_KEYS_JSON`` entry to paste into that variable's array.

The raw key is never written to a file or to any log — only to this
process's own stdout, for the operator to copy immediately.

Why the output is laid out the way it is
----------------------------------------
This script emits two strings, and they go to two different places: the
raw key to a browser field, the JSON entry to ``.env``. Version 4.6.1
added a 401 hint for operators who pasted the digest into the key field,
on the reasoning that the JSON entry is "the conspicuous, copy-pasteable
artefact". That reasoning was right and the fix was aimed one step too
late: the entry is conspicuous *because this script made it so*, printing
both values as equally-weighted flat lines under similar-looking
headings. Operators reported not registering that a second value existed
at all.

So the raw key is now framed, indented, and labelled with its length and
its destination, and the two values are numbered as sequential steps
rather than presented as a list of outputs. :data:`_RULE` and
:func:`_emit_key_block` exist for that and nothing else. This is
deliberately plain ASCII with no colour: the output is routinely piped,
redirected, and pasted into tickets and chat, where escape codes become
noise and box-drawing characters become mojibake on a Windows console
that has not been switched to UTF-8.

Capability flags
----------------
:data:`_CAPABILITY_FLAGS` maps the three ``--`` flags to the
``API_KEYS_JSON`` fields ``security/auth.py`` parses. All three have been
parseable since the admin panel's phase 2, but only ``--admin`` was
issuable here — so granting the other two meant hand-editing JSON, which
is exactly the "raw key and digest get confused" territory this script
exists to keep operators out of.

The three are not a ladder, and :func:`_capability_warnings` exists
because the shape that surprises people is not the missing flag but the
*partial* one. The admin panel's ten sections span three requirements:
``require_admin`` serves four of them, ``require_operations_or_security``
the other six. A key holding only ``admin`` therefore loads a panel where
more than half the sections show 403 — which reads as a broken deployment,
not as a permissions decision someone made at issue time. ``--full-admin``
is the one-flag answer for a single-operator deployment; the individual
flags stay for the two-role split the architecture document describes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import textwrap

#: Minimum raw key length -- mirrors security.auth.MIN_KEY_LENGTH. Not
#: imported from there to keep this a standalone script with no
#: application import (and therefore no config.py / dotenv / database
#: dependency chain) required just to mint a key.
MIN_KEY_LENGTH = 32

#: Width of the framing rules around the raw-key block. 72 columns fits
#: an 80-column terminal with room to spare and survives being quoted
#: into an email or a ticket without re-wrapping.
_RULE = "=" * 72

#: ``(flag_dest, json_field, one-line description)`` for the three
#: capabilities ``security.auth._parse_api_keys`` understands. Ordered
#: as the admin panel's architecture document introduces them: phase 1's
#: read-only ``admin``, then phase 2's two-role split.
_CAPABILITY_FLAGS: tuple[tuple[str, str, str], ...] = (
    ("admin", "admin", "read-only observability (audit, health, cache, config)"),
    ("operations", "operations", "key lifecycle, maintenance mode, domain knowledge"),
    ("security", "security", "column ACLs, schema.yaml, granting roles"),
)


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
    operations: bool = False,
    security: bool = False,
) -> dict:
    """The ``API_KEYS_JSON`` array entry for *raw_key* -- never the raw key itself.

    *admin*, *operations* and *security* grant the three capabilities
    ``security.auth._parse_api_keys`` understands
    (``docs/admin-panel-architecture.md`` §2). Each is omitted from the
    entry entirely when ``False`` (the default), matching
    *denied_columns*'s own "absent means false/none" convention rather
    than writing out three explicit ``false`` fields on every ordinary
    analyst key.

    The parser rejects a non-boolean for any of the three, so a hand-typed
    ``"admin": "false"`` fails loudly at start-up rather than granting the
    capability by truthiness. Nothing here needs to defend against that —
    :func:`json.dumps` cannot emit a Python ``bool`` as anything but a
    JSON boolean — but it is the reason these stay real booleans rather
    than being stringified for readability.
    """
    entry: dict = {
        "id": principal_id,
        "name": name,
        "key_sha256": hashlib.sha256(raw_key.encode("utf-8")).hexdigest(),
    }
    if denied_columns:
        entry["denied_columns"] = denied_columns
    for granted, field in ((admin, "admin"), (operations, "operations"), (security, "security")):
        if granted:
            entry[field] = True
    return entry


def _capability_warnings(admin: bool, operations: bool, security: bool) -> list[str]:
    """Warnings for capability combinations that load a partly-403 admin panel.

    Returned rather than printed so the wording is testable without
    capturing stdout. Empty for every combination that is coherent:
    an ordinary analyst key (none of the three), and a key holding
    ``admin`` alongside at least one of the other two.

    The two warned-about shapes are both *silent* failures at issue time
    and *confusing* ones later — the panel renders, and then individual
    sections report a permissions error that looks like a bug in the
    deployment. See the module docstring for the route-by-route split.
    """
    warnings: list[str] = []
    if admin and not (operations or security):
        warnings.append(
            "This key has 'admin' but neither 'operations' nor 'security'. It "
            "can load the admin panel's audit, deployment-checks, cache and "
            "domain-config sections -- and will get 403 on the other six "
            "(maintenance, feedback, schema drift, vocabulary, per-analyst "
            "usage, auth failures). Add --operations and/or --security, or "
            "use --full-admin, unless a read-only key is what you meant."
        )
    if (operations or security) and not admin:
        held = " and ".join(n for n, on in (("operations", operations), ("security", security)) if on)
        warnings.append(
            f"This key has '{held}' but not 'admin'. It can perform "
            "administrative writes, but the admin panel's audit, "
            "deployment-checks, cache and domain-config sections require "
            "'admin' and will return 403. Add --admin, or use --full-admin."
        )
    return warnings


def _emit_key_block(raw_key: str) -> None:
    """Print the raw key framed, indented and labelled with its destination.

    Split out from :func:`main` so the framing is one thing rather than
    six adjacent ``print`` calls in the middle of unrelated output. See
    the module docstring for why this is framed at all, and why in plain
    ASCII.
    """
    print(_RULE)
    print("  STEP 1 OF 2 -- THE RAW KEY. COPY IT NOW.")
    print("  This is shown exactly once and is never recoverable.")
    print(_RULE)
    print()
    print(f"      {raw_key}")
    print()
    print(f"  ^ {len(raw_key)} characters. This is what a PERSON pastes into the")
    print("    key field at the top of the web UI or the admin panel.")
    print("    It does NOT go in .env.")
    print(_RULE)


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
    for dest, _field, description in _CAPABILITY_FLAGS:
        parser.add_argument(
            f"--{dest}",
            action="store_true",
            help=(
                f"Grant the '{dest}' capability -- {description} "
                "(docs/admin-panel-architecture.md §2). Omitted by default: "
                "an ordinary analyst key gets no admin surface."
            ),
        )
    parser.add_argument(
        "--full-admin",
        action="store_true",
        help=(
            "Grant all three of --admin, --operations and --security -- every "
            "section of the admin panel, for a deployment with a single "
            "operator. Prefer the individual flags where the two-role split "
            "is actually being used."
        ),
    )
    args = parser.parse_args(argv)

    admin = args.admin or args.full_admin
    operations = args.operations or args.full_admin
    security = args.security or args.full_admin

    raw_key = issue_key()
    entry = build_entry(
        args.id,
        args.name,
        raw_key,
        args.denied_columns,
        admin=admin,
        operations=operations,
        security=security,
    )

    _emit_key_block(raw_key)
    print()
    print("  STEP 2 OF 2 -- add this entry to the API_KEYS_JSON array in .env,")
    print("  then restart the server. This is the key's SHA-256 DIGEST, not")
    print("  the key: pasting it into the browser gets a 401.")
    print()
    print(f"    {json.dumps(entry, ensure_ascii=False)}")
    print()

    granted = [field for _dest, field, _desc in _CAPABILITY_FLAGS if entry.get(field)]
    print(f"  Capabilities: {', '.join(granted) if granted else 'none (ordinary analyst key)'}")
    if args.denied_columns:
        print(f"  Denied columns: {', '.join(args.denied_columns)}")
    print()

    for warning in _capability_warnings(admin, operations, security):
        # Wrapped here rather than in _capability_warnings so that
        # function returns one sentence per warning, comparable in a test
        # without reasoning about where the line breaks landed.
        print(
            textwrap.fill(
                f"NOTE: {warning}",
                width=len(_RULE),
                initial_indent="  ",
                subsequent_indent="        ",
            )
        )
        print()

    print(
        "  This process never wrote the raw key to a file or log -- only to\n"
        "  this terminal. Store it in your secrets manager, not in git."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
