# cognee-community-connector-pendo

Sync Pendo guide copy, Listen feedback, qualitative guide-poll feedback, and NPS free text into
[cognee](https://github.com/topoteretes/cognee). The connector deliberately does **not**
ingest usage counters or numeric-only answers: customer and guide-author language is the
knowledge surface.

## Install

```bash
uv pip install cognee-community-connector-pendo
# From this repository:
cd packages/connector/pendo && uv sync
```

## Pendo setup

1. In Pendo, open **Settings → Integrations → Integration Keys**.
2. Create and securely copy a read-only integration key.
3. Export the key and your normal cognee LLM key:

```bash
export PENDO_INTEGRATION_KEY="..."
export LLM_API_KEY="..."
```

The integration key is sent only in the `x-pendo-integration-key` request header. For an
EU, US1, Japan, or Australia subscription, also set the matching UI origin, for example:

```bash
export PENDO_BASE_URL="https://app.eu.pendo.io"
```

See Pendo's [integration-key documentation](https://support.pendo.io/hc/en-us/articles/9491198203547-Pendo-integration-keys)
and [Engage API reference](https://engageapi.pendo.io/) for account/API entitlement details.

## Usage

```python
import cognee
from cognee_community_connector_pendo import pendo_source

source = pendo_source(
    content_types=["guides", "feedback", "nps"],
    # Optional scopes:
    app_id="123456",
    guide_ids=["guide-id"],
    # Add modern Sentiment NPS responses (IDs are visible in Pendo survey URLs):
    nps_survey_ids=["survey-id"],
)

await cognee.remember(
    source,
    dataset_name="pendo",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

Run [`examples/example.py`](examples/example.py) for a complete sync-and-search flow:

```bash
uv run python examples/example.py
```

`content_types` is the explicit ingest selection:

- `guides` — guide name, description, and authored content.
- `feedback` — Pendo Listen feedback (`vocItems`) and free-form guide poll responses.
- `nps` — free-form responses from NPS guides, plus Sentiment surveys named in
  `nps_survey_ids`.

Numeric answers, scores, event counts, and other usage metrics are never made into
documents. `start_time_ms` (or `PENDO_START_TIME_MS`) can limit the poll/survey inventory.
For modern Sentiment, a value of `0` pins the initial boundary to 180 days before the first
sync; keeping that boundary stable prevents an old, live response from aging out and being
mistaken for a deletion.

## Incremental sync and forget-on-delete

The resource uses `write_disposition="merge"` with stable, type-prefixed IDs. dlt state
stores a per-content-type cursor and the last complete ID inventory:

- A new object or one whose `lastUpdatedAt`/response timestamp is newer than the cursor is
  emitted. Unchanged content is not re-ingested.
- An ID missing from the next complete inventory becomes a dlt hard-delete tombstone.
  cognee's orphan cleanup then removes its data, graph, and vector records.
- Removing a type from `content_types` also forgets the previously synced records of that
  type.
- An API failure aborts before the inventory/cursor is committed, preventing a partial
  response from being mistaken for mass deletion.

Keep `write_disposition="merge"`, `primary_key="id"`, and `max_rows_per_table=0` on the
`cognee.remember` call so incremental rows and deletion reconciliation retain their
intended semantics.

## Tests

```bash
uv run pytest tests/
```

Tests use an in-memory fake Pendo client and temporary SQLite destination; no Pendo key or
network access is required. They cover knowledge-only ingestion, selection, the
incremental cursor, and physical removal after an upstream deletion.
