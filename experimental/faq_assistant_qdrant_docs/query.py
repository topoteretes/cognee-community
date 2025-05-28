import asyncio
import os
import cognee
from cognee.modules.search.types import SearchType
from openai import OpenAI

async def main():
    dataset_name = "qdrant_docs_dataset"
    query_text = "What documents we loaded"

    # Graph completion
    graph_completion_answer = await cognee.search(
        query_type=SearchType.GRAPH_COMPLETION, 
        query_text=query_text, 
        datasets=[dataset_name]
    )
    print("\nGraph completion answer:")
    for result in graph_completion_answer:
        print(f"- {result}")

    # Traditional RAG completion
    search_results_traditional_rag = await cognee.search(
        query_type=SearchType.RAG_COMPLETION,
        query_text=query_text,
        datasets=[dataset_name]
    )
    print("\nTraditional RAG completion answer:")
    print(search_results_traditional_rag)

    # OpenAI completion
    os.environ["OPENAI_API_KEY"] = os.environ["LLM_API_KEY"]
    client = OpenAI()
    
    llm_response = client.responses.create(
        model="gpt-4o-mini",
        input=query_text
    )
    
    print("\nOpenAI response:")
    print(llm_response.output_text)


if __name__ == "__main__":
    asyncio.run(main())
