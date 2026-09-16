# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression tests for the 2026-09-10 security audit.

Every module here encodes one finding from that audit. Each was written
*before* the fix and failed at the time of writing -- that is the point:
a security test that never failed has not been shown to test anything.

The audit ran in five passes (four code reviews plus a live penetration
test against a running server). Where a finding was proven live, the
module docstring quotes the observed response rather than paraphrasing
it, so a future reader can tell a reproduced defect from a suspected one.
"""
