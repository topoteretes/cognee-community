"""Caddy Web Server (Caddyfile) reverse proxy & routing data-source connector for cognee.

Parses ``Caddyfile`` configurations (site blocks, addresses, reverse proxy upstreams,
path matchers, security headers, routing handlers, and TLS directives) into structured
documents ready for :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_caddy import caddy_source

    await cognee.remember(
        caddy_source("/path/to/Caddyfile"),
        dataset_name="web_routing_topology",
        max_rows_per_table=0,  # ingest full configuration snapshot
    )

Design
------
* **Primary key** — ``id = "caddy:site:{primary_address_slug}"``. Stable across edits
  so incremental updates only re-cognify changed site blocks.
* **Snapshot sync (``replace``)** — Web server configs represent the active routing rules.
  Using ``write_disposition="replace"`` ensures that any site or route removed from
  a ``Caddyfile`` is automatically purged from the graph and vector index via cognee's
  orphan cleanup.
* **Agent-Optimized Routing Representation** — Reverse proxy destinations, port mappings,
  path matchers (``@api``, ``handle_path``), and header rewrites are structured so
  AI DevOps agents can accurately query backend routing without hallucinations.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("caddy_connector")
except ImportError:
    import logging
    logger = logging.getLogger("caddy_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

CADDY_TABLE_NAME = "caddy_site_blocks"
CADDY_SOURCE_NAME = "caddy"


def _slugify(text: str) -> str:
    """Sanitize address string for stable primary key construction."""
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\.:]+", "_", text.strip().lower())
    return cleaned.strip("_") or "default"


def parse_caddyfile_content(caddyfile_text: str) -> list[dict[str, Any]]:
    """Parse Caddyfile text into structured site blocks and directives."""
    site_blocks: list[dict[str, Any]] = []
    lines = [line.split("#")[0].split("//")[0].rstrip() for line in caddyfile_text.splitlines()]

    i = 0
    while i < len(lines):
        raw_line = lines[i].strip()

        if not raw_line:
            i += 1
            continue

        # Skip global options block: { ... } or snippet: (snippet) { ... }
        if raw_line.startswith("{") or (raw_line.startswith("(") and "{" in raw_line):
            open_c = len([w for w in raw_line.split() if w.endswith("{") or w == "{"])
            close_c = len([w for w in raw_line.split() if w.startswith("}") or w == "}"])
            depth = open_c - close_c
            while i < len(lines) - 1 and depth > 0:
                i += 1
                l = lines[i].strip()
                open_c = len([w for w in l.split() if w.endswith("{") or w == "{"])
                close_c = len([w for w in l.split() if w.startswith("}") or w == "}"])
                depth += open_c - close_c
            i += 1
            continue

        # Check for site block with '{'
        if "{" in raw_line:
            header_part = raw_line[: raw_line.find("{")].strip()
            addresses = [a.strip() for a in header_part.replace(",", " ").split() if a.strip()]

            block_lines: list[str] = []
            open_c = len([w for w in raw_line[raw_line.find("{") :].split() if w.endswith("{") or w == "{"])
            close_c = len([w for w in raw_line[raw_line.find("{") :].split() if w.startswith("}") or w == "}"])
            depth = open_c - close_c

            while i < len(lines) - 1 and depth > 0:
                i += 1
                l = lines[i].strip()
                open_c = len([w for w in l.split() if w.endswith("{") or w == "{"])
                close_c = len([w for w in l.split() if w.startswith("}") or w == "}"])
                depth += open_c - close_c
                if depth > 0 and l:
                    block_lines.append(l)

            if addresses:
                site_blocks.append(_build_site_dict(addresses, block_lines))
        else:
            # Single-line directive site block or shorthand
            tokens = raw_line.split()
            if tokens and not tokens[0].startswith("}"):
                site_blocks.append(_build_site_dict([tokens[0]], tokens[1:]))

        i += 1

    return site_blocks


def _build_site_dict(addresses: list[str], block_lines: list[str]) -> dict[str, Any]:
    reverse_proxies: list[dict[str, str]] = []
    headers: list[str] = []
    handlers: list[str] = []
    tls_info: str = ""
    root_path: str = ""

    for b in block_lines:
        tokens = b.split()
        if not tokens:
            continue
        directive = tokens[0]

        if directive == "reverse_proxy":
            args = tokens[1:]
            matcher = ""
            upstreams: list[str] = []
            for a in args:
                if a.startswith("@") or a.startswith("/"):
                    matcher = a
                elif not a.startswith("{") and not a.endswith("}"):
                    upstreams.append(a)
            upstream_str = ", ".join(upstreams) if upstreams else "upstream"
            reverse_proxies.append({
                "raw": b,
                "matcher": matcher,
                "upstreams": upstream_str,
            })
        elif directive == "root":
            root_path = " ".join(tokens[1:])
        elif directive == "tls":
            tls_info = " ".join(tokens[1:])
        elif directive in ("header", "header_up", "header_down"):
            headers.append(b)
        elif directive.startswith("@"):
            handlers.append(b)
        elif directive in ("handle", "handle_path", "route", "redir", "file_server", "encode"):
            handlers.append(b)

    return {
        "primary_address": addresses[0],
        "addresses": addresses,
        "reverse_proxies": reverse_proxies,
        "root_path": root_path,
        "tls_info": tls_info,
        "headers": headers,
        "handlers": handlers,
        "raw_block": "\n".join(block_lines),
    }


def _format_caddy_document(site: dict[str, Any], file_name: str) -> dict[str, Any]:
    primary = site["primary_address"]
    addresses = site["addresses"]
    proxies = site["reverse_proxies"]
    root = site.get("root_path")
    tls = site.get("tls_info")
    headers = site.get("headers", [])
    handlers = site.get("handlers", [])

    lines: list[str] = [
        f"Caddy Web Server Site Block: {primary}",
        f"Caddyfile: {file_name}",
        f"Addresses: {', '.join(addresses)}",
    ]

    if tls:
        lines.append(f"TLS Policy: {tls}")
    if root:
        lines.append(f"Web Root: {root}")

    if proxies:
        lines.append("Reverse Proxy Upstreams:")
        for p in proxies:
            matcher_str = f" (matcher: {p['matcher']})" if p["matcher"] else ""
            lines.append(f"  - Upstream: {p['upstreams']}{matcher_str}")

    if handlers:
        lines.append("Handlers & Matchers:")
        for h in handlers:
            lines.append(f"  - {h}")

    if headers:
        lines.append("Header Configurations:")
        for h in headers:
            lines.append(f"  - {h}")

    content_str = "\n".join(lines)
    primary_key = f"caddy:site:{_slugify(primary)}"
    title = f"[Caddy Site] {primary}"

    return {
        "id": primary_key,
        "title": title,
        "content": content_str,
        "primary_address": primary_address_or_default(primary),
        "addresses": addresses,
        "reverse_proxies_count": len(proxies),
        "caddyfile": file_name,
    }


def primary_address_or_default(addr: str) -> str:
    return addr if addr else "default"


def iter_caddy_site_blocks(caddyfile_path: str | os.PathLike) -> Iterator[dict[str, Any]]:
    """Yield parsed site blocks from a Caddyfile or directory containing Caddyfiles."""
    path = Path(caddyfile_path)
    if not path.exists():
        raise FileNotFoundError(f"Caddyfile path does not exist: {path}")

    files: list[Path] = []
    if path.is_file():
        files.append(path)
    elif path.is_dir():
        patterns = ["*Caddyfile*", "*.caddy"]
        for pat in patterns:
            files.extend(sorted(path.glob(f"**/{pat}")))
        files = list(dict.fromkeys(files))

    if not files:
        logger.warning("Caddy connector: no Caddyfiles found in %s", path)
        return

    count = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Caddy connector: failed to read %s: %s", f, exc)
            continue

        sites = parse_caddyfile_content(content)
        for s in sites:
            count += 1
            yield _format_caddy_document(s, f.name)

    logger.info("Caddy connector: successfully parsed %d site block(s) from %s", count, path)


def caddy_source(caddyfile_path: str | os.PathLike):
    """Return a ``dlt`` resource over Caddy site blocks for :func:`cognee.remember`.

    Args:
        caddyfile_path: Path to a ``Caddyfile`` or directory containing Caddyfiles.

    Returns:
        A ``dlt`` resource (``caddy_site_blocks``) with ``write_disposition="replace"``
        and document source marker (``cognee_document_source = "caddy"``).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Caddy connector requires the dlt extra: pip install "cognee[dlt]".'
        ) from exc

    source_path = str(caddyfile_path)

    @dlt.resource(
        name=CADDY_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def caddy_site_blocks():
        yield from iter_caddy_site_blocks(source_path)

    setattr(caddy_site_blocks, DOCUMENT_SOURCE_ATTR, CADDY_SOURCE_NAME)
    return caddy_site_blocks
