"""CLI interface for the Cognee Crates.io connector."""

import argparse
import asyncio
import json
import sys
import time
import tracemalloc

from cognee_community_connector_crates.client import CratesClient
from cognee_community_connector_crates.formatter import (
    crate_to_cognee_document,
    render_crate_markdown,
)
from cognee_community_connector_crates.models import RenderOptions


def cmd_fetch(args: argparse.Namespace) -> int:
    """Fetch and render a crate from crates.io."""
    client = CratesClient()
    options = RenderOptions(include_readme=not args.no_readme)

    try:
        pkg = asyncio.run(client.fetch_crate_package(args.crate_name, version=args.version))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.json:
        doc = crate_to_cognee_document(pkg, options=options)
        print(json.dumps(doc, indent=2))
    else:
        print(render_crate_markdown(pkg, options=options))
    return 0


def cmd_from_cargo(args: argparse.Namespace) -> int:
    """Extract and optionally fetch all crate dependencies from Cargo.toml."""
    try:
        crates = CratesClient.parse_cargo_toml(
            args.cargo_path,
            include_dev=args.include_dev,
            include_build=args.include_build,
        )
    except Exception as e:
        print(f"Error parsing Cargo.toml: {e}", file=sys.stderr)
        return 1

    print(f"Found {len(crates)} dependencies in {args.cargo_path}:")
    for name in crates:
        print(f"  - {name}")

    if args.fetch:
        print("\nFetching package details...")
        client = CratesClient()
        packages = asyncio.run(client.fetch_packages_batch(crates))
        print(f"Successfully fetched {len(packages)}/{len(crates)} packages.")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Search for Rust crates on crates.io."""
    client = CratesClient()
    try:
        results = asyncio.run(client.search_crates(args.query, limit=args.limit))
    except Exception as e:
        print(f"Search error: {e}", file=sys.stderr)
        return 1

    print(f"Top {len(results)} crates matching '{args.query}':")
    for c in results:
        name = c.get("name", "unknown")
        max_ver = c.get("max_version", "")
        desc = c.get("description", "").strip().replace("\n", " ")
        if len(desc) > 80:
            desc = desc[:77] + "..."
        print(f"  * {name} (v{max_ver}) - {desc}")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    """Run an offline microbenchmark measuring formatting latency and RAM."""
    from cognee_community_connector_crates.models import (
        CrateDependency,
        CrateMetadata,
        CratePackage,
        CrateVersion,
    )

    dummy_meta = CrateMetadata(
        name="tokio",
        description="An event-driven, non-blocking I/O platform for writing asynchronous I/O apps.",
        max_version="1.38.0",
        documentation="https://docs.rs/tokio",
        repository="https://github.com/tokio-rs/tokio",
        homepage="https://tokio.rs",
        downloads=185_000_000,
        keywords=["async", "io", "non-blocking", "futures"],
        categories=["asynchronous", "network-programming"],
    )
    dummy_ver = CrateVersion(
        num="1.38.0",
        license="MIT",
        downloads=15_000_000,
        features={"default": ["rt", "net"], "full": ["fs", "io-util", "sync", "time"]},
    )
    dummy_deps = [
        CrateDependency(crate_id="bytes", req="^1.4", kind="normal"),
        CrateDependency(crate_id="pin-project-lite", req="^0.2", kind="normal"),
        CrateDependency(crate_id="tempfile", req="^3.0", kind="dev"),
    ]
    dummy_readme = "# Tokio\nA runtime for writing reliable, asynchronous applications with Rust."

    pkg = CratePackage(
        metadata=dummy_meta,
        latest_version=dummy_ver,
        dependencies=dummy_deps,
        readme=dummy_readme,
    )

    iterations = 2000
    tracemalloc.start()
    t0 = time.perf_counter()

    for _ in range(iterations):
        _ = crate_to_cognee_document(pkg)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    per_doc_ms = elapsed_ms / iterations
    docs_per_sec = (iterations / elapsed_ms) * 1000
    peak_kb = peak_bytes / 1024

    print("=== Cognee Crates Microbenchmark ===")
    print(f"Iterations:          {iterations}")
    print(f"Total time:          {elapsed_ms:.2f} ms")
    print(f"Per-document time:   {per_doc_ms:.4f} ms")
    print(f"Throughput:          {docs_per_sec:,.0f} docs/sec")
    print(f"Peak memory delta:   {peak_kb:.2f} KB")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for cognee-crates."""
    parser = argparse.ArgumentParser(
        prog="cognee-crates",
        description="Cognee Crates.io Rust package registry data-source connector CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # fetch
    p_fetch = subparsers.add_parser("fetch", help="Fetch a crate from crates.io")
    p_fetch.add_argument("crate_name", help="Name of the Rust crate")
    p_fetch.add_argument("--version", help="Specific release version (default: latest)")
    p_fetch.add_argument("--no-readme", action="store_true", help="Omit README text")
    p_fetch.add_argument("--json", action="store_true", help="Output Cognee JSON document")
    p_fetch.set_defaults(func=cmd_fetch)

    # from-cargo
    p_cargo = subparsers.add_parser("from-cargo", help="Read dependencies from Cargo.toml")
    p_cargo.add_argument("cargo_path", help="Path to Cargo.toml")
    p_cargo.add_argument("--include-dev", action="store_true", help="Include dev-dependencies")
    p_cargo.add_argument("--include-build", action="store_true", help="Include build-dependencies")
    p_cargo.add_argument("--fetch", action="store_true", help="Fetch metadata for all dependencies")
    p_cargo.set_defaults(func=cmd_from_cargo)

    # search
    p_search = subparsers.add_parser("search", help="Search crates on crates.io")
    p_search.add_argument("query", help="Search keyword")
    p_search.add_argument("--limit", type=int, default=10, help="Maximum number of hits")
    p_search.set_defaults(func=cmd_search)

    # benchmark
    p_bench = subparsers.add_parser("benchmark", help="Run local memory and latency benchmark")
    p_bench.set_defaults(func=cmd_benchmark)

    # version
    p_ver = subparsers.add_parser("version", help="Show connector version")
    p_ver.set_defaults(func=lambda _: print("cognee-community-connector-crates 0.1.0") or 0)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
