"""SchemaRegistry — builds schema context string for the prompt."""

from schema_data.tables import TABLES
from schema_data.columns import COLUMNS


class SchemaRegistry:

    @staticmethod
    def get_table(table_name: str) -> dict | None:
        return TABLES.get(table_name)

    @staticmethod
    def get_columns(table_name: str) -> list[str]:
        return COLUMNS.get(table_name, [])

    @staticmethod
    def build_schema_context(selected_tables: list[str]) -> str:
        """Return a formatted schema block for all selected tables."""

        sections: list[str] = []

        for table in selected_tables:

            table_meta = TABLES.get(table)

            if not table_meta:
                continue

            columns = COLUMNS.get(table, [])

            col_block = "\n".join(f"  - {col}" for col in columns)

            block = (
                f"Table: {table_meta['table']}\n"
                f"Columns:\n{col_block}"
            )

            sections.append(block)

        return "\n\n".join(sections)
