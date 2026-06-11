from schema_data.registry import SchemaRegistry
from prompt_engine.templates import PROMPT_TEMPLATE


class PromptBuilder:

    @staticmethod
    def build(
        question,
        system_prompt,
        context
    ):

        selected_tables = list(
            set(
                context.entities
                +
                context.facts
            )
        )

        schema_context = (
            SchemaRegistry
            .build_schema_context(
                selected_tables
            )
        )

        relationships = "\n".join(
            context.relationships
        )

        rules = "\n\n".join(
            context.business_rules
        )

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
            question=question
        )
