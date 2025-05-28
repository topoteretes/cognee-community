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
    dataset_name = "dlt_docs_and_hackernews_api"

    # 3) Load the .md file
    dlt_docs_path = current_dir / "docs_dlt_some_pages.md"  
    hackernews_api_docs_path = current_dir / "hackernews_api_docs.md"
    extra_docs_path = current_dir / "extra_docs.md"

    with open(dlt_docs_path, "r", encoding="utf-8") as f:
        dlt_docs_content = f.read()
    
    with open(hackernews_api_docs_path, "r", encoding="utf-8") as f:
        hackernews_api_docs_content = f.read()

    with open(extra_docs_path, "r", encoding="utf-8") as f:
        extra_docs_content = f.read()

    # 4) Add the .md content to cognee
    await cognee.add([dlt_docs_content, hackernews_api_docs_content, extra_docs_content], dataset_name)    

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
