"""Synchronize an Airtable base and retrieve its document passages.

Set AIRTABLE_ACCESS_TOKEN, AIRTABLE_BASE_ID, and Cognee's model credentials.
Optional: AIRTABLE_TABLE_IDS (comma separated), AIRTABLE_LAST_MODIFIED_FIELD,
AIRTABLE_INCLUDE_COMMENTS=false, AIRTABLE_DATASET, and AIRTABLE_QUESTION.
Re-running this example reconciles the same base and dataset without pruning memory.
"""

import asyncio
import os


async def main():
    base_id = os.environ.get("AIRTABLE_BASE_ID")
    if not base_id or not os.environ.get("AIRTABLE_ACCESS_TOKEN"):
        raise SystemExit(
            "Set AIRTABLE_BASE_ID and AIRTABLE_ACCESS_TOKEN before running this example."
        )

    import cognee

    from cognee_community_connector_airtable import airtable_source

    dataset = os.environ.get("AIRTABLE_DATASET", f"airtable_{base_id}")
    selection = os.environ.get("AIRTABLE_TABLE_IDS")
    source = airtable_source(
        base_id=base_id,
        table_ids=[value.strip() for value in selection.split(",")] if selection else None,
        last_modified_field=os.environ.get("AIRTABLE_LAST_MODIFIED_FIELD", "Last modified time"),
        include_comments=os.environ.get("AIRTABLE_INCLUDE_COMMENTS", "true").lower() != "false",
    )
    result = await cognee.remember(
        source,
        dataset_name=dataset,
        self_improvement=False,
        dlt_config={
            "primary_key": "id",
            "write_disposition": "merge",
            "max_rows_per_table": 0,
        },
    )
    print(result)
    if result.status != "completed":
        raise RuntimeError(f"Airtable memory build did not complete: {result.status}")

    question = os.environ.get("AIRTABLE_QUESTION", "What information is available in this base?")
    passages = await cognee.recall(
        question,
        datasets=[dataset],
        query_type=cognee.SearchType.CHUNKS,
    )
    for passage in passages:
        print(passage.text)


if __name__ == "__main__":
    asyncio.run(main())
