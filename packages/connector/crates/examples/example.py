"""Example demonstration of Cognee Crates.io Rust package connector."""

import asyncio
from pathlib import Path

from cognee_community_connector_crates import (
    CratesClient,
    CratesConnector,
    render_crate_markdown,
)


async def main() -> None:
    print("=== Cognee Crates.io Connector Demo ===\n")

    client = CratesClient()

    # 1. Fetching a single popular crate with feature flags and dependencies
    print("1. Fetching 'tokio' package metadata and docs...")
    tokio_pkg = await client.fetch_crate_package("tokio")
    print(f"Crate:       {tokio_pkg.metadata.name}")
    print(f"Version:     {tokio_pkg.latest_version.num if tokio_pkg.latest_version else 'N/A'}")
    print(f"License:     {tokio_pkg.latest_version.license if tokio_pkg.latest_version else 'N/A'}")
    print(f"Downloads:   {tokio_pkg.metadata.downloads:,}")
    print(
        f"Features:    {len(tokio_pkg.latest_version.features if tokio_pkg.latest_version else {})}"
    )
    print(f"Deps Count:  {len(tokio_pkg.dependencies)}\n")

    # 2. Rendering Cognee-formatted Markdown
    print("2. Formatted Markdown Preview (First 350 chars):")
    md = render_crate_markdown(tokio_pkg)
    print(md[:350] + "...\n")

    # 3. Reading dependencies from a sample Cargo.toml
    print("3. Parsing local Cargo.toml manifests...")
    cargo_path = Path(__file__).parent / "Cargo.toml"
    if not cargo_path.exists():
        cargo_path.write_text(
            """
[package]
name = "my-rust-agent"
version = "0.1.0"

[dependencies]
serde = "1.0"
axum = "0.7"
tokio = { version = "1.0", features = ["full"] }
"""
        )

    deps = CratesClient.parse_cargo_toml(cargo_path)
    print(f"Discovered {len(deps)} crate dependencies from {cargo_path.name}: {deps}\n")

    # 4. Ingestion into Cognee memory
    print("4. Preparing Cognee documents for indexing...")
    connector = CratesConnector(client=client)
    docs = await connector.fetch_documents(crate_names=["serde", "axum"])
    print(f"Generated {len(docs)} Cognee documents:")
    for doc in docs:
        print(f"  - [{doc['id']}] {doc['title']} (data_id: {doc['data_id'][:12]}...)")

    print(
        "\nNext step: Pass documents to `cognee.add(doc['content'])` and `await cognee.cognify()`"
    )


if __name__ == "__main__":
    asyncio.run(main())
