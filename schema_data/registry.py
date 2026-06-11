from schema_data.columns import TABLE_COLUMNS
from schema_data.tables import TABLE_DESCRIPTIONS
from schema_data.relationships import RELATIONSHIPS


class SchemaRegistry:

    @staticmethod
    def build_schema_context(selected_tables: list) -> str:

        lines = []

        for table_name in selected_tables:

            description = TABLE_DESCRIPTIONS.get(
                table_name, ""
            )

            columns = TABLE_COLUMNS.get(
                table_name, {}
            )

            lines.append(
                f"Table: {table_name}"
            )

            if description:
                lines.append(
                    f"Description: {description}"
                )

            if columns:
                lines.append("Columns:")
                for col_name, col_desc in columns.items():
                    lines.append(
                        f"  - {col_name}: {col_desc}"
                    )

            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def get_relationships(selected_tables: list) -> list:

        selected = set(selected_tables)
        result = []

        for name, join_sql in RELATIONSHIPS.items():

            parts = name.split(" -> ")
            left  = parts[0].split(".")[0]
            right = parts[1].split(".")[0]

            if left in selected and right in selected:
                result.append(join_sql)

        return result
