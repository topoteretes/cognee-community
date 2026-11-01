"""Hermetic unit tests for Cognee Hex.pm BEAM package connector."""

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from cognee_community_connector_hexpm.cli import main as cli_main
from cognee_community_connector_hexpm.client import (
    HexClient,
    HexPackageNotFoundError,
    HexRateLimitError,
)
from cognee_community_connector_hexpm.formatter import (
    hex_to_cognee_document,
    render_hex_markdown,
)
from cognee_community_connector_hexpm.hexpm import (
    DOCUMENT_SOURCE_ATTR,
    HexConnector,
    hexpm_source,
)
from cognee_community_connector_hexpm.models import (
    HexDependency,
    HexPackage,
    HexReleaseDetail,
    HexRetirement,
    RenderOptions,
)

SAMPLE_PHOENIX_PAYLOAD = {
    "name": "phoenix",
    "meta": {
        "description": "Peace of mind from prototype to production.",
        "licenses": ["MIT"],
        "links": {"GitHub": "https://github.com/phoenixframework/phoenix"},
        "maintainers": ["Chris McCord", "José Valim"],
    },
    "releases": [
        {
            "version": "1.7.14",
            "url": "https://hex.pm/api/packages/phoenix/releases/1.7.14",
            "has_docs": True,
            "inserted_at": "2026-05-10T12:00:00Z",
        }
    ],
    "retirements": {
        "1.6.0": {"reason": "security", "message": "Known CVE, upgrade to 1.6.1"},
    },
    "downloads": {"all": 25000000},
    "html_url": "https://hex.pm/packages/phoenix",
    "docs_html_url": "https://hexdocs.pm/phoenix",
}

SAMPLE_RELEASE_PAYLOAD = {
    "version": "1.7.14",
    "has_docs": True,
    "docs_html_url": "https://phoenix.hexdocs.pm/1.7.14/",
    "checksum": "abc123checksum",
    "requirements": {
        "plug": {"app": "plug", "requirement": "~> 1.14", "optional": False},
        "jason": {"app": "jason", "requirement": "~> 1.2", "optional": True},
    },
    "security_advisories": [
        {"id": "HEX-2026-001", "title": "Denial of service via malformed params"}
    ],
    "downloads": 500000,
    "inserted_at": "2026-05-10T12:00:00Z",
    "updated_at": "2026-05-10T12:00:00Z",
    "retirement": None,
}

SAMPLE_RETIRED_RELEASE_PAYLOAD = {
    "version": "1.6.0",
    "requirements": {},
    "retirement": {"reason": "security", "message": "Critical vulnerability"},
}


