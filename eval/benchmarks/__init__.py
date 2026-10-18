# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Generated benchmarks for the evaluation harness.

:mod:`eval.benchmarks.retrieval_synth` builds a synthetic warehouse, its
``project_config`` and a golden set from a seed, to measure table retrieval
(``python -m eval.cli recall``) at a scale no committed example reaches.
:mod:`eval.benchmarks.vocabulary` is the generic word list it draws on.

The benchmark track (runbook 18.7) runs a *public* text-to-SQL benchmark through the
production pipeline on a database of the operator's own. It is a second instrument
beside the private golden set and its figure is never the deployment's accuracy:
:mod:`eval.benchmarks.sqlite_load` copies a SQLite database into one SQL Server
schema, :mod:`eval.benchmarks.bird` reads BIRD mini-dev and decides, per question,
whether it is scored, :mod:`eval.benchmarks.bird_import` is ``bench-import``,
:mod:`eval.benchmarks.bird_config` drafts a ``project_config/`` per database and
:mod:`eval.benchmarks.bird_run` is ``bench-run``.
"""

from __future__ import annotations
