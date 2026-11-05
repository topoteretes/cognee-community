"""Twitch connector demo: "ask the channels I follow".

Syncs the channels you follow on Twitch (their descriptions and categories)
plus each channel's recent VODs and clips into cognee memory, then asks a
question about them. Run it again later: the full listing is re-read, new
VODs and clips are added, unfollowed channels and expired VODs are forgotten.

One-time setup
--------------
1. Register an application at https://dev.twitch.tv/console/apps/create
   (any category works) and copy its Client-ID.
2. Get a user access token: visit, with your Client-ID in it::

       https://id.twitch.tv/oauth2/authorize?client_id=<YOUR CLIENT ID>&redirect_uri=http://localhost&response_type=token&scope=user:read:follows

   approve the prompt, and copy the ``access_token`` value from the redirect
   URL (``http://localhost/#access_token=...``).
3. Set ``TWITCH_CLIENT_ID``, ``TWITCH_ACCESS_TOKEN`` and your ``LLM_API_KEY``
   (as for any cognee run).

Optional: ``TWITCH_CHANNELS=login1,login2`` syncs only those follows, and
``TWITCH_MAX_CHANNELS`` (default 25) bounds a run.

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError

from cognee_community_connector_twitch import twitch_source

DATASET_NAME = "twitch_follows"

# write_disposition="merge" is REQUIRED: every run emits the current listing
#   plus deletions, and the default "replace" would wipe earlier syncs.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
TWITCH_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


def _channels_from_env():
    raw = os.environ.get("TWITCH_CHANNELS", "").strip()
    if not raw:
        return None
    return [name.strip() for name in raw.split(",") if name.strip()]


async def main():
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    access_token = os.environ.get("TWITCH_ACCESS_TOKEN")
    if not client_id or not access_token:
        print("Set TWITCH_CLIENT_ID and TWITCH_ACCESS_TOKEN first (see the setup steps above).")
        return

    max_channels = int(os.environ.get("TWITCH_MAX_CHANNELS", "25"))
    channels = _channels_from_env()
    print(f"Syncing {'channels: ' + ', '.join(channels) if channels else 'your follows'} ...")
    result = await cognee.remember(
        twitch_source(
            client_id=client_id,
            access_token=access_token,
            channels=channels,
            max_channels=max_channels,
        ),
        dataset_name=DATASET_NAME,
        **TWITCH_REMEMBER_KWARGS,
    )
    print(result)

    try:
        answers = await cognee.recall(
            "Which of the channels I follow recently streamed about Rust, "
            "and what were those VODs about?",
            datasets=[DATASET_NAME],
        )
    except NoDataError:  # the dataset is still empty
        print(
            "Nothing to search yet: the account follows no channels (or the selected "
            "logins match nothing). Follow a few channels, then run this again."
        )
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
