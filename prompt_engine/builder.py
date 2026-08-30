"""Assemble the final LLM prompt from retrieval context and schema.

This module contains a single static class, :class:`PromptBuilder`, whose
``build()`` method combines every piece of retrieved knowledge — schema,
relationships, business rules, few-shot examples, and value filters — into
a single structured string ready for the LLM backend.

Two assembly paths, one gate
-----------------------------
As of Phase 2 (latency), ``build()`` no longer always re-retrieves and
re-assembles prompt content per question. It picks one of two paths via
:func:`prompt_engine.static_prefix.should_use_static_prefix`:

* **Static path** (the default for today's 12-table schema) — a
  byte-identical prefix (system prompt, full schema, all relationships,
  all business rules, all metrics, all examples) built once and cached by
  :mod:`prompt_engine.static_prefix`, followed by a small variable suffix
  (detected filters, session context, the question). Identical prefix
  bytes across requests is what lets llama.cpp/vLLM reuse its KV cache
  instead of re-running prefill over the whole prompt every time —
  ``docs/api-contract-v2.md`` §8.
* **Retrieval path** (the scaling escape hatch) — the original
  per-question behaviour: only the tables the six-retriever pipeline
  matched go into the prompt. Used automatically once the knowledge base
  grows past ``cfg.settings.prompt_retrieval_token_budget``.

Typical usage::

    from retrieval.context_retriever import ContextRetriever
    from prompt_engine.builder import PromptBuilder

    context = ContextRetriever.retrieve("فروش ماهانه مشتریان")
    prompt  = PromptBuilder.build(
        question="فروش ماهانه مشتریان",
        system_prompt=system_prompt_text,
        context=context,
    )
"""

from __future__ import annotations

from core.models import RetrievalContext
from prompt_engine.static_prefix import build_static_prefix, should_use_static_prefix
from prompt_engine.templates import PROMPT_TEMPLATE, SUFFIX_TEMPLATE
from schema_data.registry import SchemaRegistry


