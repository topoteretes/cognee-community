import asyncio

import pytest
from cognee_community_retriever_code.code_retriever import CodeRetriever


@pytest.mark.parametrize("query", [None, "", " \t\n"])
def test_empty_queries_fail_before_database_or_model_access(query, monkeypatch):
    def unexpected_database_access():
        raise AssertionError("database access should not happen for an invalid query")

    monkeypatch.setattr(
        "cognee_community_retriever_code.code_retriever.get_vector_engine",
        unexpected_database_access,
    )

    async def unexpected_graph_access():
        raise AssertionError("graph access should not happen for an invalid query")

    monkeypatch.setattr(
        "cognee_community_retriever_code.code_retriever.get_graph_engine",
        unexpected_graph_access,
    )
    with pytest.raises(ValueError, match="The query must be a non-empty string"):
        asyncio.run(CodeRetriever().get_retrieved_objects(query))
