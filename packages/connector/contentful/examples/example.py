"""Sync published Contentful content into a dedicated Cognee dataset and search it.

Set CONTENTFUL_SPACE_ID and CONTENTFUL_DELIVERY_TOKEN, plus your usual Cognee
model configuration. See the package README for optional selection variables.
No provider calls are made when the Contentful credentials are missing.
"""

import asyncio
import os


def _selection(name: str) -> list[str] | None:
    value = os.environ.get(name)
    if value is None:
        return None
    selected = [item.strip() for item in value.split(",")]
    if not all(selected):
        raise ValueError(f"{name} must contain nonempty comma-separated values")
    return selected


async def main() -> None:
    if not os.environ.get("CONTENTFUL_SPACE_ID") or not os.environ.get("CONTENTFUL_DELIVERY_TOKEN"):
        print("Set CONTENTFUL_SPACE_ID and CONTENTFUL_DELIVERY_TOKEN to run this example.")
        return

    import cognee

    from cognee_community_connector_contentful import contentful_source

    include_assets = os.environ.get("CONTENTFUL_INCLUDE_ASSETS", "true").lower()
    if include_assets not in {"true", "false"}:
        raise ValueError("CONTENTFUL_INCLUDE_ASSETS must be true or false")

    dataset_name = os.environ.get("CONTENTFUL_DATASET", "contentful")
    source = contentful_source(
        content_type_ids=_selection("CONTENTFUL_CONTENT_TYPE_IDS"),
        locales=_selection("CONTENTFUL_LOCALES"),
        include_assets=include_assets == "true",
        source_id=os.environ.get("CONTENTFUL_SOURCE_ID", "default"),
        host=os.environ.get("CONTENTFUL_DELIVERY_HOST", "cdn.contentful.com"),
    )
    print(f"Synchronizing Contentful into dataset {dataset_name!r} ...")
    await cognee.remember(
        source,
        dataset_name=dataset_name,
        primary_key="id",
        write_disposition="merge",
        run_in_background=False,
        max_rows_per_table=0,
        self_improvement=False,
    )

    answer = await cognee.search(
        query_text=os.environ.get("CONTENTFUL_QUERY", "Summarize this Contentful content."),
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[dataset_name],
    )
    print("\nSearch result:\n", answer)
    print("\nEdit, unpublish, or delete content and rerun to synchronize this source again.")


if __name__ == "__main__":
    asyncio.run(main())
