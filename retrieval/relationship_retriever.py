from schema_data.relationships import RELATIONSHIPS


class RelationshipRetriever:

    @staticmethod
    def retrieve(selected_tables: list[str]) -> list[str]:

        selected_set = set(selected_tables)

        results = []

        for name, join_sql in RELATIONSHIPS.items():

            parts = name.split(" -> ")
            left_table  = parts[0].split(".")[0]
            right_table = parts[1].split(".")[0]

            if (
                left_table in selected_set
                and right_table in selected_set
            ):
                results.append(join_sql)

        return results
