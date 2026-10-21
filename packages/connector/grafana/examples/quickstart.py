"""Read selected Grafana prose into an explicitly chosen Cognee dataset."""

import argparse
import asyncio
import os

from cognee_community_connector_grafana import SyncStats, grafana_source


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--resource", required=True, help="Stable, distinct corpus name")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--from-ms", type=int, required=True)
    parser.add_argument("--dashboard", action="append")
    parser.add_argument("--folder", action="append")
    parser.add_argument("--organization-tag", action="append")
    parser.add_argument(
        "--query", default="Describe the selected dashboards and incident annotations."
    )
    parser.add_argument("--retire", action="store_true", help="Intentionally empty this corpus")
    args = parser.parse_args()
    if args.retire and (args.dashboard or args.folder or args.organization_tag):
        parser.error("--retire cannot be combined with selections")
    if not args.retire and not (args.dashboard or args.folder or args.organization_tag):
        parser.error("Select --dashboard, --folder or --organization-tag")
    url = os.environ.get("GRAFANA_URL")
    token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    if not url or not token:
        parser.error("Set GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN securely")
    import cognee

    stats = SyncStats()
    source = grafana_source(
        base_url=url,
        token=token,
        org_id=1,
        owner_id=args.owner,
        resource_name=args.resource,
        annotation_from_ms=args.from_ms,
        dashboard_uids=[] if args.retire else args.dashboard,
        folder_uids=args.folder,
        organization_annotation_tags=args.organization_tag,
        stats=stats,
    )
    await cognee.add(
        source,
        dataset_name=args.dataset,
        run_in_background=False,
        dlt_config={"primary_key": "id", "write_disposition": "replace"},
    )
    if not args.retire:
        await cognee.cognify(datasets=[args.dataset])
        results = await cognee.search(
            query_text=args.query, query_type=cognee.SearchType.CHUNKS, datasets=[args.dataset]
        )
        print(results)
    print(stats)
    print("Check Cognee cleanup logs and stores; rerun after upstream edits/deletions.")


if __name__ == "__main__":
    asyncio.run(main())
