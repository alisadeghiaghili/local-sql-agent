# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Make command-line programs print Persian on a Windows console.

Why this module exists
----------------------
Reports, prompts and error messages carry Persian questions, table names
and aliases. When a program's output is not an interactive console -- a
pipe, a redirect (``> out.txt``), PowerShell's ``|`` -- Python encodes
``sys.stdout`` with the legacy code page of the machine: cp1252, cp1256,
cp437 and so on. None of those can encode every Persian letter (cp1252 and
cp437 hold none of them), so ``print`` raises ``UnicodeEncodeError``
part-way through the output, after some of it has already been written.
Setting ``PYTHONIOENCODING`` or ``PYTHONUTF8`` in the shell avoids it, but
nobody should have to know that to run a script.

:func:`use_utf8_console` is the one place that fixes it: every command-line
entry point calls it first thing when it runs as a program.

Where it must be called
-----------------------
At the start of the ``if __name__ == "__main__":`` block, not inside
``main()``. Tests call ``main()`` in-process with captured streams (pytest's
``capsys``, ``contextlib.redirect_stdout``); reconfiguring those would
change what the test captures and, for a stream pytest still owns, can
fail. The ``__main__`` block only ever runs when the file is the program,
which is exactly when the real console is attached.
``tests/test_cli_utf8_console.py`` fails if a new entry point forgets.

Not applied to the web server
-----------------------------
``python -m api`` and the demo server hand their streams to uvicorn and the
logging system, which have their own handling; neither is touched.
"""

from __future__ import annotations

import sys

__all__ = ["use_utf8_console"]


def use_utf8_console() -> None:
    r"""Make ``sys.stdout`` and ``sys.stderr`` write UTF-8.

    Each stream that has a ``reconfigure`` method (an
    :class:`io.TextIOWrapper`, which is what the interpreter installs) is
    switched to UTF-8. A stream without one -- ``None`` under ``pythonw``,
    a test double, a custom writer -- is left as it is.

    The ``errors`` handler is **preserved, not reset**. Python resets it to
    ``"strict"`` whenever ``reconfigure`` is given an encoding on its own,
    and that would quietly make two streams stricter than the interpreter
    made them: ``sys.stderr`` is ``backslashreplace`` by default (so that
    reporting an error can never raise another one), and ``sys.stdout`` is
    ``surrogateescape`` under the POSIX ``C`` locale. In the case this
    function exists for -- a Windows console -- ``stdout`` is ``strict``
    and stays ``strict``: UTF-8 can encode every Persian string, so the
    handler only ever matters for a lone surrogate, which is a bug worth
    seeing rather than hiding behind ``replace``.

    Args:
        None: the streams are read from :mod:`sys` at call time, so a
            caller that has replaced ``sys.stdout`` is respected.

    Returns:
        None.

    Raises:
        Nothing: ``ValueError`` (the stream has already been read from, so
        its encoding can no longer change) and ``OSError`` (the stream is
        detached or closed) are swallowed and that stream is left as it
        was. Failing to switch the encoding must never stop a program that
        would otherwise have run.

    Examples:
        A stream without ``reconfigure`` is skipped, a cp1252 stream is
        switched to UTF-8, and the ``errors`` handler it already had is
        kept:

        >>> import io
        >>> buffer = io.BytesIO()
        >>> stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="backslashreplace")
        >>> saved = sys.stdout, sys.stderr
        >>> sys.stdout, sys.stderr = stream, object()
        >>> try:
        ...     result = use_utf8_console()
        ... finally:
        ...     sys.stdout, sys.stderr = saved
        >>> result is None
        True
        >>> stream.encoding, stream.errors
        ('utf-8', 'backslashreplace')

        Persian now encodes (cp1252 holds none of these letters):

        >>> print("\u0633\u0644\u0627\u0645", file=stream)
        >>> stream.flush()
        >>> buffer.getvalue()
        b'\xd8\xb3\xd9\x84\xd8\xa7\xd9\x85\n'
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        errors = getattr(stream, "errors", None) or "strict"
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except (ValueError, OSError):  # already read from, or detached/closed
            continue
