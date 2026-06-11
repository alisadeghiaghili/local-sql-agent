from core.models import RetrievalContext
from retrieval.entity_retriever import EntityRetriever
from retrieval.fact_retriever import FactRetriever
from retrieval.relationship_retriever import RelationshipRetriever
from retrieval.rule_retriever import RuleRetriever
from retrieval.example_retriever import ExampleRetriever
from retrieval.value_retriever import ValueRetriever


class ContextRetriever:

    @staticmethod
    def retrieve(question: str) -> RetrievalContext:

        entities = EntityRetriever.retrieve(question)
        facts = FactRetriever.retrieve(question)
        selected_tables = list(set(entities + facts))

        relationships = RelationshipRetriever.retrieve(selected_tables)
        rules = RuleRetriever.retrieve(question)
        examples = ExampleRetriever.retrieve(question)
        filters = ValueRetriever.retrieve(question)

        return RetrievalContext(
            entities=entities,
            facts=facts,
            dimensions=entities,
            relationships=relationships,
            business_rules=rules,
            examples=examples,
            filters=filters,
        )
