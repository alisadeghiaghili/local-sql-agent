"""PromptBuilder — assembles the full LLM prompt from retrieval context."""

from __future__ import annotations

from core.models import RetrievalContext
from schema_data.registry import SchemaRegistry
from prompt_engine.templates import PROMPT_TEMPLATE


class PromptBuilder:

    @staticmethod
    def build(
        question: str,
        system_prompt: str,
        context: RetrievalContext,
    ) -> str:
        """Return the fully assembled prompt string."""

        # --- schema ---
        schema_context = SchemaRegistry.build_schema_context(
            context.selected_tables
        )

        # --- relationships ---
        relationships = "\n".join(context.relationships)

        # --- business rules ---
        rules = "\n\n".join(context.business_rules)

        # --- few-shot examples ---
        example_blocks: list[str] = []
        for item in context.examples:
            example_blocks.append(
                f"Question:\n{item['question']}\n\nSQL:\n{item['sql'].strip()}"
            )
        example_context = "\n\n---\n\n".join(example_blocks)

        # --- filter values ---
        filter_lines = [
            f"{key}: {value}"
            for key, value in context.filters.items()
        ]
        filter_context = "\n".join(filter_lines)

        return PROMPT_TEMPLATE.format(
            system_prompt=system_prompt,
            business_rules=rules,
            schema=schema_context,
            relationships=relationships,
            filters=filter_context,
            examples=example_context,
            question=question,
        )
