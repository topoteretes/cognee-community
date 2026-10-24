"""Ingest named Google Chat spaces into Cognee, one document per thread.

Create a Desktop OAuth client in Google Cloud, enable the Google Chat API, and
save the downloaded client JSON as ``credentials.json`` before running this
example. The first run opens a browser for read-only user consent and stores a
refresh token in ``token.json``.
"""

import asyncio

import cognee

from cognee_community_connector_google_chat import google_chat_source


async def main() -> None:
    source = google_chat_source(
        ["spaces/AAAAExample"],
        credentials_path="credentials.json",
        token_path="token.json",
    )
    await cognee.remember(
        source,
        dataset_name="team-google-chat",
        max_rows_per_table=0,
    )


if __name__ == "__main__":
    asyncio.run(main())
