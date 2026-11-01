"""CLI interface for the Cognee Hex.pm BEAM package connector."""

import argparse
import asyncio
import json
import sys
import time
import tracemalloc

from cognee_community_connector_hexpm.client import HexClient
from cognee_community_connector_hexpm.formatter import (
    hex_to_cognee_document,
    render_hex_markdown,
)
from cognee_community_connector_hexpm.models import RenderOptions


def cmd_fetch(args: argparse.Namespace) -> int:
    """Fetch and render a package from Hex.pm."""
    client = HexClient()
    options = RenderOptions(exclude_retired=args.exclude_retired)

    try:
        pkg = asyncio.run(client.fetch_package_entity(args.package_name, version=args.version))
    except Exception as e:
        print(f"Error fetching '{args.package_name}': {e}", file=sys.stderr)
        return 1

    if args.json:
        doc = hex_to_cognee_document(pkg, options=options)
        print(json.dumps(doc, indent=2))
    else:
        print(render_hex_markdown(pkg, options=options))
    return 0


def cmd_from_mix(args: argparse.Namespace) -> int:
    """Extract and optionally fetch all packages from Elixir mix.lock."""
    try:
        packages = HexClient.parse_mix_lock(args.mix_path)
    except Exception as e:
        print(f"Error parsing mix.lock: {e}", file=sys.stderr)
        return 1

    print(f"Found {len(packages)} Hex packages in {args.mix_path}:")
    for name in packages:
        print(f"  - {name}")

    if args.fetch:
        print("\nFetching Hex package details...")
        client = HexClient()
        entities = asyncio.run(client.fetch_packages_batch(packages))
        print(f"Successfully fetched {len(entities)}/{len(packages)} packages.")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Search for packages on Hex.pm."""
    client = HexClient()
    try:
        results = asyncio.run(client.search_packages(args.query))
    except Exception as e:
        print(f"Search error: {e}", file=sys.stderr)
        return 1

    hits = results[: args.limit]
    print(f"Top {len(hits)} Hex packages matching '{args.query}':")
    for p in hits:
        name = p.get("name", "unknown")
        meta = p.get("meta", {})
        desc = meta.get("description", "").strip().replace("\n", " ")
        if len(desc) > 80:
            desc = desc[:77] + "..."
        print(f"  * {name} - {desc}")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    """Run an offline microbenchmark measuring formatting latency and RAM."""
    from cognee_community_connector_hexpm.models import (
        HexDependency,
        HexPackage,
        HexReleaseDetail,
        HexRetirement,
    )

    dummy_rel = HexReleaseDetail(
        version="1.7.14",
        has_docs=True,
        docs_html_url="https://phoenix.hexdocs.pm/1.7.14/",
        checksum="8f4e2c91a0b3c4d5",
        requirements=[
            HexDependency(name="plug", app="plug", requirement="~> 1.14"),
            HexDependency(name="jason", app="jason", requirement="~> 1.2", optional=True),
        ],
        retirement=None,
    )

    dummy_retirements = {
        "1.6.0": HexRetirement(version="1.6.0", reason="security", message="Fixed in 1.6.1"),
    }

    pkg = HexPackage(
        name="phoenix",
        description="Peace of mind from prototype to production.",
        licenses=["MIT"],
        links={"GitHub": "https://github.com/phoenixframework/phoenix"},
        maintainers=["Chris McCord", "José Valim"],
        latest_version="1.7.14",
        latest_release=dummy_rel,
        all_retirements=dummy_retirements,
    )

    iterations = 2000
    tracemalloc.start()
    t0 = time.perf_counter()

    for _ in range(iterations):
        _ = hex_to_cognee_document(pkg)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    per_doc_ms = elapsed_ms / iterations
    docs_per_sec = (iterations / elapsed_ms) * 1000
    peak_kb = peak_bytes / 1024

    print("=== Cognee Hex.pm Microbenchmark ===")
    print(f"Iterations:          {iterations}")
    print(f"Total time:          {elapsed_ms:.2f} ms")
    print(f"Per-document time:   {per_doc_ms:.4f} ms")
    print(f"Throughput:          {docs_per_sec:,.0f} docs/sec")
    print(f"Peak memory delta:   {peak_kb:.2f} KB")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for cognee-hexpm."""
    parser = argparse.ArgumentParser(
        prog="cognee-hexpm",
        description="Cognee Hex.pm BEAM package registry data-source connector CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # fetch
    p_fetch = subparsers.add_parser("fetch", help="Fetch a package from Hex.pm")
    p_fetch.add_argument("package_name", help="Name of the Hex package (e.g. phoenix)")
    p_fetch.add_argument("--version", help="Specific release version (default: latest)")
    p_fetch.add_argument("--exclude-retired", action="store_true", help="Exclude retired releases")
    p_fetch.add_argument("--json", action="store_true", help="Output Cognee JSON document")
    p_fetch.set_defaults(func=cmd_fetch)

    # from-mix
    p_mix = subparsers.add_parser("from-mix", help="Read dependencies from Elixir mix.lock")
    p_mix.add_argument("mix_path", help="Path to mix.lock")
    p_mix.add_argument("--fetch", action="store_true", help="Fetch metadata for all packages")
    p_mix.set_defaults(func=cmd_from_mix)

    # search
    p_search = subparsers.add_parser("search", help="Search packages on Hex.pm")
    p_search.add_argument("query", help="Search keyword")
    p_search.add_argument("--limit", type=int, default=10, help="Maximum number of hits")
    p_search.set_defaults(func=cmd_search)

    # benchmark
    p_bench = subparsers.add_parser("benchmark", help="Run local memory and latency benchmark")
    p_bench.set_defaults(func=cmd_benchmark)

    # version
    p_ver = subparsers.add_parser("version", help="Show connector version")
    p_ver.set_defaults(func=lambda _: print("cognee-community-connector-hexpm 0.1.0") or 0)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
