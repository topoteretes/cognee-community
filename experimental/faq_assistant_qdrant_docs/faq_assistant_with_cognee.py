import os
import asyncio
import pathlib
from cognee.shared.logging_utils import get_logger, ERROR

import cognee


async def main():

    current_dir = pathlib.Path(__file__).parent
    data_directory_path = str(current_dir / "data_storage")
    cognee.config.data_root_directory(data_directory_path)

    cognee_directory_path = str(current_dir / "cognee_system")
    cognee.config.system_root_directory(cognee_directory_path)

    # 1) Clean slate
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    # 2) Give a dataset name
    dataset_name = "qdrant_docs_cleaned_dataset"

    # 3) Load the .md file
    md_file_path = current_dir / "docs_qdrant_cleaned.md"  # Adjust if needed
    if not md_file_path.exists():
        raise FileNotFoundError(f"Could not find {md_file_path}")

    with open(md_file_path, "r", encoding="utf-8") as f:
        md_content = f.read()

    # 4) Add the .md content to cognee
    await cognee.add([md_content], dataset_name)

    # 5) "Cognify" the data to build out the knowledge graph
    await cognee.cognify([dataset_name])



if __name__ == "__main__":
    logger = get_logger(level=ERROR)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
