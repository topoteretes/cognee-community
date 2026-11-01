"""Unit tests for Cognee Crates.io package registry connector."""

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from cognee_community_connector_crates.cli import main as cli_main
from cognee_community_connector_crates.client import (
    CrateNotFoundError,
    CratesClient,
    CratesRateLimitError,
)
from cognee_community_connector_crates.crates import (
    DOCUMENT_SOURCE_ATTR,
    CratesConnector,
    crates_source,
)
from cognee_community_connector_crates.formatter import (
    crate_to_cognee_document,
    render_crate_markdown,
)
from cognee_community_connector_crates.models import (
    CrateDependency,
    CrateMetadata,
    CratePackage,
    CrateVersion,
    RenderOptions,
)

SAMPLE_TOKIO_PAYLOAD = {
    "crate": {
        "id": "tokio",
        "name": "tokio",
        "description": "An event-driven non-blocking I/O platform.",
        "max_version": "1.38.0",
        "documentation": "https://docs.rs/tokio",
        "repository": "https://github.com/tokio-rs/tokio",
        "homepage": "https://tokio.rs",
        "downloads": 185000000,
        "recent_downloads": 12000000,
        "updated_at": "2026-10-15T12:00:00Z",
    },
    "versions": [
        {
            "id": 1001,
            "num": "1.38.0",
            "license": "MIT",
            "downloads": 15000000,
            "features": {
                "default": ["rt", "net"],
                "full": ["fs", "io-util", "sync", "time"],
            },
            "yanked": False,
            "created_at": "2026-10-15T12:00:00Z",
            "updated_at": "2026-10-15T12:00:00Z",
        }
    ],
    "keywords": ["async", "io", "network"],
    "categories": ["asynchronous", "network-programming"],
}

SAMPLE_DEPS_PAYLOAD = {
    "dependencies": [
        {
            "id": 501,
            "version_id": 1001,
            "crate_id": "bytes",
            "req": "^1.4",
            "optional": False,
            "features": [],
            "target": None,
            "kind": "normal",
        },
        {
            "id": 502,
            "version_id": 1001,
            "crate_id": "tempfile",
            "req": "^3.0",
            "optional": True,
            "features": [],
            "target": None,
            "kind": "dev",
        },
    ]
}

SAMPLE_SEARCH_PAYLOAD = {
    "crates": [
        {
            "name": "tokio",
            "max_version": "1.38.0",
            "description": "Async runtime for Rust.",
        },
        {
            "name": "tokio-util",
            "max_version": "0.7.10",
            "description": "Utilities for working with Tokio.",
        },
    ]
}


