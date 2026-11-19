"""Example demonstration of Cognee Hex.pm BEAM package connector."""

import asyncio
from pathlib import Path

from cognee_community_connector_hexpm import (
    HexClient,
    HexConnector,
    render_hex_markdown,
)


async def main() -> None:
    print("=== Cognee Hex.pm Connector Demo ===\n")

    client = HexClient()

    # 1. Fetching a single package with retirements & dependencies
    print("1. Fetching 'phoenix' package metadata and releases from Hex.pm...")
    phoenix = await client.fetch_package_entity("phoenix")
    print(f"Package:     {phoenix.name}")
    print(f"Version:     {phoenix.latest_version}")
    print(f"Licenses:    {', '.join(phoenix.licenses)}")
    print(f"Maintainers: {', '.join(phoenix.maintainers)}")
    print(f"Retirements: {len(phoenix.all_retirements)} historical entries")
    if phoenix.latest_release:
        print(f"Dependencies: {len(phoenix.latest_release.requirements)} requirements\n")

    # 2. Rendering Cognee-formatted Markdown
    print("2. Formatted Markdown Preview (First 350 chars):")
    md = render_hex_markdown(phoenix)
    print(md[:350] + "...\n")

    # 3. Reading dependencies from a sample mix.lock
    print("3. Parsing local Elixir mix.lock manifests...")
    mix_lock_path = Path(__file__).parent / "mix.lock"
    if not mix_lock_path.exists():
        mix_lock_path.write_text(
            """
%{
  "bandit": {:hex, :bandit, "1.5.7", "checksum", [:mix], []},
  "jason": {:hex, :jason, "1.4.4", "checksum", [:mix], []},
  "phoenix": {:hex, :phoenix, "1.7.14", "checksum", [:mix], []}
}
"""
        )

    packages = HexClient.parse_mix_lock(mix_lock_path)
    print(f"Discovered {len(packages)} Hex packages from {mix_lock_path.name}: {packages}\n")

    # 4. Ingestion into Cognee memory
    print("4. Preparing Cognee documents for indexing...")
    connector = HexConnector(client=client)
    docs = await connector.fetch_documents(package_names=["jason", "bandit"])
    print(f"Generated {len(docs)} Cognee documents:")
    for doc in docs:
        print(f"  - [{doc['id']}] {doc['title']} (data_id: {doc['data_id'][:12]}...)")

    print(
        "\nNext step: Pass documents to `cognee.add(doc['content'])` and `await cognee.cognify()`"
    )


if __name__ == "__main__":
    asyncio.run(main())