class PromptBuilder:
    """Stateless factory that converts a :class:`~core.models.RetrievalContext` into a prompt string.

    All methods are static — there is no instance state.  The class exists
    purely as a namespace to keep prompt-assembly logic together.

    Design notes
    ------------
    * :meth:`build` dispatches to :meth:`build_static` or
      :meth:`_build_retrieval` based on
      :func:`~prompt_engine.static_prefix.should_use_static_prefix` — see
      the module docstring.
    * :meth:`_build_retrieval` (the fallback path) uses
      ``context.selected_tables`` — the order-preserving property on
      :class:`~core.models.RetrievalContext` — rather than deduplicating
      ``context.entities + context.facts`` with a bare ``set()``. A plain
      ``set()`` does not preserve insertion order, which made table order
      in the prompt vary between runs with the same input for no reason;
      this was flagged in this module's own prior docstring but never
      fixed until now.
    * :meth:`~schema_data.registry.SchemaRegistry.build_schema_context` is
      called with the deduplicated table list so the schema block contains
      only the columns relevant to the question (retrieval path), or with
      ``None`` for every table (static path).
    * Empty sections (no rules, no examples, no filters) produce empty strings
      rather than placeholder text — the template handles missing sections
      gracefully without printing ``"None"`` or ``"[]"``.
    """

    @staticmethod
    def build(
        question: str,
        system_prompt: str,
        context: RetrievalContext,
        *,
        session_context: str = "",
    ) -> str:
        """Build a complete prompt string for the LLM backend.

        Parameters
        ----------
        question:
            The original natural-language question in Persian or English.
        system_prompt:
            Domain-specific system instructions loaded from
            ``prompts/system_prompt.md`` at server startup.
        context:
            Fully populated :class:`~core.models.RetrievalContext` produced
            by :class:`~retrieval.context_retriever.ContextRetriever`. Only
            ``context.filters`` is used on the static path (see the module
            docstring); every field is used on the retrieval fallback path.
        session_context:
            Prior-turn context for a conversational session (see
            ``docs/api-contract-v2.md`` §8's "session context" block —
            question, SQL, and result *column names* for the last
            ``session_prompt_turns`` turns; never row data). Empty string
            when there is no session, which is every call today —
            sessions are not yet implemented. Always placed in the
            variable suffix, never the static prefix.

        Returns
        -------
        str
            A ready-to-send prompt string.

        Examples
        --------
        On the static path (today's schema), the prompt always contains
        every known table and every configured example — ``context``'s own
        ``entities``/``examples`` only matter on the retrieval fallback
        path (see :meth:`_build_retrieval`):

        >>> from core.models import RetrievalContext
        >>> ctx = RetrievalContext(
        ...     entities=["Customer"],
        ...     facts=["Contract"],
        ...     filters={"PersianYear": 1402},
        ... )
        >>> prompt = PromptBuilder.build("خرید مشتریان در ۱۴۰۲", "You are a T-SQL expert.", ctx)
        >>> "Table: Customer" in prompt
        True
        >>> "1402" in prompt
        True

        Forcing the retrieval fallback path (a tiny token budget) makes the
        prompt reflect ``context``'s own retrieved knowledge instead:

        >>> from config import override_settings
        >>> ctx2 = RetrievalContext(
        ...     entities=["Customer"],
        ...     facts=["Contract"],
        ...     relationships=["JOIN [Dim].[Customer] ON [Fact].[Contract].[CustID] = [Dim].[Customer].[CustID]"],
        ...     business_rules=["Persian year starts from Farvardin (month 1)."],
        ...     examples=[{"question": "Top buyers", "sql": "SELECT TOP 10 Name FROM Customer", "tags": ["customer"]}],
        ...     filters={"PersianYear": 1402},
        ... )
        >>> with override_settings(prompt_retrieval_token_budget=1):
        ...     fallback_prompt = PromptBuilder.build(
        ...         "خرید مشتریان در ۱۴۰۲", "You are a T-SQL expert.", ctx2
        ...     )
        >>> "Top buyers" in fallback_prompt
        True
        """
        if should_use_static_prefix(system_prompt):
            return PromptBuilder.build_static(
                question, system_prompt, context, session_context=session_context
            )
        return PromptBuilder._build_retrieval(
            question, system_prompt, context, session_context=session_context
        )

    @staticmethod
    def build_static(
        question: str,
        system_prompt: str,
        context: RetrievalContext,
        *,
        session_context: str = "",
    ) -> str:
        """Static-prefix path: cached prefix + a small variable suffix.

        The prefix (system prompt, full schema, all relationships, all
        business rules, all metrics, all examples) comes from
        :func:`~prompt_engine.static_prefix.build_static_prefix`, which is
        cached and therefore byte-identical across calls with the same
        ``system_prompt`` — the whole point of this path (see module
        docstring). Only ``context.filters``, ``session_context``, and
        ``question`` vary.

        Parameters
        ----------
        question, system_prompt, context, session_context:
            As in :meth:`build`.

        Returns
        -------
        str

        Examples
        --------
        >>> from core.models import RetrievalContext
        >>> ctx = RetrievalContext(filters={"Ring": "تالار پتروشیمی"})
        >>> p1 = PromptBuilder.build_static("q1", "You are a T-SQL expert.", ctx)
        >>> p2 = PromptBuilder.build_static("q2", "You are a T-SQL expert.", ctx)
        >>> p1[: p1.index("DETECTED FILTERS")] == p2[: p2.index("DETECTED FILTERS")]
        True
        >>> "تالار پتروشیمی" in p1
        True
        """
        prefix = build_static_prefix(system_prompt)
        filters = "\n".join(f"{key}: {value}" for key, value in context.filters.items())
        suffix = SUFFIX_TEMPLATE.format(
            filters=filters,
            session_context=session_context,
            question=question,
        )
        return prefix + suffix

    @staticmethod
    def _build_retrieval(
        question: str,
        system_prompt: str,
        context: RetrievalContext,
        *,
        session_context: str = "",
    ) -> str:
        """Retrieval-fallback path: only the retrieved tables/rules/examples.

        This is the original per-question prompt assembly, used once the
        knowledge base is too large for :meth:`build_static` to be
        economical (see module docstring). Kept fully functional — it is
        not dead code, it is the scaling path for later phases with a
        bigger schema.
        """
        selected_tables = context.selected_tables

        schema_context = SchemaRegistry.build_schema_context(selected_tables)

        relationships = "\n".join(context.relationships)

        rules = "\n\n".join(context.business_rules)

        examples = []
        for item in context.examples:
            examples.append(
                f"""Question:\n{item['question']}\n\nSQL:\n{item['sql']}"""
            )
        example_context = "\n\n".join(examples)

        filters = []
        for key, value in context.filters.items():
            filters.append(f"{key}: {value}")
        filter_context = "\n".join(filters)

        return PROMPT_TEMPLATE.format(
            system_prompt=system_prompt,
            business_rules=rules,
            schema=schema_context,
            relationships=relationships,
            filters=filter_context,
            examples=example_context,
            question=question,
        )