def create_fake_transport() -> httpx.MockTransport:
    """Create a mock transport simulating Hex.pm REST endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path == "/api/packages/phoenix":
            if request.headers.get("If-None-Match") == '"etag123"':
                return httpx.Response(304)
            return httpx.Response(200, json=SAMPLE_PHOENIX_PAYLOAD, headers={"ETag": '"etag123"'})

        if path == "/api/packages/phoenix/releases/1.7.14":
            return httpx.Response(200, json=SAMPLE_RELEASE_PAYLOAD)

        if path == "/api/packages/phoenix/releases/1.6.0":
            return httpx.Response(200, json=SAMPLE_RETIRED_RELEASE_PAYLOAD)

        if path == "/api/packages/rate-limited":
            return httpx.Response(429, headers={"X-RateLimit-Reset": "40"})

        if path == "/api/packages/nonexistent":
            return httpx.Response(404, json={"message": "Not Found"})

        if path == "/api/packages" and request.url.params.get("search"):
            return httpx.Response(
                200,
                json=[
                    {"name": "phoenix", "meta": {"description": "Web framework"}},
                    {"name": "phoenix_live_view", "meta": {"description": "Rich UI"}},
                ],
            )

        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_client_get_package_success():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        data, etag = await client.get_package("phoenix")
        assert data is not None
        assert data["name"] == "phoenix"
        assert etag == '"etag123"'

    asyncio.run(_run())


def test_client_get_package_empty_name_raises():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        with pytest.raises(ValueError, match="cannot be empty"):
            await client.get_package("   ")

    asyncio.run(_run())


def test_client_get_package_not_found():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        with pytest.raises(HexPackageNotFoundError, match="not found on Hex.pm"):
            await client.get_package("nonexistent")

    asyncio.run(_run())


def test_client_get_package_etag_304():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        data, etag = await client.get_package("phoenix", etag='"etag123"')
        assert data is None
        assert etag == '"etag123"'

    asyncio.run(_run())


def test_client_rate_limit_429():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        with pytest.raises(HexRateLimitError, match="Reset in 40s"):
            await client.get_package("rate-limited")

    asyncio.run(_run())


def test_client_get_release_success():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        rel = await client.get_release("phoenix", "1.7.14")
        assert rel["version"] == "1.7.14"
        assert "plug" in rel["requirements"]

    asyncio.run(_run())


def test_client_search_packages():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        results = await client.search_packages("phoenix")
        assert len(results) == 2
        assert results[0]["name"] == "phoenix"

    asyncio.run(_run())


def test_client_fetch_package_entity_full():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        pkg = await client.fetch_package_entity("phoenix")
        assert pkg.name == "phoenix"
        assert pkg.latest_version == "1.7.14"
        assert pkg.latest_release is not None
        assert len(pkg.latest_release.requirements) == 2
        assert "1.6.0" in pkg.all_retirements
        assert pkg.all_retirements["1.6.0"].reason == "security"

    asyncio.run(_run())


def test_client_fetch_packages_batch_skips_missing():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        packages = await client.fetch_packages_batch(["phoenix", "nonexistent"])
        assert len(packages) == 1
        assert packages[0].name == "phoenix"

    asyncio.run(_run())


def test_parse_mix_lock():
    sample_mix_lock = """
    %{
      "bandit": {:hex, :bandit, "1.5.7", "checksum", [:mix], []},
      "jason": {:hex, :jason, "1.4.4", "checksum", [:mix], []},
      "phoenix": {:hex, :phoenix, "1.7.14", "checksum", [:mix], []}
    }
    """
    with tempfile.NamedTemporaryFile("w+", suffix=".lock", delete=False) as f:
        f.write(sample_mix_lock)
        temp_path = f.name

    try:
        packages = HexClient.parse_mix_lock(temp_path)
        assert packages == ["bandit", "jason", "phoenix"]
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_parse_mix_lock_missing_file():
    with pytest.raises(FileNotFoundError):
        HexClient.parse_mix_lock("missing_mix.lock")


def test_render_hex_markdown():
    rel = HexReleaseDetail(
        version="1.7.14",
        requirements=[
            HexDependency(name="plug", app="plug", requirement="~> 1.14"),
            HexDependency(name="jason", app="jason", requirement="~> 1.2", optional=True),
        ],
    )
    retirements = {
        "1.6.0": HexRetirement(version="1.6.0", reason="security", message="Patched in 1.6.1")
    }
    pkg = HexPackage(
        name="phoenix",
        description="Peace of mind from prototype to production.",
        licenses=["MIT"],
        latest_version="1.7.14",
        latest_release=rel,
        all_retirements=retirements,
    )

    md = render_hex_markdown(pkg)
    assert "# Hex Package: phoenix (v1.7.14)" in md
    assert "## Upstream Retirements History" in md
    assert "| `1.6.0` | `security` | Patched in 1.6.1 |" in md
    assert "## Dependencies" in md
    assert "| `plug` | `plug` | `~> 1.14` | No |" in md
    assert "| `jason` | `jason` | `~> 1.2` | Yes |" in md


def test_render_hex_markdown_with_retirement_warning():
    retired_rel = HexReleaseDetail(
        version="1.6.0",
        retirement=HexRetirement(
            version="1.6.0", reason="security", message="Do not use in production"
        ),
    )
    pkg = HexPackage(
        name="phoenix",
        description="Old release",
        latest_version="1.6.0",
        latest_release=retired_rel,
    )

    md = render_hex_markdown(pkg)
    assert "⚠️ **RETIRED RELEASE:** Version `1.6.0` has been retired upstream." in md
    assert "**Reason:** `security`" in md
    assert "Do not use in production" in md


def test_hex_to_cognee_document():
    pkg = HexPackage(name="phoenix", description="Web framework", latest_version="1.7.14")
    doc = hex_to_cognee_document(pkg)

    assert doc["id"] == "hexpm:phoenix"
    assert doc["title"] == "Hex Package: phoenix (v1.7.14)"
    assert doc["source"] == "hexpm"
    assert doc["_deleted"] is False
    assert len(doc["data_id"]) == 64


def test_hex_to_cognee_document_retirement_tombstone():
    retired_rel = HexReleaseDetail(
        version="1.6.0",
        retirement=HexRetirement(version="1.6.0", reason="deprecated"),
    )
    pkg = HexPackage(
        name="phoenix",
        description="Web framework",
        latest_version="1.6.0",
        latest_release=retired_rel,
    )

    opts = RenderOptions(exclude_retired=True)
    doc = hex_to_cognee_document(pkg, options=opts)
    assert doc["_deleted"] is True


def test_hexpm_source_generator():
    client = HexClient(transport=create_fake_transport())
    docs = list(hexpm_source(package_names=["phoenix"], client=client))
    assert len(docs) == 1
    assert docs[0]["id"] == "hexpm:phoenix"
    assert docs[0][DOCUMENT_SOURCE_ATTR] is True


def test_hex_connector_fetch_documents():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        connector = HexConnector(client=client)
        docs = await connector.fetch_documents(package_names=["phoenix"])
        assert len(docs) == 1
        assert docs[0]["id"] == "hexpm:phoenix"

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
    assert "Cognee Hex.pm Microbenchmark" in captured.out
    assert "Throughput:" in captured.out


def test_hex_client_repr_redacts_api_key():
    client = HexClient(api_key="secret_hex_token_12345")
    repr_str = repr(client)
    assert "secret_hex_token" not in repr_str
    assert "'***'" in repr_str


def test_hex_connector_records_sync_stats():
    async def _run():
        client = HexClient(transport=create_fake_transport())
        connector = HexConnector(client=client)
        docs = await connector.fetch_documents(package_names=["phoenix", "nonexistent"])
        assert len(docs) == 1
        assert connector.sync_stats["processed"] == 1
        assert connector.sync_stats["skipped"] == 1

    asyncio.run(_run())
