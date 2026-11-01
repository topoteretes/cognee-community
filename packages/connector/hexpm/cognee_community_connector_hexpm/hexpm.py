"""Cognee and DLT connector integration for Hex.pm BEAM package registry."""

import asyncio
import logging
from collections.abc import Iterable, Iterator
from typing import Any

from cognee_community_connector_hexpm.client import HexClient
from cognee_community_connector_hexpm.formatter import hex_to_cognee_document
from cognee_community_connector_hexpm.models import RenderOptions

logger = logging.getLogger(__name__)

DOCUMENT_SOURCE_ATTR = "hexpm"


class HexConnector:
    """High-level connector for syncing Hex.pm packages into Cognee memory."""

    def __init__(
        self,
        client: HexClient | None = None,
        options: RenderOptions | None = None,
    ) -> None:
        self.client = client or HexClient()
        self.options = options or RenderOptions()

    async def fetch_documents(
        self,
        package_names: list[str] | None = None,
        mix_lock_path: str | None = None,
        search_query: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch Hex packages and format as Cognee documents with tombstones."""
        targets: list[str] = []
        if package_names:
            targets.extend(package_names)

        if mix_lock_path:
            discovered = self.client.parse_mix_lock(mix_lock_path)
            targets.extend(discovered)

        if search_query:
            results = await self.client.search_packages(search_query)
            targets.extend(p["name"] for p in results if "name" in p)

        unique_targets = list(dict.fromkeys(targets))
        if not unique_targets:
            return []

        packages = await self.client.fetch_packages_batch(unique_targets)
        documents = [hex_to_cognee_document(pkg, options=self.options) for pkg in packages]
        return documents

    def fetch_documents_sync(
        self,
        package_names: list[str] | None = None,
        mix_lock_path: str | None = None,
        search_query: str | None = None,
    ) -> list[dict[str, Any]]:
        """Synchronous wrapper for fetch_documents."""
        return asyncio.run(
            self.fetch_documents(
                package_names=package_names,
                mix_lock_path=mix_lock_path,
                search_query=search_query,
            )
        )


def hexpm_source(
    package_names: list[str] | None = None,
    mix_lock_path: str | None = None,
    search_query: str | None = None,
    client: HexClient | None = None,
    options: RenderOptions | None = None,
) -> Iterator[dict[str, Any]]:
    """Generator source emitting Cognee documents for selected Hex packages."""
    connector = HexConnector(client=client, options=options)
    documents = connector.fetch_documents_sync(
        package_names=package_names,
        mix_lock_path=mix_lock_path,
        search_query=search_query,
    )
    for doc in documents:
        doc[DOCUMENT_SOURCE_ATTR] = True
        yield doc


try:
    import dlt

    @dlt.resource(
        name="hexpm_documents",
        write_disposition="merge",
        primary_key="id",
    )
    def hexpm_resource(
        package_names: list[str] | None = None,
        mix_lock_path: str | None = None,
        search_query: str | None = None,
        client: HexClient | None = None,
        options: RenderOptions | None = None,
    ) -> Iterable[dict[str, Any]]:
        """DLT resource emitting Hex packages as Cognee documents."""
        yield from hexpm_source(
            package_names=package_names,
            mix_lock_path=mix_lock_path,
            search_query=search_query,
            client=client,
            options=options,
        )

    @dlt.source(name="hexpm")
    def hexpm_dlt_source(
        package_names: list[str] | None = None,
        mix_lock_path: str | None = None,
        search_query: str | None = None,
        client: HexClient | None = None,
        options: RenderOptions | None = None,
    ) -> Any:
        """DLT source wrapping the hexpm_resource for Cognee ingestion pipelines."""
        return hexpm_resource(
            package_names=package_names,
            mix_lock_path=mix_lock_path,
            search_query=search_query,
            client=client,
            options=options,
        )

except ImportError:
    pass
