"""Example: Ingesting NuGet v3 .NET packages into Cognee memory."""

import asyncio
import os
import sys
from pathlib import Path

# Enable direct script execution without prior installation
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cognee_community_connector_nuget import (
    NugetRenderOptions,
    nuget_source,
)

try:
    import cognee
except ImportError:
    cognee = None


async def main():
    # Packages demonstrating active, deprecated, and vulnerability advisory handling
    packages_to_track = [
        "Newtonsoft.Json",           # Iconic JSON serializer
        "Serilog",                   # Logging library
        "WindowsAzure.Storage",      # Deprecated library (author recommends Azure.Storage.Common)
    ]

    print("======================================================================")
    print(">> COGNEE COMMUNITY CONNECTOR: NuGet v3 (.NET Package Registry)")
    print(f">> Selected Packages: {', '.join(packages_to_track)}")
    print("======================================================================\n")

    options = NugetRenderOptions(
        include_vulnerabilities=True,
        include_dependencies=True,
    )

    source = nuget_source(
        packages=packages_to_track,
        options=options,
    )

    if cognee is not None:
        print(">> Ingesting .NET package ecosystem into Cognee Graph Memory...")
        await cognee.remember(
            source,
            dataset_name="dotnet_package_registry",
        )
        print("\n[SUCCESS] NuGet data successfully remembered in Cognee AI memory!")
    else:
        print("[INFO] Running in inspection mode (Cognee core optional dependency not present):")
        docs = []
        if isinstance(source, list) and len(source) > 0 and hasattr(source[0], "__call__"):
            docs = list(source[0]())
        elif isinstance(source, list):
            docs = source
        else:
            try:
                docs = list(source)
            except Exception:
                docs = []

        print(f"\n+-------------------------+----------------------+---------------+----------------------------+")
        print(f"| Package ID              | Status               | Advisories    | Alternate Package          |")
        print(f"+-------------------------+----------------------+---------------+----------------------------+")
        for doc in docs:
            pkg = doc.get("package_id", "")[:23].ljust(23)
            status = ("Deprecated" if doc.get("is_deprecated") else "Active")[:20].ljust(20)
            adv = str(doc.get("vulnerability_count", 0)).rjust(13)
            alt = str(doc.get("alternate_package") or "N/A")[:26].ljust(26)
            print(f"| {pkg} | {status} | {adv} | {alt} |")
        print(f"+-------------------------+----------------------+---------------+----------------------------+\n")

        if docs:
            print(f"Document Snapshot [ID: {docs[0]['id']}]:")
            print("-" * 50)
            print(docs[0]["content"][:350] + "...\n")


if __name__ == "__main__":
    asyncio.run(main())
