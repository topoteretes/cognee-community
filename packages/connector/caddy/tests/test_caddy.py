"""Unit tests for the Caddy Web Server data-source connector.

Tests use self-contained Caddyfile fixtures generated in ``tmp_path``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_caddy.caddy import (
    CADDY_SOURCE_NAME,
    CADDY_TABLE_NAME,
    DOCUMENT_SOURCE_ATTR,
    caddy_source,
    iter_caddy_site_blocks,
    parse_caddyfile_content,
)

SAMPLE_CADDYFILE = """
# Global configuration options
{
  email certs@example.com
}

# Snippet
(security_headers) {
  header {
    X-Frame-Options "DENY"
  }
}

# Production site
example.com, www.example.com {
  root * /var/www/html
  file_server
  encode gzip zstd
  tls certs@example.com

  @api {
    path /api/*
  }
  reverse_proxy @api 127.0.0.1:8080 {
    header_up Host {upstream_hostport}
  }

  handle_path /assets/* {
    file_server
  }

  reverse_proxy 127.0.0.1:3000
}

# Internal monitoring dashboard
metrics.internal:9090 {
  reverse_proxy 10.0.0.12:9090
}
"""


def test_parse_caddyfile_content():
    blocks = parse_caddyfile_content(SAMPLE_CADDYFILE)
    assert len(blocks) == 2

    prod_site = blocks[0]
    assert prod_site["primary_address"] == "example.com"
    assert "www.example.com" in prod_site["addresses"]
    assert prod_site["tls_info"] == "certs@example.com"
    assert prod_site["root_path"] == "* /var/www/html"

    proxies = prod_site["reverse_proxies"]
    assert len(proxies) == 2
    assert proxies[0]["matcher"] == "@api"
    assert proxies[0]["upstreams"] == "127.0.0.1:8080"
    assert proxies[1]["upstreams"] == "127.0.0.1:3000"

    metrics_site = blocks[1]
    assert metrics_site["primary_address"] == "metrics.internal:9090"
    assert metrics_site["reverse_proxies"][0]["upstreams"] == "10.0.0.12:9090"


def test_iter_caddy_site_blocks(tmp_path):
    caddy_file = tmp_path / "Caddyfile"
    caddy_file.write_text(SAMPLE_CADDYFILE)

    docs = list(iter_caddy_site_blocks(caddy_file))
    assert len(docs) == 2

    prod_doc = next(d for d in docs if "example.com" in d["title"])
    assert prod_doc["id"] == "caddy:site:example.com"
    assert "127.0.0.1:8080 (matcher: @api)" in prod_doc["content"]
    assert "127.0.0.1:3000" in prod_doc["content"]
    assert "TLS Policy: certs@example.com" in prod_doc["content"]


def test_orphan_cleanup_simulation(tmp_path):
    """Removing a site block from Caddyfile drops it from the yielded records."""
    f1 = tmp_path / "Caddyfile.v1"
    f1.write_text(SAMPLE_CADDYFILE)

    # v2 drops metrics.internal site
    f2_text = SAMPLE_CADDYFILE.split("# Internal monitoring dashboard")[0]
    f2 = tmp_path / "Caddyfile.v2"
    f2.write_text(f2_text)

    docs_v1 = list(iter_caddy_site_blocks(f1))
    docs_v2 = list(iter_caddy_site_blocks(f2))

    assert len(docs_v1) == 2
    assert len(docs_v2) == 1

    titles_v1 = {d["title"] for d in docs_v1}
    titles_v2 = {d["title"] for d in docs_v2}

    assert "[Caddy Site] metrics.internal:9090" in titles_v1
    assert "[Caddy Site] metrics.internal:9090" not in titles_v2


def test_directory_with_multiple_caddyfiles(tmp_path):
    sub = tmp_path / "sites"
    sub.mkdir()
    (sub / "site1.caddy").write_text("site1.com {\n reverse_proxy 127.0.0.1:8001\n}")
    (sub / "site2.caddy").write_text("site2.com {\n reverse_proxy 127.0.0.1:8002\n}")

    docs = list(iter_caddy_site_blocks(sub))
    assert len(docs) == 2


def test_missing_and_empty_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(iter_caddy_site_blocks(tmp_path / "NonexistentCaddyfile"))

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert list(iter_caddy_site_blocks(empty_dir)) == []


def test_caddy_source_resource():
    mock_dlt = MagicMock()

    def mock_resource_decorator(*args, **kwargs):
        def decorator(fn):
            wrapper = MagicMock()
            wrapper.__name__ = fn.__name__
            wrapper._kwargs = kwargs
            return wrapper
        return decorator

    mock_dlt.resource = mock_resource_decorator

    with patch.dict("sys.modules", {"dlt": mock_dlt}):
        resource = caddy_source("/dummy/Caddyfile")

        assert getattr(resource, DOCUMENT_SOURCE_ATTR) == CADDY_SOURCE_NAME
        assert resource._kwargs.get("name") == CADDY_TABLE_NAME
        assert resource._kwargs.get("primary_key") == "id"
        assert resource._kwargs.get("write_disposition") == "replace"
