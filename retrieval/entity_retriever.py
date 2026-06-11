from knowledge.entities import ENTITIES


class EntityRetriever:

    @staticmethod
    def retrieve(question: str) -> list[str]:

        question_lower = question.lower()

        matches = []

        for entity_key, entity_data in ENTITIES.items():

            for alias in entity_data["aliases"]:

                if alias.lower() in question_lower:

                    matches.append(
                        entity_data["table"]
                    )

                    break

        return matches
