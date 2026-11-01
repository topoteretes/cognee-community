"""Command-Line Interface (CLI) for testing and previewing the Cognee FRED Connector."""

import argparse
import os
import sys

from .fred import FredClient, FredRenderOptions, sync_fred_records


def main():
    parser = argparse.ArgumentParser(
        description="Cognee Community FRED Economic Data Connector CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("FRED_API_KEY", ""),
        help="FRED API key (or set FRED_API_KEY environment variable)",
    )
    parser.add_argument(
        "--series",
        default="GDP,UNRATE",
        help="Comma-separated FRED series identifiers (e.g. 'GDP,UNRATE,FEDFUNDS')",
    )
    parser.add_argument(
        "--search",
        help="Search query to discover series instead of passing IDs",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum recent observations to show per series",
    )

    args = parser.parse_args()

    api_key = args.api_key.strip()
    if not api_key:
        print("[ERROR] FRED API key missing. Please provide --api-key or set FRED_API_KEY in your environment.", file=sys.stderr)
        print("Obtain a free instant key at: https://fred.stlouisfed.org/docs/api/api_key.html", file=sys.stderr)
        sys.exit(1)

    client = FredClient(api_key=api_key)

    target_series = [s.strip() for s in args.series.split(",") if s.strip()]

    if args.search:
        print(f">> Searching FRED for query: '{args.search}'...")
        try:
            results = client.search_series(args.search, limit=5)
            print(f">> Found {len(results)} matches:")
            for item in results:
                s_id = item.get("id")
                s_title = item.get("title")
                print(f"   * [{s_id}] {s_title}")
                if s_id and s_id not in target_series:
                    target_series.append(s_id)
        except Exception as exc:
            print(f"[ERROR] Search failed: {exc}", file=sys.stderr)
            sys.exit(1)

    print(f">> Fetching and rendering documents for series: {target_series}...")
    options = FredRenderOptions(max_observations=args.limit)

    count = 0
    for doc in sync_fred_records(client, target_series, options=options):
        count += 1
        print("\n" + "=" * 70)
        print(f"DOCUMENT ID : {doc['id']}")
        print(f"TITLE       : {doc['title']}")
        print(f"URL         : {doc['url']}")
        print(f"LATEST DATE : {doc.get('latest_observation_date')}")
        print("=" * 70)
        print(doc["content"])

    print(f"\n[SUCCESS] Successfully fetched and rendered {count} FRED document(s) for Cognee AI Memory.")


if __name__ == "__main__":
    main()
