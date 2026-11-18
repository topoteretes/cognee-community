"""Sync a TLS IMAP mailbox into cognee and ask a question about it."""

import asyncio
import os

import cognee

from cognee_community_connector_imap import imap_source


async def main() -> None:
    required = ("IMAP_HOST", "IMAP_USERNAME", "IMAP_APP_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Set these environment variables first: {', '.join(missing)}")

    folders = [item.strip() for item in os.getenv("IMAP_FOLDERS", "INBOX").split(",")]
    source = imap_source(
        host=os.environ["IMAP_HOST"],
        port=int(os.getenv("IMAP_PORT", "993")),
        username=os.environ["IMAP_USERNAME"],
        password=os.environ["IMAP_APP_PASSWORD"],
        folders=folders,
        since=os.getenv("IMAP_SINCE") or None,
        tls_mode=os.getenv("IMAP_TLS_MODE", "ssl"),
    )

    await cognee.remember(
        source,
        dataset_name="imap_mail",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    answer = await cognee.search(
        query_text="What are the most important recent messages in my inbox?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["imap_mail"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
