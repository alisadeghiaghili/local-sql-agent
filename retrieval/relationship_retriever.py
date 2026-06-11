"""Relationship retriever.

Reads JOIN definitions from schema_data.relationships (canonical source)
and returns only the JOIN clauses relevant to the selected tables.
"""

from __future__ import annotations

from schema_data.relationships import RELATIONSHIPS


class RelationshipRetriever:

    @staticmethod
    def retrieve(selected_tables: list[str]) -> list[str]:

        selected = set(selected_tables)
        results: list[str] = []

        for name, join_sql in RELATIONSHIPS.items():
            parts = name.split(" -> ")
            left  = parts[0].split(".")[0]
            right = parts[1].split(".")[0]

            if left in selected and right in selected:
                results.append(join_sql)

        return results
