"""Example: ingest CircleCI pipeline executions into cognee.

Run:
    CIRCLECI_API_TOKEN=your-token python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_circleci import circleci_source


async def main() -> None:
    # --- cognee boilerplate (adjust paths / infrastructure to taste) ---
    cognee.config.set_data_root_directory(".cognee-data")
    cognee.config.set_db_path(".cognee-data")
    await cognee.infrastructure.engine.connect()

    # --- connector usage ----------------------------------------------
    token = os.environ.get("CIRCLECI_API_TOKEN")
    if not token:
        raise SystemExit(
            "Please set the CIRCLECI_API_TOKEN environment variable "
            "(create one at https://app.circleci.com/settings/user/tokens)."
        )

    source = circleci_source(
        api_token=token,
        project_slugs=["gh/your-org/your-repo"],  # customize
        branch="main",  # optional
    )

    print("Ingesting CircleCI pipeline executions …")
    await cognee.add(source)
    print('Done. Try cognee.search("why did my latest build fail?")')


if __name__ == "__main__":
    asyncio.run(main())
