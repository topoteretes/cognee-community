"""Command-Line Interface (CLI) for testing and querying the Cognee NuGet Connector."""

import argparse
import sys

from .nuget import (
    NugetClient,
    NugetRenderOptions,
    extract_packages_from_dotnet_file,
    sync_nuget_records,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cognee Community NuGet v3 Connector CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--packages",
        default="Newtonsoft.Json,WindowsAzure.Storage",
        help="Comma-separated list of NuGet package IDs",
    )
    parser.add_argument(
        "--search",
        help="Search query to discover NuGet packages dynamically",
    )
    parser.add_argument(
        "--project-file",
        help="Path to .csproj, Directory.Packages.props, or packages.lock.json file",
    )
    parser.add_argument(
        "--exclude-deprecated",
        action="store_true",
        help="Filter out and tombstone deprecated packages",
    )

    args = parser.parse_args(argv)

    client = NugetClient()

    target_packages = [p.strip() for p in args.packages.split(",") if p.strip()]

    if args.project_file:
        print(f">> Extracting dependencies from .NET project file '{args.project_file}'...")
        try:
            extracted = extract_packages_from_dotnet_file(args.project_file)
            print(f">> Found {len(extracted)} dependencies: {', '.join(extracted)}")
            for p in extracted:
                if p not in target_packages:
                    target_packages.append(p)
        except Exception as exc:
            print(f"[ERROR] Failed extracting dependencies: {exc}", file=sys.stderr)
            sys.exit(1)

    if args.search:
        print(f">> Searching NuGet for query '{args.search}'...")
        try:
            results = client.search_packages(args.search, limit=5)
            print(f">> Found {len(results)} matches:")
            for p in results:
                print(f"   * {p}")
                if p not in target_packages:
                    target_packages.append(p)
        except Exception as exc:
            print(f"[ERROR] Search failed: {exc}", file=sys.stderr)
            sys.exit(1)

    print(f">> Syncing and rendering documents for: {target_packages}...\n")
    options = NugetRenderOptions(exclude_deprecated=args.exclude_deprecated)

    count = 0
    for doc in sync_nuget_records(client, target_packages, options=options):
        count += 1
        is_dep = doc.get("is_deprecated", False)
        alt = doc.get("alternate_package")
        print("=" * 70)
        print(f"DOCUMENT ID : {doc['id']}")
        print(f"TITLE       : {doc['title']}")
        print(f"VERSION     : {doc.get('latest_version')}")
        print(f"DEPRECATED  : {is_dep} (Alternate: {alt})")
        print(f"ADVISORIES  : {doc.get('vulnerability_count', 0)}")
        print(f"TOMBSTONE   : {doc.get('_deleted', False)}")
        print("=" * 70)
        print(doc["content"])
        print()

    print(f"[SUCCESS] Processed {count} NuGet package document(s) for Cognee AI Memory.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
