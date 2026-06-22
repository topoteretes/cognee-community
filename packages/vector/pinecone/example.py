import asyncio
import os
import pathlib
from os import path

# Check for OpenAI API Key - don't use placeholder
openai_api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
if not openai_api_key or openai_api_key == "your-api-key":
    raise ValueError(
        "OpenAI API key is required! Please set OPENAI_API_KEY environment variable. "
        "You can get your API key from https://platform.openai.com/account/api-keys"
    )

os.environ["LLM_API_KEY"] = openai_api_key


async def main():
    # NOTE: Importing the register module we let cognee know it can use the Pinecone vector adapter
    # NOTE: The "noqa: F401" mark is to make sure the linter doesn't flag this as an unused import
    from cognee import SearchType, add, cognify, config, prune, search
    from cognee_community_vector_adapter_pinecone import register  # noqa: F401

    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee-system"))
    config.data_root_directory(path.join(system_path, ".cognee-data"))

    # Please provide your Pinecone API key and configuration
    config.set_vector_db_config(
        {
            "vector_db_provider": "pinecone",
            "vector_db_url": os.getenv(
                "PINECONE_API_URL", "https://api.pinecone.io"
            ),  # Allow override via env var
            "vector_db_key": os.getenv("PINECONE_API_KEY", "your-pinecone-api-key"),
            # "environment": os.getenv("PINECONE_ENVIRONMENT", None),  # Optional
            # "cloud": os.getenv("PINECONE_CLOUD", "aws"),  # Optional, defaults to aws
            # "region": os.getenv("PINECONE_REGION", "us-east-1"),  # Optional, default us-east-1
        }
    )

    await prune.prune_data()
    await prune.prune_system(metadata=True)

    await add("""
    Natural language processing (NLP) is an interdisciplinary
    subfield of computer science and information retrieval.
    """)

    await add("""
    Sandwiches are best served toasted with cheese, ham, mayo,
    lettuce, mustard, and salt & pepper.
    """)

    await cognify()

    query_text = "Tell me about NLP"

    search_results = await search(
        query_type=SearchType.GRAPH_COMPLETION, query_text=query_text
    )

    for result_text in search_results:
        print("\nSearch result: \n" + result_text)


if __name__ == "__main__":
    asyncio.run(main())
