"""Example demonstration: Ingesting Packagist PHP package metadata into Cognee."""

import sys
from pathlib import Path

# Ensure local package is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cognee_community_connector_packagist import PackagistRenderOptions, packagist_source

try:
    import cognee
except ImportError:
    cognee = None


async def main():
    packages_to_index = [
        "monolog/monolog",  # Popular logging library
        "guzzlehttp/guzzle",  # HTTP client
        "swiftmailer/swiftmailer",  # Famous abandoned library (replaced by symfony/mailer)
    ]

    print(f">> Initializing Packagist Data Connector for packages: {packages_to_index}...")
    source = packagist_source(
        packages=packages_to_index,
        options=PackagistRenderOptions(
            include_security_advisories=True,
            include_abandonment_info=True,
            include_requirements=True,
        ),
    )

    if cognee is not None:
        print(">> Ingesting Packagist packages into Cognee AI knowledge graph...")
        await cognee.remember(
            source,
            dataset_name="php_package_registry",
        )
        print("\n[SUCCESS] Packagist data successfully remembered in Cognee AI memory!")
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
        print("| Package Name            | Status     | Advisories | Replacement Package   |")
        print("+-------------------------+------------+------------+-----------------------+")
        for doc in docs:
            pkg = doc.get("package_name", "")[:23].ljust(23)
            status = ("Abandoned" if doc.get("is_abandoned") else "Active")[:10].ljust(10)
            adv = str(doc.get("advisory_count", 0)).rjust(10)
            rep = str(doc.get("replacement_package") or "N/A")[:21].ljust(21)
            print(f"| {pkg} | {status} | {adv} | {rep} |")
        print("+-------------------------+------------+------------+-----------------------+\n")

        if docs:
            print(f"Document Snapshot [ID: {docs[0]['id']}]:")
            print("-" * 50)
            print(docs[0]["content"][:350] + "...\n")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
