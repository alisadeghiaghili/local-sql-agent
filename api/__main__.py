# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Launcher for ``python -m api``.

Runs the exact same ASGI app the documented command runs
(``uvicorn api.server:app --host 0.0.0.0 --port 8000 --no-server-header``
— see ``docs/deployment-runbook.md`` step 4 and ``README.md``), but reads
the host and port from :class:`config.Settings` (``API_HOST`` /
``API_PORT``) instead of requiring them on the command line every time.
The port has always been settable with ``uvicorn``'s own ``--port`` flag;
it was never settable in ``.env``, which is where every other setting on
this page lives, so an operator who only ever edited ``.env`` had no way
to see or change it there.

Usage (from the repo root, same working directory the documented command
assumes)::

    python -m api

Equivalent to::

    uvicorn api.server:app --host <API_HOST> --port <API_PORT> --no-server-header

The documented plain ``uvicorn api.server:app ...`` command keeps working
completely unmodified: this is an additional entry point, not a
replacement, and uvicorn itself never reads ``API_HOST`` / ``API_PORT`` —
those are read here, through :mod:`config`, before ``uvicorn.run`` is
even called.
"""

from __future__ import annotations

import uvicorn

import config as cfg


def main() -> None:
    """Run the API with uvicorn, bound to ``cfg.settings.api_host`` /
    ``cfg.settings.api_port``, with the same ``server_header=False`` the
    documented command's ``--no-server-header`` flag sets.

    ``--no-server-header`` matters: ``SecurityHeadersMiddleware`` deletes
    an app-set ``Server`` header, but uvicorn appends its own
    ``Server: uvicorn`` at the protocol layer *after* the ASGI app
    returns, where no middleware can reach it — ``server_header=False``
    (the programmatic equivalent of the CLI flag, see uvicorn's own
    ``Config`` docs) is the only place that banner is actually suppressed
    on the wire.
    """
    uvicorn.run(
        "api.server:app",
        host=cfg.settings.api_host,
        port=cfg.settings.api_port,
        server_header=False,
    )


if __name__ == "__main__":  # pragma: no cover - real bind, exercised via main()'s own test instead
    main()
