"""Prometheus connector demo: ask memory what an alert means.

Pull a Prometheus server's alerting and recording rules into cognee.
``prometheus_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Every rule is ingested as a normal document, so it goes
through the full cognify entity-extraction pipeline.

Each run is a full snapshot of the rule list. Unchanged rules keep a stable id
and are not re-cognified. A rule you edit is re-processed, and a rule you
remove from the server is forgotten on the next sync.

What is read
------------
Rule definitions only: name, group, file, expression, ``for``, labels and
annotations. No samples are read and no PromQL is run. Firing state and active
alerts are left out on purpose, because they change on every read.

One-time setup
--------------
1. Install the connector:

       uv pip install cognee-community-connector-prometheus
       # or, from this monorepo: cd packages/connector/prometheus && uv sync

2. Point it at your server and set your LLM key, then run:

       export PROMETHEUS_URL="http://localhost:9090"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   Behind a proxy, also set PROMETHEUS_USERNAME and PROMETHEUS_PASSWORD, or
   PROMETHEUS_TOKEN.

Edit or remove a rule, reload Prometheus, then re-run to see the re-sync.
"""

import asyncio
import os

import cognee

from cognee_community_connector_prometheus import prometheus_source

# Keep Prometheus in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "prometheus"


async def main() -> None:
    base_url = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")

    # rule_type="alert" or rule_groups=[...] narrow what is read;
    # include_metadata=True adds one document per metric with its help text.
    source = prometheus_source(
        base_url=base_url,
        username=os.environ.get("PROMETHEUS_USERNAME"),
        password=os.environ.get("PROMETHEUS_PASSWORD"),
        token=os.environ.get("PROMETHEUS_TOKEN"),
    )

    print(f"Syncing rules from {base_url} into cognee ...")
    # self_improvement=False: a re-sync should not trigger the whole-graph
    # enrichment pass every time.
    await cognee.remember(source, dataset_name=DATASET_NAME, self_improvement=False)

    answer = await cognee.search(
        query_text="Which alerts page someone, what does each one check, and is there a runbook?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or remove a rule, reload Prometheus, then re-run: changed rules re-sync "
        "and removed rules are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
