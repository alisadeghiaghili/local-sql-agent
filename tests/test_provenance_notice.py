# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The start-up notice must actually be emitted, not merely importable.

``tests/test_license_headers.py`` proves the SPDX headers and the licence
files are present. Nothing there proves the run-time notice fires: delete
the ``log_startup_notice()`` call from ``api/server.py``'s ``lifespan``
and every other test in this suite still passes, while the one thing the
notice exists for -- appearing in the operator's log -- is silently gone.

So these tests assert on captured log output from the real entry points,
not on the helper in isolation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from unittest.mock import patch

import pytest

from config import override_settings
from core.provenance import (
    FINGERPRINT,
    LICENCE_ID,
    LICENSOR,
    SIGNATURE_FA,
    banner,
    log_startup_notice,
    tamper_notice,
)

_VALID_DB = (
    "mssql+pyodbc://realuser:pw@dbhost.internal:1433/Auction_DM"
    "?driver=ODBC+Driver+17+for+SQL+Server"
)
_KEYS = json.dumps(
    [{"id": "p", "name": "P", "key_sha256": hashlib.sha256(b"k" * 40).hexdigest()}]
)


class TestNoticeReachesTheApiOperatorsLog:
    """The FastAPI entry point must emit it on every start."""

    def test_lifespan_logs_licensor_and_licence(self, caplog):
        import api.server as server_module

        server_module._system_prompt = "stub system prompt"

        async def _start():
            async with server_module.lifespan(server_module.app):
                pass

        with caplog.at_level(logging.INFO), override_settings(
            auth_required=True,
            api_keys_json=_KEYS,
            db_connection_url=_VALID_DB,
            openai_model="gpt-oss-20b",
        ):
            asyncio.run(_start())

        emitted = "\n".join(r.getMessage() for r in caplog.records)
        assert LICENSOR in emitted
        assert LICENCE_ID in emitted
        assert FINGERPRINT in emitted

    def test_notice_precedes_configuration_failure(self, caplog):
        """An operator whose config is broken must still see whose work
        this is. The notice is logged before validate() can raise, so a
        server that never starts successfully has still said so once."""
        import api.server as server_module

        async def _start():
            async with server_module.lifespan(server_module.app):
                pass  # pragma: no cover - config is invalid, never reached

        with caplog.at_level(logging.INFO), override_settings(
            db_connection_url="mssql+pyodbc://username@server:1433/Auction_DM"
            "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes",
            openai_model="llama3",
        ):
            with pytest.raises(RuntimeError):
                asyncio.run(_start())

        emitted = "\n".join(r.getMessage() for r in caplog.records)
        assert LICENSOR in emitted, "notice must be logged before config validation"


class TestNoticeReachesTheCliOperator:
    def test_cli_main_emits_the_notice_before_exiting_on_bad_config(self, caplog):
        import app as cli

        with caplog.at_level(logging.INFO), override_settings(
            db_connection_url="mssql+pyodbc://username@server:1433/Auction_DM"
            "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes",
            openai_model="llama3",
        ):
            with pytest.raises(SystemExit):
                cli.main()

        emitted = "\n".join(r.getMessage() for r in caplog.records)
        assert LICENSOR in emitted
        assert LICENCE_ID in emitted


class TestTamperWarning:
    def test_missing_licence_files_produce_a_warning_not_a_crash(self, caplog):
        """Removing the licence files must warn loudly and change nothing
        else. A licence check that refuses to start is a production
        outage aimed at whoever is on call -- see core/provenance.py."""
        import core.provenance as prov

        with caplog.at_level(logging.INFO), patch.object(
            prov, "missing_licence_files", return_value=("LICENSE", "NOTICE")
        ):
            prov.log_startup_notice()  # must not raise

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a stripped installation must produce a WARNING"
        text = "\n".join(r.getMessage() for r in warnings)
        assert "LICENSE" in text and "NOTICE" in text
        assert LICENSOR in text, "the warning must name whose work it is"

    def test_intact_installation_produces_no_warning(self, caplog):
        with caplog.at_level(logging.INFO):
            log_startup_notice()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestBannerSurvivesANonUtf8LogStream:
    """This project is Persian-first and deployed on Windows, where a log
    handler is often still cp1252. ``logging`` swallows a handler's
    ``UnicodeEncodeError``, so an unencodable banner would not crash --
    it would just never appear, defeating its only purpose."""

    def test_ascii_fallback_keeps_every_load_bearing_fact(self):
        fallback = banner(ascii_only=True)
        assert fallback.isascii()
        for required in (LICENSOR, LICENCE_ID, FINGERPRINT, "written agreement"):
            assert required in fallback

    def test_full_banner_carries_the_authors_own_line(self):
        assert SIGNATURE_FA in banner()
        assert SIGNATURE_FA not in banner(ascii_only=True)

    def test_tamper_notice_is_ascii_so_it_can_never_be_the_lost_one(self):
        """The tamper warning is the message most likely to be read by
        someone who should not have this copy. It must not be the one an
        encoding drops."""
        assert tamper_notice(("LICENSE",)).isascii()
