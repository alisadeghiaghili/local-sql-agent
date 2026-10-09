# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Generated benchmarks for the evaluation harness.

:mod:`eval.benchmarks.retrieval_synth` builds a synthetic warehouse, its
``project_config`` and a golden set from a seed, to measure table retrieval
(``python -m eval.cli recall``) at a scale no committed example reaches.
:mod:`eval.benchmarks.vocabulary` is the generic word list it draws on.
"""

from __future__ import annotations
