# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for ``scripts/issue_api_key.py``.

Two things are covered, and they are not the same kind of thing.

The first is ordinary: the entry this script prints must be one
``security.auth._parse_api_keys`` accepts, with exactly the capabilities
that were asked for and no others. Those tests round-trip the entry
through the real parser rather than asserting on dict keys, so a change
to either side that breaks the pairing fails here rather than at a
deployment's first start-up.

The second is unusual, and needs saying: this module asserts on the
*shape of printed output*. Normally that is a brittle test of a cosmetic
detail. Here the layout is the feature. Version 4.6.1 added a 401 hint
for operators who had pasted the digest into the browser's key field,
having correctly identified the JSON entry as "the conspicuous,
copy-pasteable artefact" -- and the reason it was conspicuous is that
this script printed both values as equally-weighted flat lines, such that
operators did not register that a second value existed at all. A
regression that quietly re-flattens the output would restore that
failure, and no other test in this suite would notice: every consumer of
this script is a human reading a terminal.

So the assertions are deliberately about *relative prominence and
ordering* -- the raw key appears before the JSON, framed, labelled with
where it goes -- rather than about exact strings, which would break on
any wording change without catching anything.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.issue_api_key import (
    _RULE,
    _capability_warnings,
    build_entry,
    issue_key,
    main,
)
from security.auth import _parse_api_keys


# ---------------------------------------------------------------------------
# The entry itself
# ---------------------------------------------------------------------------

class TestBuildEntry:
    def test_entry_carries_the_digest_and_never_the_raw_key(self):
        raw_key = issue_key()
        entry = build_entry("analyst-1", "Analyst One", raw_key)
        assert entry["key_sha256"] == hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        assert raw_key not in json.dumps(entry), (
            "the raw key leaked into the entry that gets pasted into .env -- "
            "the one place this module's whole design says it never goes"
        )

    def test_an_ordinary_analyst_key_carries_no_capability_fields(self):
        """Absent means false. An analyst entry must not acquire three
        explicit ``false`` fields just because the flags now exist."""
        entry = build_entry("analyst-1", "Analyst One", issue_key())
        assert set(entry) == {"id", "name", "key_sha256"}

    @pytest.mark.parametrize(
        "kwargs, expected",
        [
            ({"admin": True}, {"admin"}),
            ({"operations": True}, {"operations"}),
            ({"security": True}, {"security"}),
            ({"admin": True, "operations": True}, {"admin", "operations"}),
            (
                {"admin": True, "operations": True, "security": True},
                {"admin", "operations", "security"},
            ),
        ],
    )
    def test_capabilities_round_trip_through_the_real_parser(self, kwargs, expected):
        """The parser is the authority on what these fields mean, so the
        assertion is on the ``Principal`` it produces -- not on the dict."""
        raw_key = issue_key()
        entry = build_entry("admin-1", "Admin", raw_key, **kwargs)
        principals = _parse_api_keys(json.dumps([entry]))
        principal = principals[entry["key_sha256"]]
        assert principal.capabilities == frozenset(expected)

    def test_denied_columns_survive_alongside_capabilities(self):
        entry = build_entry(
            "broker", "Broker", issue_key(), ["NationalID"], admin=True, security=True,
        )
        principal = _parse_api_keys(json.dumps([entry]))[entry["key_sha256"]]
        assert principal.denied_columns == ("NationalID",)
        assert principal.capabilities == frozenset({"admin", "security"})


# ---------------------------------------------------------------------------
# The partial-capability warnings
# ---------------------------------------------------------------------------

class TestCapabilityWarnings:
    """The shape that surprises operators is not the missing flag but the
    partial one: the admin panel's ten sections split across
    ``require_admin`` (four) and ``require_operations_or_security`` (six),
    so either half alone renders a panel that is more than a third 403 --
    which reads as a broken deployment rather than a decision someone made
    at issue time."""

    def test_an_ordinary_analyst_key_is_not_warned_about(self):
        assert _capability_warnings(False, False, False) == []

    def test_a_complete_admin_is_not_warned_about(self):
        assert _capability_warnings(True, True, True) == []

    def test_admin_with_either_half_is_not_warned_about(self):
        assert _capability_warnings(True, True, False) == []
        assert _capability_warnings(True, False, True) == []

    def test_admin_alone_is_warned_about(self):
        [warning] = _capability_warnings(True, False, False)
        assert "403" in warning
        assert "--full-admin" in warning, (
            "the warning names the problem without naming the one flag that "
            "fixes it -- half a diagnosis"
        )

    def test_operations_without_admin_is_warned_about(self):
        [warning] = _capability_warnings(False, True, False)
        assert "403" in warning
        assert "--admin" in warning

    def test_security_without_admin_is_warned_about(self):
        [warning] = _capability_warnings(False, False, True)
        assert "403" in warning


