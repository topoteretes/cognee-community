"""Example demonstration: Ingesting NuGet package metadata into Cognee."""

import asyncio
import sys
from pathlib import Path

# Ensure local package is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cognee_community_connector_nuget import NugetRenderOptions, nuget_source

try:
    import cognee
except ImportError:
    cognee = None


async def main():
    packages_to_index = [
        "Newtonsoft.Json",  # Popular JSON serialization library
        "Serilog",  # Logging library
        "WindowsAzure.Storage",  # Deprecated library (author recommends Azure.Storage.Common)
    ]

    print(f">> Initializing NuGet Data Connector for packages: {packages_to_index}...")
    source = nuget_source(
        packages=packages_to_index,
        options=NugetRenderOptions(
            include_vulnerabilities=True,
            include_dependencies=True,
            include_deprecations=True,
        ),
    )

    if cognee is not None:
        print(">> Ingesting NuGet packages into Cognee AI knowledge graph...")
        await cognee.remember(
            source,
            dataset_name="dotnet_package_registry",
        )
        print("\n[SUCCESS] NuGet data successfully remembered in Cognee AI memory!")
    else:
        print("[INFO] Running in inspection mode (Cognee core optional dependency not present):")
        docs = []
        if isinstance(source, list) and len(source) > 0 and callable(source[0]):
            docs = list(source[0]())
        elif isinstance(source, list):
            docs = source
        else:
            try:
                docs = list(source)
            except Exception:
                docs = []

        print("\n+-------------------------+------------+------------+-----------------------+")
        print("| Package ID              | Status     | Advisories | Alternate Package     |")
        print("+-------------------------+------------+------------+-----------------------+")
        for doc in docs:
            pkg = doc.get("package_id", "")[:23].ljust(23)
            status = ("Deprecated" if doc.get("is_deprecated") else "Active")[:10].ljust(10)
            adv = str(doc.get("vulnerability_count", 0)).rjust(10)
            alt = str(doc.get("alternate_package") or "N/A")[:21].ljust(21)
            print(f"| {pkg} | {status} | {adv} | {alt} |")
        print("+-------------------------+------------+------------+-----------------------+\n")

        if docs:
            print(f"Document Snapshot [ID: {docs[0]['id']}]:")
            print("-" * 50)
            print(docs[0]["content"][:350] + "...\n")


if __name__ == "__main__":
    asyncio.run(main())