def create_fake_transport() -> httpx.MockTransport:
    """Create a mock transport simulating Crates.io endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path == "/api/v1/crates/tokio":
            return httpx.Response(200, json=SAMPLE_TOKIO_PAYLOAD)

        if path == "/api/v1/crates/tokio/1.38.0/dependencies":
            return httpx.Response(200, json=SAMPLE_DEPS_PAYLOAD)

        if path == "/api/v1/crates/tokio/1.38.0/readme":
            return httpx.Response(200, text="# Tokio\nReliable, asynchronous runtime.")

        if path == "/api/v1/crates/rate-limited":
            return httpx.Response(429, headers={"Retry-After": "45"})

        if path == "/api/v1/crates/nonexistent":
            return httpx.Response(404, json={"errors": [{"detail": "Not Found"}]})

        if path == "/api/v1/crates" and request.url.params.get("q"):
            return httpx.Response(200, json=SAMPLE_SEARCH_PAYLOAD)

        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_client_get_crate_success():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        data = await client.get_crate("tokio")
        assert data["crate"]["name"] == "tokio"
        assert data["crate"]["max_version"] == "1.38.0"

    asyncio.run(_run())


def test_client_get_crate_empty_name_raises():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        with pytest.raises(ValueError, match="cannot be empty"):
            await client.get_crate("   ")

    asyncio.run(_run())


def test_client_get_crate_not_found():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        with pytest.raises(CrateNotFoundError, match="not found on crates.io"):
            await client.get_crate("nonexistent")

    asyncio.run(_run())


def test_client_rate_limit_429():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        with pytest.raises(CratesRateLimitError, match="Retry after 45s"):
            await client.get_crate("rate-limited")

    asyncio.run(_run())


def test_client_get_dependencies_success():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        deps = await client.get_dependencies("tokio", "1.38.0")
        assert len(deps) == 2
        assert deps[0]["crate_id"] == "bytes"

    asyncio.run(_run())


def test_client_get_readme_success():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        readme = await client.get_readme("tokio", "1.38.0")
        assert readme is not None
        assert "Reliable, asynchronous runtime" in readme

    asyncio.run(_run())


def test_client_search_crates():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        results = await client.search_crates("tokio", limit=5)
        assert len(results) == 2
        assert results[0]["name"] == "tokio"

    asyncio.run(_run())


def test_client_fetch_crate_package_full():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        pkg = await client.fetch_crate_package("tokio")
        assert pkg.metadata.name == "tokio"
        assert pkg.latest_version is not None
        assert pkg.latest_version.license == "MIT"
        assert "default" in pkg.latest_version.features
        assert len(pkg.dependencies) == 2
        assert pkg.readme is not None

    asyncio.run(_run())


def test_client_fetch_packages_batch_skips_missing():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        packages = await client.fetch_packages_batch(["tokio", "nonexistent"])
        assert len(packages) == 1
        assert packages[0].metadata.name == "tokio"

    asyncio.run(_run())


def test_parse_cargo_toml():
    cargo_content = """
    [package]
    name = "test-crate"
    version = "0.1.0"

    [dependencies]
    tokio = { version = "1.0", features = ["full"] }
    serde = "1.0"

    [dev-dependencies]
    tempfile = "3.0"

    [build-dependencies]
    cc = "1.0"
    """
    with tempfile.NamedTemporaryFile("w+", suffix=".toml", delete=False) as f:
        f.write(cargo_content)
        temp_path = f.name

    try:
        # standard normal deps only
        deps = CratesClient.parse_cargo_toml(temp_path)
        assert deps == ["tokio", "serde"]

        # with dev and build deps
        all_deps = CratesClient.parse_cargo_toml(temp_path, include_dev=True, include_build=True)
        assert all_deps == ["tokio", "serde", "tempfile", "cc"]
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_parse_cargo_toml_missing_file():
    with pytest.raises(FileNotFoundError):
        CratesClient.parse_cargo_toml("non_existent_cargo.toml")


def test_render_crate_markdown():
    meta = CrateMetadata(
        name="tokio",
        description="Async runtime for Rust.",
        max_version="1.38.0",
        documentation="https://docs.rs/tokio",
        repository="https://github.com/tokio-rs/tokio",
        downloads=1000,
        keywords=["async", "io"],
    )
    ver = CrateVersion(
        num="1.38.0",
        license="MIT",
        features={"default": ["rt"], "full": ["fs", "net"]},
    )
    deps = [CrateDependency(crate_id="bytes", req="^1.0")]
    pkg = CratePackage(
        metadata=meta,
        latest_version=ver,
        dependencies=deps,
        readme="# Documentation\nHello from Tokio.",
    )

    md = render_crate_markdown(pkg)
    assert "# Rust Crate: tokio (v1.38.0)" in md
    assert "## Cargo Feature Flags" in md
    assert "| `default` | `rt` |" in md
    assert "## Dependencies" in md
    assert "| `bytes` | `^1.0` | `normal` | No |" in md
    assert "Hello from Tokio" in md


def test_render_crate_markdown_respects_options():
    meta = CrateMetadata(name="small", description="", max_version="0.1.0")
    pkg = CratePackage(metadata=meta, readme="# Long Readme")

    opts = RenderOptions(include_readme=False)
    md = render_crate_markdown(pkg, options=opts)
    assert "Long Readme" not in md


def test_crate_to_cognee_document():
    meta = CrateMetadata(name="tokio", description="Fast async", max_version="1.38.0")
    pkg = CratePackage(metadata=meta)

    doc = crate_to_cognee_document(pkg)
    assert doc["id"] == "crates:tokio"
    assert doc["title"] == "Rust Crate: tokio (v1.38.0)"
    assert doc["source"] == "crates"
    assert len(doc["data_id"]) == 64  # SHA-256


def test_crates_source_generator():
    client = CratesClient(transport=create_fake_transport())
    docs = list(crates_source(crate_names=["tokio"], client=client))
    assert len(docs) == 1
    assert docs[0]["id"] == "crates:tokio"
    assert docs[0][DOCUMENT_SOURCE_ATTR] is True


def test_crates_connector_fetch_documents():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        connector = CratesConnector(client=client)
        docs = await connector.fetch_documents(crate_names=["tokio"])
        assert len(docs) == 1
        assert docs[0]["id"] == "crates:tokio"

    asyncio.run(_run())


def test_cli_version_command(capsys):
    ret = cli_main(["version"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "0.1.0" in captured.out


def test_cli_benchmark_command(capsys):
    ret = cli_main(["benchmark"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "Cognee Crates Microbenchmark" in captured.out
    assert "Throughput:" in captured.out


def test_crates_client_repr():
    client = CratesClient()
    repr_str = repr(client)
    assert "CratesClient" in repr_str
    assert "crates.io" in repr_str


def test_crates_connector_records_sync_stats():
    async def _run():
        client = CratesClient(transport=create_fake_transport())
        connector = CratesConnector(client=client)
        docs = await connector.fetch_documents(crate_names=["tokio", "nonexistent"])
        assert len(docs) == 1
        assert connector.sync_stats["processed"] == 1
        assert connector.sync_stats["skipped"] == 1

    asyncio.run(_run())