# ---------------------------------------------------------------------------
# The printed output -- see this module's docstring for why this is tested
# ---------------------------------------------------------------------------

def _run(capsys, *argv: str) -> str:
    assert main(list(argv)) == 0
    return capsys.readouterr().out


class TestTheRawKeyIsNotMissable:
    def test_the_raw_key_comes_before_the_json_entry(self, capsys):
        """Reading order is the whole point: the value that must be copied
        *now* cannot be printed after the value that must be copied
        later."""
        out = _run(capsys, "--id", "analyst-1", "--name", "Analyst One")
        assert out.index("key_sha256") > out.index(_RULE), (
            "the JSON entry was printed before the framed raw-key block -- "
            "the artefact that scrolls the un-recoverable one off screen "
            "now comes first"
        )

    def test_the_raw_key_is_framed_and_the_json_entry_is_not(self, capsys):
        out = _run(capsys, "--id", "analyst-1", "--name", "Analyst One")
        framed, _, after = out.partition("key_sha256")
        assert framed.count(_RULE) >= 2, "the raw key is not inside a frame"
        assert _RULE not in after, (
            "the JSON entry is framed too, which restores exactly the "
            "equal-weighting this layout exists to remove"
        )

    def test_the_output_says_where_each_value_goes(self, capsys):
        """A reader who has already made the mistake once needs the
        destination, not just the value."""
        out = _run(capsys, "--id", "analyst-1", "--name", "Analyst One")
        assert ".env" in out
        assert "401" in out, (
            "nothing tells the reader what pasting the digest into the "
            "browser actually does, which is the mistake being prevented"
        )

    def test_the_raw_key_appears_exactly_once(self, capsys):
        """Printed twice, it is twice as likely to end up in a scrollback
        someone screenshots -- and the 'shown exactly once' promise in the
        surrounding text would be false."""
        out = _run(capsys, "--id", "analyst-1", "--name", "Analyst One")
        entry = json.loads(out[out.index("{"): out.rindex("}") + 1])
        raw_keys = [
            line.strip() for line in out.splitlines()
            if line.strip()
            and hashlib.sha256(line.strip().encode()).hexdigest() == entry["key_sha256"]
        ]
        assert len(raw_keys) == 1

    def test_the_printed_entry_is_valid_for_the_real_parser(self, capsys):
        """What is on screen is what gets pasted, so the parser must accept
        the printed text -- not merely the dict it came from."""
        out = _run(capsys, "--id", "admin-1", "--name", "Admin", "--full-admin")
        entry_json = out[out.index("{"): out.rindex("}") + 1]
        principals = _parse_api_keys(f"[{entry_json}]")
        [principal] = principals.values()
        assert principal.capabilities == frozenset({"admin", "operations", "security"})


class TestFullAdminFlag:
    def test_full_admin_grants_all_three(self, capsys):
        out = _run(capsys, "--id", "admin-1", "--name", "Admin", "--full-admin")
        entry = json.loads(out[out.index("{"): out.rindex("}") + 1])
        assert entry["admin"] is True
        assert entry["operations"] is True
        assert entry["security"] is True

    def test_full_admin_produces_no_partial_capability_warning(self, capsys):
        out = _run(capsys, "--id", "admin-1", "--name", "Admin", "--full-admin")
        assert "NOTE:" not in out

    def test_full_admin_composes_with_an_individual_flag(self, capsys):
        """``--full-admin --security`` is redundant, not contradictory --
        it must not double-grant or fail."""
        out = _run(capsys, "--id", "a", "--name", "A", "--full-admin", "--security")
        entry = json.loads(out[out.index("{"): out.rindex("}") + 1])
        assert entry["security"] is True
        assert "NOTE:" not in out
