# Elasticsearch connector for Cognee

Sync selected Elasticsearch documents into searchable Cognee memory, incrementally,
with deletion propagation. Implements [cognee#4770](https://github.com/topoteretes/cognee/issues/4770).

## Install

Requires Python 3.11–3.13, Cognee 1.6.3 and Elasticsearch 8.17+. This package uses
an 8.x official Python client. Match the client major to your server; Elasticsearch
9 is not part of the tested compatibility contract. The dlt upper bound avoids
1.31's removed datetime helper used by Cognee 1.6.3.

From this directory:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Elasticsearch setup

1. Enable `_source` on your index. Map `updated_at` (or your chosen update field)
   as `date` or `date_nanos` with doc values. Every selected document needs a
   single date value. Your application should update it whenever content changes.
2. Create an API key with `read` and `view_index_metadata` index privileges on
   **all** selected indices.
   No cluster privilege is needed for the connector. The Python client expects
   the API's base64 `encoded` key, without an `ApiKey ` prefix.
3. Keep this key's document/field permissions stable between runs. Permission
   changes that successfully hide documents look like deletions to Elasticsearch.
   Give a different permission scope its own `source_id` and destination dataset.
4. Configure Cognee's LLM and embedding providers using its normal setup. For
   hosted providers set `LLM_API_KEY`; local providers are supported by Cognee.

Example API key creation (run as an Elasticsearch administrator):

```http
POST /_security/api_key
{
  "name": "cognee-knowledge-read",
  "role_descriptors": {
    "reader": {
      "cluster": [],
      "indices": [{"names": ["knowledge-*"], "privileges": ["read", "view_index_metadata"]}]
    }
  }
}
```

For HTTPS with a private CA, pass `ca_certs="/path/to/http_ca.crt"`. TLS verification
stays enabled. For a disposable local test, HTTP on localhost is sufficient.

```sh
export ELASTICSEARCH_URL='https://your-elasticsearch:9200'
export ELASTICSEARCH_API_KEY='your-encoded-key'
export ELASTICSEARCH_INDEX='knowledge-*'
export ELASTICSEARCH_SOURCE_ID='my-cluster-knowledge-reader'
python examples/remember_elasticsearch.py
```

## Use

```python
import cognee
from cognee_community_connector_elasticsearch import elasticsearch_source

await cognee.remember(
    elasticsearch_source(
        source_id="my-cluster-knowledge-reader",
        index="knowledge-*",
        query={"term": {"published": True}},
        updated_field="updated_at",
        fields=["title", "body", "updated_at"],
        title_field="title",
    ),
    dataset_name="elasticsearch_knowledge",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
answers = await cognee.recall(
    "What does our knowledge base say about onboarding?", datasets=["elasticsearch_knowledge"]
)
```

**Always pass `write_disposition="merge"`.** Cognee's default is `replace`, which
would drop unchanged staged rows on an incremental run. The connector refuses
a non-merge run before contacting Elasticsearch; the example supplies merge.
The document-mode adapter reads the complete staging table, so deletion comparison
is never limited to 50 rows. Recreate the source for each sync using the same
configuration and dataset; dlt persists its state locally. Keep the staging DB
and dlt working directory together, and serialize syncs for a given scope.

`query` is an Elasticsearch **query clause**, not an entire search request.
`fields=None` includes the entire JSON document; a list filters `_source`.
Selected JSON is rendered deterministically as document text, then goes through
Cognee's ordinary document extraction and search pipeline. This avoids treating
prose as relational schema data. Include `title_field` in `fields` to get a title.

## Sync and deletion behavior

- A point-in-time (PIT) view fixes index contents for the entire sync. Both scans
  paginate with `search_after` on the update date plus `_shard_doc`. The connector
  carries the **entire** sort tuple, uses refreshed PIT IDs, and closes the PIT.
  It never uses `from` or carries a PIT cursor into the next run.
- Each run scans the query's complete ID/revision inventory without document
  bodies. This O(N) metadata pass is necessary: Elasticsearch has no delete feed.
- A persisted date watermark selects recent changes inclusively. Per-document
  `_seq_no`/`_primary_term` plus date revisions identify the content delta.
  Index UUIDs force a fresh content scan if an index is recreated with recycled
  sequence numbers. A changed index selection during a sync aborts safely. Targeted
  recovery also reads new/query-entering documents or edits with old/backdated
  dates. Equal dates and multiple shards do not drop documents.
- Only changed bodies are retrieved. Stable content fingerprints avoid emitting
  a document whose selected content has not changed, even if its revision did.
  Cognee reuses unchanged staged documents and does not re-cognify them.
- IDs include source configuration, concrete index, and `_id`. Source/table and
  dlt pipeline namespaces isolate different queries, fields, clusters and datasets.
  API keys are never part of identity, stored in state, or rendered into content.
- Documents absent from a **complete** inventory emit dlt hard-delete tombstones.
  On merge they leave staging; Cognee's deferred orphan cleanup removes their
  relational records, graph nodes and vector artifacts. A confirmed empty index
  removes the final document too. Documents leaving the query are also forgotten.
- Authentication failures, unavailable indices, timeouts, failed shards, malformed
  responses, disabled `_source`, or incomplete deltas abort before publishing rows
  or advancing connector state. No partial inventory drives deletion.

Limitations: local indices/aliases only (no cross-cluster search); one update date
mapping across indices; memory and persisted state scale with the selected corpus.
An unavailable index selection raises instead of authorizing a mass deletion.
Empty its contents and sync first if that is intended. If a wildcard or alias
still resolves to other indices, documents from a removed index are forgotten.
Index UUIDs distinguish an index
recreated under the same name; sequence numbers alone are not permanent identities
across index incarnations. Changing query or
field selection creates a new scope; it does not remove the previous scope's memory.
A lost staging DB with retained dlt state requires resetting both and a full sync.
Cognee cleanup failures are logged by the core and retried on later syncs.

## Tests

```sh
pytest tests -m 'not live and not cognee' -q
ruff check .
ruff format --check .
```

The offline tests use an independent fake Elasticsearch API and a real dlt SQLite
merge destination. They cover >10,000 hits, tied dates, persisted cursors, updates,
query departures, final-document deletion, field selection, scoped identities,
zero-change runs, failed extraction/load recovery, and generated lifecycle
sequences checked against an independent upstream-state oracle. Fault injection
covers every request in a multi-page scan, malformed responses, repeated pages,
PIT closure failure, SDK retry exhaustion and index changes during a scan.

For connector branch coverage and the generated lifecycle statistics:

```sh
pytest tests --cov=cognee_community_connector_elasticsearch --cov-branch \
  --hypothesis-show-statistics
```

Live tests need a **disposable**, security-enabled Elasticsearch server. They create
and delete uniquely named test indices and API keys, never existing indices:

```sh
export ES_TEST_URL='http://127.0.0.1:19200'
export ES_TEST_PASSWORD='your-disposable-admin-password'
pytest tests -m live -q
```

The `cognee` test exercises actual Cognee document ingestion, graph/vector storage
and orphan cleanup with deterministic local test providers (no paid model key),
including recovery after model or graph-cleanup failure. Local model mocks do
not measure semantic answer quality or a production provider's behavior.

```sh
pytest tests -m cognee -q
```

With the live server configured, the example lifecycle test also runs the shipped
example against real Elasticsearch and Cognee storage, checking unchanged sync,
an edit and deletion of the final document. It pins the actual recall API to chunk
retrieval for deterministic validation:

```sh
pytest tests/test_cognee_sync.py::test_runnable_example_with_live_source_updates_and_deletion -q
```
