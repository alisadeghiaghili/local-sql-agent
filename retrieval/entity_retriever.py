from knowledge.entities import ENTITIES


class EntityRetriever:

    @staticmethod
    def retrieve(question: str):

        question = question.lower()

        results = []

        for entity_name, entity_info in ENTITIES.items():

            aliases = entity_info["aliases"]

            for alias in aliases:

                if alias.lower() in question:

                    results.append(entity_name)

                    break

        return list(set(results))