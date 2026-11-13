"""Refresh the dedicated Fathom dataset from the current API snapshot."""

import asyncio
import os

import cognee

from cognee_community_connector_fathom import fathom_source

DATASET = "fathom-meetings"


async def main() -> None:
    api_key = os.environ.get("FATHOM_API_KEY")
    if not api_key:
        raise SystemExit("Set FATHOM_API_KEY before running this example.")

    # This dataset must be dedicated to Fathom; do not use a shared dataset.
    await cognee.forget(dataset=DATASET)

    await cognee.remember(
        fathom_source(api_key=api_key, include_transcripts=False),
        dataset_name=DATASET,
        max_rows_per_table=0,
    )
    print(f"Refreshed Fathom dataset: {DATASET}")


if __name__ == "__main__":
    asyncio.run(main())
