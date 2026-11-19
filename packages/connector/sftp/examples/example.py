"""SFTP connector demo: turn files on an SFTP server into memory.

``sftp_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Every text file and PDF under the chosen folders is ingested as a normal document,
titled with its path, so it goes through cognify entity extraction.

Re-running syncs only what changed: new and edited files are processed again and
files deleted, renamed or moved off the server are forgotten.

Privacy / opt-in
----------------
This reads every supported file the account can read under the chosen folders.
Nothing is fetched until you run this script. Keep the server in its own dataset.

One-time setup
--------------
Export the server, the account and a password or private key, the folders to sync,
the server's host key fingerprint (unless it is in ~/.ssh/known_hosts), and your
LLM key, then run:

    export SFTP_HOST="files.example.com"
    export SFTP_USERNAME="cognee"
    export SFTP_PRIVATE_KEY_PATH="$HOME/.ssh/cognee_sftp"   # or SFTP_PASSWORD
    export SFTP_HOST_KEY_FINGERPRINT="SHA256:..."           # optional
    export SFTP_ROOTS="/reports,/runbooks"
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_sftp import sftp_source

DATASET_NAME = "sftp"


async def main() -> None:
    missing = [name for name in ("SFTP_HOST", "SFTP_USERNAME", "SFTP_ROOTS") if not os.getenv(name)]
    if missing or not (os.getenv("SFTP_PASSWORD") or os.getenv("SFTP_PRIVATE_KEY_PATH")):
        print(
            "Set SFTP_HOST, SFTP_USERNAME, SFTP_ROOTS and SFTP_PASSWORD or "
            "SFTP_PRIVATE_KEY_PATH to run this example."
        )
        return

    source = sftp_source(
        host=os.environ["SFTP_HOST"],
        port=int(os.getenv("SFTP_PORT", "22")),
        username=os.environ["SFTP_USERNAME"],
        password=os.getenv("SFTP_PASSWORD"),
        private_key_path=os.getenv("SFTP_PRIVATE_KEY_PATH"),
        private_key_passphrase=os.getenv("SFTP_PRIVATE_KEY_PASSPHRASE"),
        host_key_fingerprint=os.getenv("SFTP_HOST_KEY_FINGERPRINT"),
        roots=[root.strip() for root in os.environ["SFTP_ROOTS"].split(",") if root.strip()],
    )

    print("Syncing the SFTP server into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )
    print("Sync stats:", source.cognee_sync_stats)

    results = await cognee.recall(
        "What do the latest reports say about outage credits?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)

    print(
        "\nEdit, delete or rename a file on the server, then re-run: only the changes are "
        "synced and removed files are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
