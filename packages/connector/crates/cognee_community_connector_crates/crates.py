"""Cognee and DLT connector integration for Crates.io Rust package registry."""

import asyncio
import logging
from collections.abc import Iterable, Iterator
from typing import Any

from cognee_community_connector_crates.client import CratesClient
from cognee_community_connector_crates.formatter import crate_to_cognee_document
from cognee_community_connector_crates.models import RenderOptions

logger = logging.getLogger(__name__)

DOCUMENT_SOURCE_ATTR = "crates"


class CratesConnector:
    """High-level connector for fetching and ingesting Crates into Cognee."""

    def __init__(
        self,
        client: CratesClient | None = None,
        options: RenderOptions | None = None,
    ) -> None:
        self.client = client or CratesClient()
        self.options = options or RenderOptions()
        self.sync_stats: dict[str, int] = {
            "processed": 0,
            "skipped": 0,
            "failed": 0,
        }

    async def fetch_documents(
        self,
        crate_names: list[str] | None = None,
        cargo_toml_path: str | None = None,
        search_query: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch Rust crates from names, Cargo.toml or search and format as documents."""
        targets: list[str] = []
        if crate_names:
            targets.extend(crate_names)

        if cargo_toml_path:
            discovered = self.client.parse_cargo_toml(cargo_toml_path)
            targets.extend(discovered)

        if search_query:
            results = await self.client.search_crates(search_query, limit=limit)
            targets.extend(c["name"] for c in results if "name" in c)

        unique_targets = list(dict.fromkeys(targets))
        if not unique_targets:
            return []

        packages = await self.client.fetch_packages_batch(unique_targets)
        documents = [crate_to_cognee_document(pkg, options=self.options) for pkg in packages]
        self.sync_stats = {
            "processed": len(documents),
            "skipped": len(unique_targets) - len(packages),
            "failed": 0,
        }
        return documents

    def fetch_documents_sync(
        self,
        crate_names: list[str] | None = None,
        cargo_toml_path: str | None = None,
        search_query: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Synchronous wrapper for fetch_documents."""
        return asyncio.run(
            self.fetch_documents(
                crate_names=crate_names,
                cargo_toml_path=cargo_toml_path,
                search_query=search_query,
                limit=limit,
            )
        )


def crates_source(
    crate_names: list[str] | None = None,
    cargo_toml_path: str | None = None,
    search_query: str | None = None,
    limit: int = 20,
    client: CratesClient | None = None,
    options: RenderOptions | None = None,
) -> Iterator[dict[str, Any]]:
    """Generator source emitting Cognee documents for selected Rust crates."""
    connector = CratesConnector(client=client, options=options)
    documents = connector.fetch_documents_sync(
        crate_names=crate_names,
        cargo_toml_path=cargo_toml_path,
        search_query=search_query,
        limit=limit,
    )
    for doc in documents:
        doc[DOCUMENT_SOURCE_ATTR] = True
        yield doc


try:
    import dlt

    @dlt.resource(
        name="crates_documents",
        write_disposition="replace",
        primary_key="id",
    )
    def crates_resource(
        crate_names: list[str] | None = None,
        cargo_toml_path: str | None = None,
        search_query: str | None = None,
        limit: int = 20,
        client: CratesClient | None = None,
        options: RenderOptions | None = None,
    ) -> Iterable[dict[str, Any]]:
        """DLT resource emitting Crates.io packages as Cognee documents."""
        yield from crates_source(
            crate_names=crate_names,
            cargo_toml_path=cargo_toml_path,
            search_query=search_query,
            limit=limit,
            client=client,
            options=options,
        )

    @dlt.source(name="crates")
    def crates_dlt_source(
        crate_names: list[str] | None = None,
        cargo_toml_path: str | None = None,
        search_query: str | None = None,
        limit: int = 20,
        client: CratesClient | None = None,
        options: RenderOptions | None = None,
    ) -> Any:
        """DLT source wrapping the crates_resource for Cognee ingestion pipelines."""
        return crates_resource(
            crate_names=crate_names,
            cargo_toml_path=cargo_toml_path,
            search_query=search_query,
            limit=limit,
            client=client,
            options=options,
        )

except ImportError:
    pass
