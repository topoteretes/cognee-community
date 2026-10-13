"""Remember selected Box folders and recall their content."""

import asyncio
import os

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError

from cognee_community_connector_box import box_source


async def main():
    folder_ids = os.environ["BOX_FOLDER_IDS"].split(",")
    dataset = os.environ.get("BOX_DATASET", "box_documents")
    await cognee.remember(
        box_source(folder_ids=folder_ids),
        dataset_name=dataset,
        self_improvement=False,
        dlt_config={"write_disposition": "merge", "primary_key": "id"},
    )
    try:
        results = await cognee.recall(
            os.environ.get("BOX_QUERY", "What is described in these Box documents?"),
            datasets=[dataset],
        )
    except NoDataError:
        print("No searchable Box documents remain in this dataset.")
        return
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
