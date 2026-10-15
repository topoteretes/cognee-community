# Fireflies connector

Sync Fireflies.ai meeting transcripts into cognee as speaker-aware documents.
The connector ingests selected transcript content, remembers a transcript-date
cursor between runs, and forgets meetings deleted upstream.

## Install

From this directory:

```bash
pip install -e .
```

Set your Fireflies API key:

```bash
export FIREFLIES_API_KEY="your-api-key"
```

PowerShell:

```powershell
$env:FIREFLIES_API_KEY = "your-api-key"
```

The connector sends the key as a Bearer token to
`https://api.fireflies.ai/graphql`.

## Use

```python
from cognee_community_connector_fireflies import fireflies_source, sync_fireflies

source = fireflies_source(
    include_transcript=True,
    include_summary=True,
    include_action_items=True,
    include_speakers=True,
)

await sync_fireflies(source, dataset_name="fireflies_meetings")
```

The helper performs ingestion, reconciliation and cognify in the foreground.
It supports local Cognee instances. Use one dataset per Fireflies account and
run syncs sequentially. Keep the same account and visibility scope between
runs: a transcript that is no longer visible is treated as removed.

Each option controls whether that section is queried and included. Setting
`include_speakers=False` omits the standalone speaker list; selecting transcripts
still retains the speaker labels within sentences. Transcript
sentences retain speaker names, Fireflies speaker IDs, and timestamps; the
explicit wording feeds the normal cognify pipeline's person/entity extraction.
There is no connector-specific graph schema. Actual extraction depends on your
configured LLM; inspect the speaker entities and their meeting relationships
when validating with your account.

## Sync semantics

The first run lists every visible transcript and ingests it. Later runs keep
`last_date`, `known_ids` and `content_hashes` in dlt resource state:

- selected content is compared with its previous hash and unchanged rows are skipped;
- unknown IDs are ingested even at the cursor boundary;
- IDs missing from a successful full listing emit dlt `_deleted` tombstones;
- a successful complete empty listing emits tombstones for all known meetings;
- a failed, malformed or repeated-page listing raises before deletion reconciliation;
- state changes only after the complete resource has been extracted.

Fireflies defines `date` as transcript creation time. Consequently this cursor
tracks newly-created transcripts. Because it is not a modification timestamp,
the connector also reads existing transcripts' selected content each run and
compares hashes. This detects edits and late summaries without re-emitting
unchanged documents. Each run costs one request per listing page (up to 50
meetings) plus one detail request per visible meeting, before retries. Check
your account's [API limits](https://docs.fireflies.ai/fundamentals/limits).

Existing states without hashes are upgraded by re-reading and emitting each
visible meeting once. Changing content options also refreshes affected rows.

HTTP 429/500/502/503/504, connection errors and timeouts have at most five
attempts, with Retry-After or exponential backoff. GraphQL errors and invalid
payloads fail the run, including partial-data responses; they never count as
deletions. A successful empty list is authoritative, so do not reuse a dataset
with a different API account or visibility scope.

The original `fireflies_source` interface is also compatible with `cognee.add`
or `cognee.remember`: pass `primary_key="id"`, `write_disposition="merge"` and
`max_rows_per_table=0`. **Cognee 1.4.0 skips its orphan cleanup when staging is
empty**, so use `sync_fireflies` for full forget-on-delete support, including
deleting the final meeting. This helper uses Cognee 1.4.0 ingestion and cleanup
internals; the dependency is pinned and integration tests cover this boundary.

`sync_fireflies` propagates empty-corpus deletion failures so retrying can
finish cleanup. Nonempty reconciliation uses Cognee's existing cleanup, which
logs individual cleanup failures for retry on the next sync.

## Example and tests

```bash
python examples/example.py
pytest tests/ -q
```

Requires Python 3.11–3.13 and Cognee 1.4.0. The runnable example also requires
your normal Cognee LLM/embedding configuration (for example `LLM_API_KEY`).
Install development tools with `pip install -e '.[test]'`.

Tests use fake clients and do not require a real Fireflies API key. Integration
tests use actual dlt/SQLite, Cognee graph and vector storage, with deterministic
LLM/embedding mocks. They check document routing, speaker nodes/relationships,
search, edits, unchanged re-sync, deletion of one/all meetings and API failure.
These tests do not establish live Fireflies API compatibility or live LLM
speaker-extraction accuracy. Run the example against your own account for that
final verification; no live credentials were used during implementation.

Schema references: [transcript](https://docs.fireflies.ai/schema/transcript),
[listing](https://docs.fireflies.ai/graphql-api/query/transcripts).
