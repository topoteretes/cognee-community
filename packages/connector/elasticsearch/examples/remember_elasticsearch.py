"""Sync an existing Elasticsearch index and query it with Cognee.

Set ELASTICSEARCH_URL, ELASTICSEARCH_API_KEY, ELASTICSEARCH_INDEX and
ELASTICSEARCH_SOURCE_ID; configure Cognee's model providers as usual.
"""

import asyncio
import os

import cognee

from cognee_community_connector_elasticsearch import elasticsearch_source


async def main():
    dataset = os.environ.get("COGNEE_DATASET", "elasticsearch_knowledge")
    source = elasticsearch_source(
        source_id=os.environ["ELASTICSEARCH_SOURCE_ID"],
        index=os.environ["ELASTICSEARCH_INDEX"],
        updated_field=os.environ.get("ELASTICSEARCH_UPDATED_FIELD", "updated_at"),
        ca_certs=os.environ.get("ELASTICSEARCH_CA_CERTS"),
    )
    await cognee.remember(
        source,
        dataset_name=dataset,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    results = await cognee.recall(
        os.environ.get("COGNEE_QUESTION", "What information is in these documents?"),
        datasets=[dataset],
    )
    for result in results:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
