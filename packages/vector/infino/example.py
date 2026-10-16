import asyncio
import os
import pathlib
from os import path

import cognee
from cognee import config

# NOTE: Importing the register module lets cognee know it can use the Infino vector adapter
# NOTE: The "noqa: F401" mark is to make sure the linter doesn't flag this as an unused import
from cognee_community_vector_adapter_infino import register  # noqa: F401
from dotenv import load_dotenv

load_dotenv()

MY_PREFERENCE = """
- I like to visit places near the beach where I can find the best spots.
- I need locations that are rare to find on blogs but are goldmine places for your eyes
- I prefer Vegetarian meals. Use this when I ask for restaurants recommendation
- My hobbies that might also help in planning Itineraries: I love Anime, F1 and Cricket.
"""


async def main():
    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee_system"))
    config.data_root_directory(path.join(system_path, ".data_storage"))

    config.set_relational_db_config({"db_provider": "sqlite"})
    config.set_vector_db_config(
        {
            "vector_db_provider": "infino",
            # Infino is embedded: the URL is where the data lives, not a
            # server. A local directory works out of the box; point it at an
            # s3:// prefix and the same memory persists on object storage as
            # plain Parquet files.
            "vector_db_url": os.getenv(
                "VECTOR_DB_URL", path.join(system_path, ".infino_storage")
            ),
            "vector_db_key": "",
            "vector_dataset_database_handler": "infino",
        }
    )
    config.set_graph_db_config(
        {
            "graph_database_provider": "ladybug",
            "graph_dataset_database_handler": "ladybug",
        }
    )
    await cognee.remember(MY_PREFERENCE)

    query_text = "plan a 3 days Itinerary for Berlin along with restaurants to try food."
    search_results = await cognee.recall(query_text=query_text)

    for result_text in search_results:
        print(result_text)


if __name__ == "__main__":
    asyncio.run(main())
