"""Assemble the final LLM prompt from retrieval context and schema.

This module contains a single static class, :class:`PromptBuilder`, whose
``build()`` method combines every piece of retrieved knowledge — schema,
relationships, business rules, few-shot examples, and value filters — into
a single structured string ready for the LLM backend.

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
from schema_data.registry import SchemaRegistry
from prompt_engine.templates import PROMPT_TEMPLATE


class PromptBuilder:
    """Stateless factory that converts a :class:`~core.models.RetrievalContext` into a prompt string.

    All methods are static — there is no instance state.  The class exists
    purely as a namespace to keep prompt-assembly logic together.

    Design notes
    ------------
    * ``selected_tables`` is derived from ``context.entities + context.facts``
      via ``set()``; insertion order is **not** preserved at this step.  If
      ordering matters downstream, use ``context.selected_tables`` instead
      (which is order-preserving).
    * :meth:`~schema_data.registry.SchemaRegistry.build_schema_context` is
      called with the deduplicated table list so the schema block contains
      only the columns relevant to the question.
    * Empty sections (no rules, no examples, no filters) produce empty strings
      rather than placeholder text — the template handles missing sections
      gracefully without printing ``"None"`` or ``"[]"``.
    """

    @staticmethod
    def build(
        question: str,
        system_prompt: str,
        context: RetrievalContext,
    ) -> str:
        """Build a complete prompt string for the LLM backend.

        Combines system instructions, domain business rules, table schema,
        JOIN relationships, value filters, few-shot examples, and the
        question itself into a single string formatted by
        :data:`~prompt_engine.templates.PROMPT_TEMPLATE`.

        Parameters
        ----------
        question:
            The original natural-language question in Persian or English.
            Injected verbatim into the ``{question}`` placeholder of the
            template.
        system_prompt:
            Domain-specific system instructions loaded from
            ``prompts/system_prompt.md`` at server startup.  Injected into
            the ``{system_prompt}`` placeholder.
        context:
            Fully populated :class:`~core.models.RetrievalContext` produced
            by :class:`~retrieval.context_retriever.ContextRetriever`.  An
            empty context (no entities, no facts) produces a minimal prompt
            that still contains the system instructions and the question.

        Returns
        -------
        str
            A ready-to-send prompt string that contains the following
            labelled sections (in template order):

            1. **System prompt** — domain instructions and output format rules.
            2. **Business rules** — domain-specific constraints relevant to the
               question (one rule per paragraph, separated by blank lines).
            3. **Schema** — table names, descriptions, and column definitions
               for every selected table.
            4. **Relationships** — JOIN SQL snippets for FK edges between the
               selected tables (one clause per line).
            5. **Filters** — concrete value filters extracted from the question
               (e.g. ``Ring: تالار پتروشیمی``, ``PersianYear: 1402``).
            6. **Examples** — few-shot ``Question / SQL`` pairs ranked by tag
               overlap (separated by blank lines).
            7. **Question** — the user's question, repeated at the end.

        Examples
        --------
        >>> from core.models import RetrievalContext
        >>> ctx = RetrievalContext(
        ...     entities=["Customer"],
        ...     facts=["Contract"],
        ...     relationships=["JOIN [Dim].[Customer] ON [Fact].[Contract].[CustID] = [Dim].[Customer].[CustID]"],
        ...     business_rules=["Persian year starts from Farvardin (month 1)."],
        ...     examples=[{"question": "Top buyers", "sql": "SELECT TOP 10 Name FROM Customer", "tags": ["customer"]}],
        ...     filters={"PersianYear": 1402},
        ... )
        >>> prompt = PromptBuilder.build("خرید مشتریان در ۱۴۰۲", "You are a T-SQL expert.", ctx)
        >>> "Customer" in prompt
        True
        >>> "1402" in prompt
        True
        >>> "Persian year" in prompt
        True
        >>> "Top buyers" in prompt
        True
        """
        selected_tables = list(
            set(context.entities + context.facts)
        )

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
