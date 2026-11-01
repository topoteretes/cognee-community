"""Example: Ingesting Packagist / Composer PHP packages into Cognee memory."""

import asyncio
import os
import sys
from pathlib import Path

# Enable direct script execution without prior installation
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cognee_community_connector_packagist import (
    PackagistRenderOptions,
    packagist_source,
)

try:
    import cognee
except ImportError:
    cognee = None


async def main():
    # Popular packages to demonstrate active, abandoned, and security advisory handling
    packages_to_track = [
        "monolog/monolog",          # Popular logging library
        "guzzlehttp/guzzle",        # HTTP client
        "swiftmailer/swiftmailer",  # Famous abandoned library (replaced by symfony/mailer)
    ]

    print("======================================================================")
    print(">> COGNEE COMMUNITY CONNECTOR: Packagist / Composer (PHP Registry)")
    print(f">> Selected Packages: {', '.join(packages_to_track)}")
    print("======================================================================\n")

    options = PackagistRenderOptions(
        include_security_advisories=True,
        include_version_requirements=True,
        max_recent_versions=3,
    )

    source = packagist_source(
        packages=packages_to_track,
        options=options,
    )

    if cognee is not None:
        print(">> Ingesting PHP package ecosystem into Cognee Graph Memory...")
        await cognee.remember(
            source,
            dataset_name="php_package_registry",
        )
        print("\n[SUCCESS] Packagist data successfully remembered in Cognee AI memory!")
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
        print(f"| Package Name            | Status               | Advisories    | Replacement Package        |")
        print(f"+-------------------------+----------------------+---------------+----------------------------+")
        for doc in docs:
            pkg = doc.get("package_name", "")[:23].ljust(23)
            status = ("Abandoned" if doc.get("is_abandoned") else "Active")[:20].ljust(20)
            adv = str(doc.get("advisory_count", 0)).rjust(13)
            rep = str(doc.get("replacement_package") or "N/A")[:26].ljust(26)
            print(f"| {pkg} | {status} | {adv} | {rep} |")
        print(f"+-------------------------+----------------------+---------------+----------------------------+\n")

        if docs:
            print(f"Document Snapshot [ID: {docs[0]['id']}]:")
            print("-" * 50)
            print(docs[0]["content"][:350] + "...\n")


if __name__ == "__main__":
    asyncio.run(main())
