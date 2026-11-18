# cognee-community-connector-grain

Sync [Grain](https://grain.com) recordings, transcripts, and human-curated
highlights into [cognee](https://github.com/topoteretes/cognee).

The connector uses Grain's read-only Public API and returns a `dlt` document
resource for `cognee.remember(...)`. Each selected item becomes a searchable
cognee document. Highlights are indexed as standalone, explicitly high-priority
documents instead of being buried in a long transcript, giving curated moments
stronger retrieval granularity than raw transcript text.

## Install

```bash
uv pip install cognee-community-connector-grain
# or from this repository
cd packages/connector/grain && uv sync
```

## Setup

1. In Grain, open **Settings → Integrations → API** and create a Personal Access
   Token or Workspace Access Token. A workspace token can see the whole
   workspace; a personal token has the same access as its user.
2. Export it along with the LLM key used by cognee:

```bash
export GRAIN_API_TOKEN="your_grain_token"
export LLM_API_KEY="your_llm_key"
```

Grain requires both `Authorization: Bearer ...` and a version header. This
connector sends the current `Public-Api-Version: 2026-10-01` header.

## Usage

```python
import cognee
from cognee_community_connector_grain import grain_source

source = grain_source(
    content_types=["recordings", "transcripts", "highlights"],
    # Optional scope controls:
    # recording_ids=["recording-uuid"],
    # recording_filter={"team": "team-uuid"},
)

await cognee.remember(
    source,
    dataset_name="grain",
    primary_key="id",
    write_disposition="merge",
)

result = await cognee.search(
    query_text="What customer objections appeared in curated highlights?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["grain"],
)
print(result)
```

`content_types` accepts any non-empty selection of `recordings`, `transcripts`,
and `highlights`. “Recordings” means searchable meeting metadata and a link back
to Grain; the connector does not download or embed the video binary.

See [`examples/example.py`](examples/example.py) for a complete runnable script.

## Incremental sync and deletions

The first run stores a creation cursor plus stable document fingerprints in
dlt resource state. Later runs still paginate the lightweight recording list so
upstream deletion can be detected, but they only fetch a transcript and emit a
row when that selected document is new or its visible recording metadata has
changed. New/changed highlights are emitted independently.

Grain's current recording response exposes `start_datetime`, while some API
responses expose `created_at` or `created_datetime`. The cursor prefers the
explicit creation fields and safely falls back to `start_datetime`.

When a recording or highlight disappears upstream, the connector emits a dlt
hard-delete tombstone. With `write_disposition="merge"`, dlt removes the staging
row and cognee's existing orphan cleanup removes its graph and vector data.

> Keep one dataset/resource state per selection and scope. If you intentionally
> change `content_types`, previously selected kinds are treated as out of scope
> and removed on the next successful sync.

## Test

```bash
uv run --with pytest pytest tests/
```

The suite uses a fake Grain API; no live token or network access is required.
