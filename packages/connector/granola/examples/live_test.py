"""Live test script for Granola connector with real credentials.

Usage:
    export GRANOLA_API_KEY="grn_..."
    # Optional for Cognee graph extraction and search:
    export LLM_API_KEY="sk-..."

    uv run python examples/live_test.py

All Granola API access goes through the dlt REST API source (``granola_source``);
no hand-written HTTP calls are made here.
"""

import asyncio
import os
import sys

from cognee_community_connector_granola import granola_source


def _print_empty_reasons(file=None) -> None:
    """Print common reasons the Granola API returns no notes."""
    out = file or sys.stdout
    print("\nCommon reasons in Granola:", file=out)
    print("1. Key Scope: 'Personal' vs 'Workspace' key must match the notes:", file=out)
    print("   - Personal keys only access owned/shared notes.", file=out)
    print("   - Workspace keys only access spaces with API access enabled.", file=out)
    print("2. Processing: only notes with an AI summary AND transcript are", file=out)
    print("   returned (hand-written notes without a meeting are excluded).", file=out)
    print("3. Account: ensure the key belongs to the notes' account.", file=out)


async def main() -> None:
    api_key = os.environ.get("GRANOLA_API_KEY")
    if not api_key:
        print("ERROR: GRANOLA_API_KEY environment variable is not set.", file=sys.stderr)
        print("Please export it before running:", file=sys.stderr)
        print('    export GRANOLA_API_KEY="grn_..."', file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print("STEP 1: Extracting notes via the dlt REST API source")
    print("=" * 60)

    try:
        source = granola_source(api_key=api_key, include_transcript=True)
    except Exception as exc:
        print(f"Failed to initialize source: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Running DLT extraction pipeline...")
    notes_found = 0

    try:
        for row in source.resources["granola_notes"]:
            notes_found += 1
            print(f"\n--- Note #{notes_found} ---")
            print(f"Title: {row.get('title')}")
            print(f"URL:   {row.get('url')}")
            print(f"ID:    {row.get('id')}")
            content = row.get("content", "")
            preview = content[:400] + ("..." if len(content) > 400 else "")
            print("Rendered Markdown Preview:\n" + preview)
    except Exception as exc:
        print(f"\nDLT Extraction failed: {exc}", file=sys.stderr)
        _print_empty_reasons(file=sys.stderr)
        sys.exit(1)

    if notes_found == 0:
        print("\nNo notes found to ingest into memory.")
        _print_empty_reasons()
        return

    print(f"\nSuccessfully extracted {notes_found} note(s) from your Granola account!")

    # Check if user also configured Cognee LLM key
    llm_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not llm_key:
        print("\n" + "=" * 60)
        print("DLT extraction succeeded!")
        print("To also test ingestion into Cognee memory and AI search, export your LLM key:")
        print('    export LLM_API_KEY="sk-..."')
        print("    uv run python examples/live_test.py")
        print("=" * 60)
        return

    print("\n" + "=" * 60)
    print("STEP 2: Ingesting into Cognee Memory & Running Search")
    print("=" * 60)

    import cognee

    dataset_name = "granola_live_test"
    # Recreate fresh source generator for the pipeline
    source = granola_source(api_key=api_key, include_transcript=True)

    print(f"Syncing notes into cognee dataset '{dataset_name}'...")
    await cognee.remember(source, dataset_name=dataset_name)

    query = "Summarize the key decisions and action items from my recent meetings."
    print(f"\nRunning search query: '{query}'...")
    answer = await cognee.search(
        query_text=query,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[dataset_name],
    )

    print("\n" + "=" * 60)
    print("Cognee Search Result:")
    print("=" * 60)
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
