"""Cognee and DLT connector integration for Excalidraw architecture diagrams."""

import asyncio
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from cognee_community_connector_excalidraw.formatter import diagram_to_cognee_document
from cognee_community_connector_excalidraw.models import (
    ExcalidrawDiagram,
    RenderOptions,
)
from cognee_community_connector_excalidraw.parser import ExcalidrawParser

logger = logging.getLogger(__name__)

DOCUMENT_SOURCE_ATTR = "excalidraw"


class ExcalidrawConnector:
    """High-level connector for ingesting Excalidraw diagrams into Cognee memory."""

    def __init__(
        self,
        parser: type[ExcalidrawParser] | None = None,
        options: RenderOptions | None = None,
    ) -> None:
        self.parser = parser or ExcalidrawParser
        self.options = options or RenderOptions()
        self.sync_stats: dict[str, int] = {
            "processed": 0,
            "skipped": 0,
            "failed": 0,
        }

    def __repr__(self) -> str:
        return f"ExcalidrawConnector(options={self.options!r})"

    async def fetch_documents(
        self,
        file_paths: list[str | Path] | None = None,
        directory_path: str | Path | None = None,
        json_payloads: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Parse files, directories, or raw JSON into Cognee diagram documents."""
        diagrams: list[ExcalidrawDiagram] = []
        skipped_count = 0
        failed_count = 0

        if file_paths:
            for p in file_paths:
                try:
                    diag = self.parser.parse_file(p)
                    diagrams.append(diag)
                except Exception as exc:
                    logger.warning("Failed to parse Excalidraw file '%s': %s", p, exc)
                    failed_count += 1

        if directory_path:
            try:
                dir_diags = self.parser.parse_directory(directory_path)
                diagrams.extend(dir_diags)
            except Exception as exc:
                logger.error("Error reading directory '%s': %s", directory_path, exc)
                failed_count += 1

        if json_payloads:
            for i, payload in enumerate(json_payloads):
                try:
                    diag = self.parser.parse_json(
                        payload, diagram_name=f"Inline Diagram {i + 1}", file_path=f"inline_{i + 1}"
                    )
                    diagrams.append(diag)
                except Exception as exc:
                    logger.warning("Failed parsing JSON payload %d: %s", i, exc)
                    failed_count += 1

        documents: list[dict[str, Any]] = []
        for diag in diagrams:
            if not diag.nodes and not diag.connections and not diag.standalone_texts:
                skipped_count += 1
                continue
            doc = diagram_to_cognee_document(diag, options=self.options)
            documents.append(doc)

        self.sync_stats = {
            "processed": len(documents),
            "skipped": skipped_count,
            "failed": failed_count,
        }
        return documents

    def fetch_documents_sync(
        self,
        file_paths: list[str | Path] | None = None,
        directory_path: str | Path | None = None,
        json_payloads: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Synchronous wrapper for fetch_documents."""
        return asyncio.run(
            self.fetch_documents(
                file_paths=file_paths,
                directory_path=directory_path,
                json_payloads=json_payloads,
            )
        )


def excalidraw_source(
    file_paths: list[str | Path] | None = None,
    directory_path: str | Path | None = None,
    json_payloads: list[dict[str, Any]] | None = None,
    options: RenderOptions | None = None,
) -> Iterator[dict[str, Any]]:
    """Generator source emitting Cognee documents for Excalidraw diagrams."""
    connector = ExcalidrawConnector(options=options)
    documents = connector.fetch_documents_sync(
        file_paths=file_paths,
        directory_path=directory_path,
        json_payloads=json_payloads,
    )
    for doc in documents:
        doc[DOCUMENT_SOURCE_ATTR] = True
        yield doc


try:
    import dlt

    @dlt.resource(
        name="excalidraw_documents",
        write_disposition="replace",
        primary_key="id",
    )
    def excalidraw_resource(
        file_paths: list[str | Path] | None = None,
        directory_path: str | Path | None = None,
        json_payloads: list[dict[str, Any]] | None = None,
        options: RenderOptions | None = None,
    ) -> Iterable[dict[str, Any]]:
        """DLT resource yielding Excalidraw architecture documents."""
        yield from excalidraw_source(
            file_paths=file_paths,
            directory_path=directory_path,
            json_payloads=json_payloads,
            options=options,
        )

    @dlt.source(name="excalidraw")
    def excalidraw_dlt_source(
        file_paths: list[str | Path] | None = None,
        directory_path: str | Path | None = None,
        json_payloads: list[dict[str, Any]] | None = None,
        options: RenderOptions | None = None,
    ) -> Any:
        """DLT source wrapping excalidraw_resource for Cognee ingestion pipelines."""
        return excalidraw_resource(
            file_paths=file_paths,
            directory_path=directory_path,
            json_payloads=json_payloads,
            options=options,
        )

except ImportError:
    pass
