"""Microsoft Teams connector demo — turn your team channels into memory.

Pull Teams channel threads into cognee, incrementally and with
forget-on-delete. ``teams_source`` returns a ``dlt`` source you hand straight
to ``cognee.remember``. Each thread (root message + replies) is ingested as a
normal document, so it goes through the full cognify entity-extraction
pipeline.

The first run reads every thread in the selected channels. Later runs walk
each channel only back to where the previous run stopped, so an unchanged
channel costs one Graph request. Threads you delete in Teams, and channels
that are deleted or dropped from ``channel_ids``, are forgotten on the next
sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the messages in your Teams channels. It is strictly opt-in —
nothing is fetched until you run this script. Only standard channels of the
teams you select are read (never 1:1 or group chats, private or shared
channels). Use a dedicated dataset so you can wipe it with a single
``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
One-time setup (see README.md for the click-by-click version)
────────────────────────────────────────────────────────────────────────────
1. Register an app in the Microsoft Entra admin center and create a client
   secret.
2. Add the Microsoft Graph *application* permissions
   ``ChannelMessage.Read.All``, ``Channel.ReadBasic.All`` (and
   ``Team.ReadBasic.All`` to list teams), then have a tenant admin click
   "Grant admin consent".
3. Export the app credentials, the team to sync, and your LLM key, then run:

       export MS_GRAPH_TENANT_ID="<directory (tenant) id>"
       export MS_GRAPH_CLIENT_ID="<application (client) id>"
       export MS_GRAPH_CLIENT_SECRET="<client secret value>"
       export TEAMS_TEAM_ID="<team id>"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after posting, editing or deleting a message to see the incremental
re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_teams import teams_source

# Keep Teams in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "teams"


async def main() -> None:
    required = ("MS_GRAPH_TENANT_ID", "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(f"Set {', '.join(missing)} (see README.md) to run this example.")
        return

    # Scope with team_ids=[...] and, optionally, channel_ids=[...]. Omitting
    # team_ids syncs every team the app can list (needs Team.ReadBasic.All).
    team_id = os.environ.get("TEAMS_TEAM_ID")
    source = teams_source(team_ids=[team_id] if team_id else None)

    print("Syncing Teams channel threads into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        # Required: each run after the first only yields what changed.
        write_disposition="merge",
    )

    answer = await cognee.search(
        query_text="What has the team been discussing recently?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nPost, edit or delete a message in Teams, then re-run: changed threads "
        "re-sync and deleted ones are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
