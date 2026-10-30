import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from cognee_community_connector_npm.npm import (
    NpmConnector,
    NpmPackage,
)


@pytest.fixture
def mock_package_data():
    return {
        "name": "express",
        "dist-tags": {"latest": "4.19.2"},
        "description": "Fast, unopinionated, minimalist web framework for node.",
        "license": "MIT",
        "homepage": "http://expressjs.com/",
        "repository": {"type": "git", "url": "git+https://github.com/expressjs/express.git"},
        "readme": "# Express\n\nFast web framework.",
        "versions": {
            "4.19.2": {
                "name": "express",
                "version": "4.19.2",
                "dependencies": {"accepts": "~1.3.8", "cookie": "0.6.0"},
                "devDependencies": {"mocha": "10.4.0"},
                "keywords": ["express", "framework", "rest"],
            }
        },
    }


def test_fetch_single_package(mock_package_data):
    async def run_test():
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=mock_package_data)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = NpmConnector(packages=["express"], session=mock_session)
        pkg = await connector.fetch_package("express")
        assert pkg is not None
        assert pkg.name == "express"
        assert pkg.latest_version == "4.19.2"
        assert pkg.license == "MIT"
        assert "accepts" in pkg.dependencies
        assert "cookie" in pkg.dependencies

        md = pkg.to_markdown()
        assert "# npm Package: express (v4.19.2)" in md
        assert "**Registry Link:** https://www.npmjs.com/package/express" in md
        assert "**License:** MIT" in md
        assert "`accepts`: ~1.3.8" in md
        assert "Fast web framework." in md

    asyncio.run(run_test())


def test_fetch_scoped_package():
    async def run_test():
        scoped_data = {
            "name": "@angular/core",
            "dist-tags": {"latest": "18.0.0"},
            "description": "Angular core framework.",
            "license": "MIT",
            "versions": {"18.0.0": {"name": "@angular/core", "version": "18.0.0", "peerDependencies": {"rxjs": "^7.8.0"}}},
        }

        mock_session = MagicMock()
        called_urls = []

        def mock_get(url, headers=None):
            called_urls.append(url)
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value=scoped_data)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.get.side_effect = mock_get
        mock_session.closed = False

        connector = NpmConnector(packages=["@angular/core"], session=mock_session)
        pkg = await connector.fetch_package("@angular/core")
        assert pkg is not None
        assert pkg.name == "@angular/core"
        assert pkg.latest_version == "18.0.0"
        assert "rxjs" in pkg.peer_dependencies
        # Ensure properly URL-encoded path was hit
        assert any("@angular%2Fcore" in u for u in called_urls)

    asyncio.run(run_test())


def test_fetch_package_404_handling():
    async def run_test():
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 404
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = NpmConnector(packages=["nonexistent-pkg-404xyz"], session=mock_session)
        pkg = await connector.fetch_package("nonexistent-pkg-404xyz")
        assert pkg is None

    asyncio.run(run_test())


def test_rate_limit_handling():
    async def run_test():
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 429
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = cm
        mock_session.closed = False

        connector = NpmConnector(packages=["rate-limited-pkg"], session=mock_session)
        with pytest.raises(RuntimeError, match="npm registry rate limit exceeded"):
            await connector.fetch_package("rate-limited-pkg")

    asyncio.run(run_test())


def test_to_documents(mock_package_data):
    async def run_test():
        mock_session = MagicMock()

        def mock_get(url, headers=None):
            mock_resp = MagicMock()
            if "express" in url:
                mock_resp.status = 200
                mock_resp.json = AsyncMock(return_value=mock_package_data)
            else:
                mock_resp.status = 404
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session.get.side_effect = mock_get
        mock_session.closed = False

        connector = NpmConnector(packages=["express", "invalid-package-xyz"], session=mock_session)
        docs = await connector.to_documents()
        assert len(docs) == 1
        assert "# npm Package: express (v4.19.2)" in docs[0]

    asyncio.run(run_test())
