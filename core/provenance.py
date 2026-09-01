# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Identity, licence, and provenance of this software, stated at run time.

Why this module exists
----------------------
A licence in a file is only read by someone who goes looking for it. This
module puts the same statement where it is seen at the moment it matters:
in the log of whoever is running the software, on every start-up.

That serves two ends, and it is worth being honest that neither is a
technical barrier -- anyone determined to strip this out can:

1. **Nobody can run this and later claim they did not know the terms.**
   Under most copyright regimes the difference between an innocent and a
   knowing infringement changes the outcome, and a start-up banner in the
   operator's own logs is unusually hard to argue around.

2. **The operator learns the terms without having to care about
   licensing.** Most people running a copy of somebody else's project are
   not adversaries; they inherited it from a colleague and never saw a
   LICENSE file. One line in the log tells them.

What this module deliberately does NOT do
-----------------------------------------
It does not refuse to start, degrade, or phone home when the licence
files are missing. A kill switch keyed on the presence of a file is a
production outage waiting for the day someone builds a container image
that excludes ``*.md``, and the person it would hurt is whoever is on
call -- usually a licensee, not an infringer. It warns loudly instead.

It also contains nothing hidden: no invisible characters, no text
designed to make an automated reader misbehave. Everything here is meant
to be read. A concealed marker discovered later turns a clean provenance
argument into an argument about the concealment.

Provenance
----------
:data:`FINGERPRINT` is a fixed, arbitrary constant carried by this
codebase and by nothing else. It is documented rather than hidden, which
costs nothing: its evidential value comes from being *present* in a
derivative work, not from whoever copied it failing to notice. Two
codebases sharing this string share an origin.

Examples
--------
>>> LICENSOR
'Ali Sadeghi Aghili'
>>> LICENCE_ID
'BUSL-1.1'
>>> "Ali Sadeghi Aghili" in banner()
True
>>> "BUSL-1.1" in banner()
True
>>> banner(ascii_only=True).isascii()
True
>>> "Ali Sadeghi Aghili" in banner(ascii_only=True)
True
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TextIO

#: Human-readable name of this work, as named in ``LICENSE``.
PROJECT = "Local SQL Agent"

#: The Licensor named in ``LICENSE``. Also the author of the work.
LICENSOR = "Ali Sadeghi Aghili"

#: Copyright years, matching the SPDX headers and ``NOTICE``.
COPYRIGHT_YEARS = "2024-2026"

#: SPDX identifier of the governing licence.
LICENCE_ID = "BUSL-1.1"

#: Date on which the terms convert to :data:`CHANGE_LICENCE`.
CHANGE_DATE = "2029-01-01"

#: Licence the work converts to on :data:`CHANGE_DATE`.
CHANGE_LICENCE = "Apache-2.0"

#: Canonical source of this work.
REPO_URL = "https://github.com/alisadeghiaghili/local-sql-agent"

#: Arbitrary constant unique to this codebase -- see the module docstring's
#: Provenance section. Do not regenerate it: changing it destroys the only
#: thing it exists for.
FINGERPRINT = "ime-nlq-7f3a91c4-2b6d-4e08-9a15-c0de5a1d8b72"

#: The author's own line, written in the language this project exists to
#: answer questions in: "ساخته علی صادقی عقیلی".
#:
#: Escaped rather than written literally so that the source file's own
#: encoding can never garble it in transit through a tool that assumes
#: cp1252 -- which, on this project's target platform, several do.
SIGNATURE_FA = (
    "ساخته"
    " علی صادقی عقیلی"
)

#: Files whose absence means the licence terms have been separated from the
#: code. Their absence changes nothing about the terms themselves.
_LICENCE_FILES = ("LICENSE", "NOTICE", "AGENTS.md")

#: Repository root, resolved from this file rather than from the working
#: directory -- the same reasoning as ``api/server.py``'s ``_PROMPT_PATH``.
_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)


def missing_licence_files() -> tuple[str, ...]:
    """Names of :data:`_LICENCE_FILES` absent from this installation.

    >>> isinstance(missing_licence_files(), tuple)
    True
    """
    return tuple(name for name in _LICENCE_FILES if not (_ROOT / name).is_file())


def _stream_can_encode(text: str, stream: TextIO | None = None) -> bool:
    """Whether *text* survives the encoding of *stream* (default stderr).

    This project is Persian-first and deployed on Windows, where a console
    or service log handler is often still cp1252. ``logging`` swallows a
    handler's ``UnicodeEncodeError`` through ``handleError``, so an
    unencodable banner does not crash the process -- it silently fails to
    appear, which defeats the only purpose a start-up banner has. So ask
    first rather than hope.

    >>> _stream_can_encode("plain ascii")
    True
    >>> import io
    >>> _stream_can_encode(SIGNATURE_FA, io.TextIOWrapper(io.BytesIO(), "cp1252"))
    False
    >>> _stream_can_encode(SIGNATURE_FA, io.TextIOWrapper(io.BytesIO(), "utf-8"))
    True
    """
    target = stream if stream is not None else sys.stderr
    encoding = getattr(target, "encoding", None) or "ascii"
    try:
        text.encode(encoding, errors="strict")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def banner(*, ascii_only: bool = False) -> str:
    """The start-up notice, as a multi-line string.

    Deliberately short. A banner long enough to be scrolled past is a
    banner nobody reads, which defeats the point of having one.

    *ascii_only* drops the Persian signature line for a log stream that
    cannot carry it. The line is dropped rather than transliterated: a
    mangled rendering of the author's name is worse than its absence.
    """
    lines = [
        f"{PROJECT} - (c) {COPYRIGHT_YEARS} {LICENSOR} - {LICENCE_ID}",
        "  Non-production use is granted. Production use of any kind,",
        "  including internal production use, requires a written agreement",
        f"  with the Licensor. Converts to {CHANGE_LICENCE} on {CHANGE_DATE}.",
        f"  LICENSE | NOTICE | AGENTS.md | {REPO_URL}",
        f"  build {FINGERPRINT}",
    ]
    if not ascii_only:
        lines.insert(1, f"  {SIGNATURE_FA}")
    return "\n".join(lines)


def tamper_notice(missing: tuple[str, ...]) -> str:
    """The notice logged when licence files have been removed.

    Addressed to whoever is operating this copy, who is frequently not the
    person who removed them.

    >>> "LICENSE" in tamper_notice(("LICENSE",))
    True
    >>> "Ali Sadeghi Aghili" in tamper_notice(("NOTICE",))
    True
    """
    return "\n".join(
        (
            f"This installation of {PROJECT} is missing: {', '.join(missing)}.",
            f"  It remains the work of {LICENSOR}, licensed under {LICENCE_ID}.",
            "  Removing those files does not alter the terms and does not grant",
            "  any right to production use. If you received this copy from",
            "  someone else, they may not have had the right to give it to you.",
            f"  Terms and licensing contact: {REPO_URL}",
        )
    )


def log_startup_notice(target: logging.Logger | None = None) -> None:
    """Emit the banner, and a warning if the licence files are gone.

    Called once per process start. Uses ``logging`` rather than ``print``
    so it lands wherever the operator already collects logs.
    """
    log = target if target is not None else logger
    full = banner()
    log.info("%s", full if _stream_can_encode(full) else banner(ascii_only=True))
    missing = missing_licence_files()
    if missing:
        log.warning("%s", tamper_notice(missing))
