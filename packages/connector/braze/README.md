# cognee-community-connector-braze

A Braze data-source connector for [cognee](https://github.com/topoteretes/cognee).
It ingests campaigns, Canvases, and their message content as searchable documents.

## What it supports

- REST API key authentication (`Authorization: Bearer ...`).
- Configurable Braze REST instance/cluster.
- Campaign and Canvas selection, with optional ID allowlists.
- Incremental detail fetches using Braze's `last_edit.time[gt]` filter.
- Forget-on-delete through a lightweight ID sweep and dlt hard-delete markers.
- Campaign messages and Canvas step messages rendered to searchable text.

## Install

From the community repository:

```bash
cd packages/connector/braze
uv sync
```

## Braze setup

1. In Braze, open **Settings > APIs and Identifiers > API Keys**.
2. Create a REST API key with the permissions needed for your selection:
   - `campaigns.list` and `campaigns.details`
   - `canvas.list` and `canvas.details`
3. Copy the REST endpoint shown beside the key. Braze endpoints are instance-specific;
   for example, `https://rest.iad-01.braze.com` or
   `https://rest.fra-01.braze.eu`.
4. Export the values along with your normal cognee LLM configuration:

```bash
export BRAZE_API_KEY="..."
export BRAZE_REST_ENDPOINT="https://rest.iad-01.braze.com"
export LLM_API_KEY="..."
```

`BRAZE_CLUSTER` can be used instead of the full endpoint, such as `US-01`,
`EU-01`, or `iad-01`. The full endpoint copied from Braze is recommended because
it is unambiguous and continues to work for newly introduced instances.

## Usage

```python
import cognee
from cognee_community_connector_braze import braze_source

source = braze_source(
    content_types=("campaigns", "canvases"),
    # Optional fine-grained selection:
    # campaign_ids=("campaign-api-id",),
    # canvas_ids=("canvas-api-id",),
)

await cognee.remember(
    source,
    dataset_name="braze",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)

answer = await cognee.search(
    query_text="What messaging do we use in our onboarding journey?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["braze"],
)
```

See [`examples/example.py`](examples/example.py) for a runnable script.

## Incremental sync and deletion reconciliation

On the first run, the connector lists the selected object types and fetches each
object's details. It stores the highest `last_edited` value and the current IDs in
dlt resource state.

On later runs, it sends that cursor to Braze as `last_edit.time[gt]`; only matching
campaigns or Canvases have their details downloaded and re-ingested. A lightweight
unfiltered list pass still gathers IDs. IDs that were previously known but are no
longer returned become dlt hard-delete rows, which lets cognee's existing orphan
cleanup remove their graph and vector records. Archived objects are treated as
removed by default; pass `include_archived=True` to retain and ingest them.

Use the same dataset name and selection on each run so the persisted cursor and ID
set describe the same source scope. Pass `max_rows_per_table=0` so reconciliation
operates on the complete synced corpus.

## Testing

The suite uses a fake Braze session and requires no API key:

```bash
uv run pytest tests/
```

It covers pagination, selection, campaign and Canvas content rendering, the
`last_edited` cursor, changed-only detail fetches, and delete markers.
