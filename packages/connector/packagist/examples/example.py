#!/usr/bin/env python3
"""Example usage of the Cognee Packagist connector."""

import sys
import os

# Add package root to sys.path for direct execution
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cognee_community_connector_packagist import packagist_source

def main():
    print("Testing Cognee Packagist Connector...")
    # Query monolog/monolog and swiftmailer/swiftmailer
    source = packagist_source(packages=["monolog/monolog", "swiftmailer/swiftmailer"])
    for item in source:
        print("--------------------------------------------------")
        print(f"Package: {item.get('name')}")
        print(f"Abandoned: {item.get('abandoned')} (Replacement: {item.get('replacement_package')})")
        print(f"Advisories: {item.get('advisory_count')}")
        print(f"Downloads: {item.get('total_downloads'):,}")

if __name__ == "__main__":
    main()
