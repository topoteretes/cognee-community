import asyncio
import os
import pathlib
from os import path

import cognee
from cognee import config

# NOTE: Importing the register module lets cognee know it can use the LodeDB vector adapter.
# NOTE: The "noqa: F401" mark keeps the linter from flagging this as an unused import.
from cognee_community_vector_adapter_lodedb import register  # noqa: F401
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
    # LodeDB is local and on-disk: vector_db_url is a directory (created if absent).
    config.set_vector_db_config(
        {
            "vector_db_provider": "lodedb",
            "vector_db_url": os.getenv("VECTOR_DB_URL", path.join(system_path, ".lodedb")),
            "vector_db_key": "",
        }
    )
    await cognee.remember(MY_PREFERENCE)

    query_text = "plan a 3 days Itinerary for Berlin along with restaurants to try food."
    search_results = await cognee.recall(query_text=query_text)

    for result_text in search_results:
        print(result_text)


if __name__ == "__main__":
    asyncio.run(main())
