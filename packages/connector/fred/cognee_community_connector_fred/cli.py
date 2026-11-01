"""Command-Line Interface (CLI) for testing and querying the Cognee FRED Connector."""

import argparse
import os
import sys

from .fred import (
    FredClient,
    FredRenderOptions,
    sync_fred_records,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cognee Community FRED Macroeconomic Data Connector CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--series",
        default="GDP,UNRATE,CPIAUCSL",
        help="Comma-separated list of FRED series symbols",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("FRED_API_KEY", ""),
        help="FRED API Key (or FRED_API_KEY environment variable)",
    )
    parser.add_argument(
        "--search",
        help="Search query to discover FRED series automatically",
    )
    parser.add_argument(
        "--category-id",
        type=int,
        help="FRED Category ID to fetch top indicators from (e.g. 106 for GDP)",
    )
    parser.add_argument(
        "--max-obs",
        type=int,
        default=5,
        help="Maximum recent observations to include in the document content",
    )

    args = parser.parse_args(argv)

    api_key = args.api_key.strip()
    if not api_key:
        print(
            "[ERROR] FRED API key missing. Provide --api-key or set FRED_API_KEY.",
            file=sys.stderr,
        )
        print(
            "Obtain a free instant key at: https://fred.stlouisfed.org/docs/api/api_key.html",
            file=sys.stderr,
        )
        return 1

    client = FredClient(api_key=api_key)

    series_list = [s.strip() for s in args.series.split(",") if s.strip()]

    if args.category_id:
        print(f">> Fetching series from FRED Category ID: {args.category_id}...")
        try:
            cat_series = client.get_category_series(args.category_id, limit=5)
            cat_ids = [s.get("id") for s in cat_series if s.get("id")]
            print(f">> Discovered {len(cat_ids)} series: {', '.join(cat_ids)}")
            for sid in cat_ids:
                if sid not in series_list:
                    series_list.append(sid)
        except Exception as exc:
            print(f"[ERROR] Category fetch failed: {exc}", file=sys.stderr)
            return 1

    if args.search:
        print(f">> Searching FRED for query: '{args.search}'...")
        try:
            results = client.search_series(args.search, limit=5)
            sids = [s.get("id") for s in results if s.get("id")]
            print(f">> Discovered {len(sids)} series: {', '.join(sids)}")
            for sid in sids:
                if sid not in series_list:
                    series_list.append(sid)
        except Exception as exc:
            print(f"[ERROR] Search failed: {exc}", file=sys.stderr)
            return 1

    print(f">> Syncing and rendering documents for {len(series_list)} series: {series_list}...\n")
    options = FredRenderOptions(max_observations=args.max_obs)

    count = 0
    for doc in sync_fred_records(client, series_list, options=options):
        count += 1
        print("=" * 70)
        print(f"DOCUMENT ID : {doc['id']}")
        print(f"TITLE       : {doc['title']}")
        print(f"LATEST DATE : {doc.get('latest_observation_date')}")
        print(f"FREQUENCY   : {doc.get('frequency')}")
        print(f"UNITS       : {doc.get('units')}")
        print("=" * 70)
        print(doc["content"])

    print(f"\n[SUCCESS] Rendered {count} FRED document(s) for Cognee AI Memory.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
