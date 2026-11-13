"""Live OAuth synchronization example. Never prunes existing Cognee data."""

import argparse
import asyncio
from pathlib import Path

from cognee_community_connector_read_ai import read_ai_source, sync_read_ai


async def run(args):
    source = read_ai_source(
        connection_id=args.connection_id,
        credentials_path=args.credentials,
        meeting_ids=args.meeting_id,
        start_time=args.start,
        end_time=args.end,
        include_summary=not args.no_summary,
        include_transcript=not args.no_transcript,
        include_metrics=not args.no_metrics,
        state_dir=args.state_dir,
    )
    result = await sync_read_ai(source, dataset_name=args.dataset)
    print(result)
    if args.query:
        import cognee

        answers = await cognee.search(
            query_text=args.query,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[args.dataset],
        )
        print(answers)


def main():
    parser = argparse.ArgumentParser(
        description="Synchronize Read.ai through OAuth. Requires configured Cognee LLM/embeddings."
    )
    parser.add_argument(
        "--connection-id", required=True, help="Stable ID for this account/selection"
    )
    parser.add_argument("--dataset", default="read-meetings")
    parser.add_argument(
        "--credentials", type=Path, help="Protected read-ai-connect credential file"
    )
    parser.add_argument(
        "--state-dir", type=Path, help="Private synchronization journal outside checkout"
    )
    parser.add_argument("--meeting-id", action="append", help="Repeat for explicit meeting IDs")
    parser.add_argument("--start", help="Inclusive timezone-aware ISO 8601 bound")
    parser.add_argument("--end", help="Exclusive timezone-aware ISO 8601 bound")
    parser.add_argument("--no-summary", action="store_true")
    parser.add_argument("--no-transcript", action="store_true")
    parser.add_argument("--no-metrics", action="store_true")
    parser.add_argument("--query", help="Optional graph-completion question after successful sync")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
