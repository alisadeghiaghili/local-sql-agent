from schema_data.tables import TABLES
from schema_data.columns import COLUMNS
from schema_data.relationships import RELATIONSHIPS


class SchemaRegistry:

    @staticmethod
    def get_table(table_name):
        return TABLES.get(table_name)

    @staticmethod
    def get_columns(table_name):
        return COLUMNS.get(table_name, [])

    @staticmethod
    def get_relationships():
        return RELATIONSHIPS

    @staticmethod
    def build_schema_context(selected_tables):

        sections = []

        for table in selected_tables:

            table_info = TABLES.get(table)
            columns = COLUMNS.get(table, [])

            if not table_info:
                continue

            block = f"""
Table:
{table_info['table']}

Columns:
{chr(10).join(columns)}
            """

            sections.append(block.strip())

        return "\n\n".join(sections)
