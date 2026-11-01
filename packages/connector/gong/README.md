# cognee-community-connector-gong

Sync selected Gong calls, transcripts, and related deal context into a searchable
[cognee](https://github.com/topoteretes/cognee) dataset.

## Setup

1. Install this package and its dependencies: `uv sync --group dev` from this directory,
   or `pip install -e packages/connector/gong` from the repository root.
2. In Gong, obtain the [customer-specific API base URL](https://help.gong.io/apidocs/introduction-2)
   and either an access key and secret or an [OAuth access token](https://help.gong.io/docs/create-an-app-for-gong).
   The token needs `api:calls:read:basic`,
   `api:calls:read:transcript`, and `api:calls:read:extensive` scopes when both
   transcripts and deal context are enabled. OAuth token acquisition and refresh are
   managed by your application; supply a current token for each run.
3. Set `GONG_BASE_URL` and either `GONG_ACCESS_TOKEN` or both `GONG_ACCESS_KEY` and
   `GONG_ACCESS_KEY_SECRET`. Set Cognee's `LLM_API_KEY` for cognify/search.

```python
import cognee
from cognee_community_connector_gong import gong_source

await cognee.remember(
    gong_source(from_datetime="2026-01-01T00:00:00Z", workspace_id="123"),
    dataset_name="gong",
    write_disposition="merge",  # required for incremental runs and deletions
)

results = await cognee.search(
    query_text="What did the customer say about the renewal?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["gong"],
)
```

Select individual calls with `call_ids=["..."]`, or choose a workspace with
`workspace_id`. Set `include_transcripts=False` or `include_deal_context=False` to
exclude either kind of content. See [the runnable example](examples/example.py).
Use a dedicated dataset for each Gong tenant/scope.

## Sync behavior

The first run reads selected calls from `from_datetime` through the run's start
time. Later runs use a persisted cursor and a seven-day lookback window to refresh
recent calls, including transcripts that arrive after the call. They also compare
call metadata across the selected inventory and fetch older calls only when that
metadata changes. Gong's date filter is based on **call start time**, not edit time;
transcript-only edits outside the lookback cannot be detected by the Public API.

Gong does not expose a deleted-call feed. Every run therefore lists the full selected
call inventory using the lightweight `/v2/calls` endpoint. IDs that disappear are
hard-deleted from dlt staging; Cognee's document-source orphan cleanup then removes
their graph and vector records. Keep `write_disposition="merge"`. A failed or
incomplete inventory aborts before issuing deletions or advancing the cursor.
The full inventory costs API calls proportional to your retained history; choose a
practical `from_datetime` and workspace to stay within Gong's API limits.

## Test

```bash
uv run --group dev pytest tests -q
uv run --group dev ruff check .
```

The tests use a fake Gong client and HTTP mock transport. No Gong tenant, LLM key, or
running service is needed. Live tenant verification remains necessary before relying
on this connector in production.
