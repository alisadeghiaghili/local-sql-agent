# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Shared pieces for the test doubles that stand in for ``WizardLLM``.

The wizard asks for aliases through ``WizardLLM.generate_aliases``. A double
that answers by prompt in its own ``generate`` gets that call from
:class:`AliasesViaGenerate`, so one definition of "the aliases of this answer"
(``AliasAnswer.from_parsed``) serves all of them.
"""

from __future__ import annotations

from core.wizard_drafts import ALIAS_LIMIT
from llm.wizard_llm import AliasAnswer


class AliasesViaGenerate:
    """Gives a wizard-model double ``generate_aliases`` on top of its own ``generate``."""

    def generate_aliases(self, prompt: str, *, limit: int = ALIAS_LIMIT) -> AliasAnswer:
        return AliasAnswer.from_parsed(self.generate(prompt, expect_json=True), limit)
