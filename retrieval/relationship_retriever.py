from schema.relationships import RELATIONSHIPS


class RelationshipRetriever:

    @staticmethod
    def retrieve(selected_tables):

        selected_tables = set(selected_tables)

        results = []

        for name, join_sql in RELATIONSHIPS.items():

            left = name.split(" -> ")[0].split(".")[0]

            right = name.split(" -> ")[1].split(".")[0]

            if (
                left in selected_tables
                and right in selected_tables
            ):

                results.append(join_sql)

        return results