# cognee-community-connector-pubmed

A PubMed data-source connector for [cognee](https://github.com/topoteretes/cognee). It turns
article metadata and abstracts into normal cognee documents, supports incremental sync by
PubMed Entrez date (`edat`), and removes records that NCBI deletes upstream.

## Install

```bash
uv pip install cognee-community-connector-pubmed
# or, from this monorepo:
cd packages/connector/pubmed && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_pubmed import pubmed_source

source = pubmed_source(
    '("retrieval augmented generation"[Title/Abstract]) AND humans[MeSH Terms]',
    start_date="2026-01-01",
    api_key="...",  # optional; or set NCBI_API_KEY
    email="you@example.com",  # recommended; or set NCBI_EMAIL
)

await cognee.remember(
    source,
    dataset_name="pubmed_rag",
    primary_key="id",
    write_disposition="merge",  # required for incremental upserts/deletes
    max_rows_per_table=0,  # let orphan cleanup see the full corpus
)
```

The connector stores title, structured abstract, authors, journal, publication and Entrez
dates, DOI/PMC identifiers, publication types, MeSH terms, keywords, and the canonical PubMed
URL. See [`examples/example.py`](examples/example.py) for an ingest-and-search program.

## How synchronization works

1. **Discover:** ESearch selects PMIDs for the configured query and inclusive `edat` window.
2. **Fetch:** EFetch retrieves article XML in batches of at most 200 and preserves structured
   abstract labels such as `BACKGROUND` and `METHODS`.
3. **Advance:** dlt resource state stores the last completed `edat`, query, and known PMIDs.
4. **Resume safely:** because `edat` has day resolution, the previous cursor day is searched
   again and known PMIDs are filtered before EFetch. Late same-day records are not lost.
5. **Forget deletes:** the connector intersects known PMIDs with NCBI's authoritative
   `deleted.pmids.gz` feed and emits dlt hard-delete rows. Cognee's existing orphan cleanup then
   removes the corresponding graph, vector, and relational data.

State advances only after search, fetch, and deletion reconciliation all succeed. A failed API
or deletion-feed request therefore retries the same window instead of creating a silent gap.
Changing the query for an existing resource state is rejected; use a new dataset when changing
the corpus definition.

## NCBI limits and configuration

Without an API key, the client stays at three E-utilities requests per second. With a key it
uses ten requests per second. `batch_size` defaults to 100 and is capped at 200, matching NCBI's
recommendation to avoid sending larger UID lists via GET.

ESearch exposes at most 10,000 PubMed records for one query. If a sync window exceeds that
limit, the connector fails before advancing its cursor; narrow the search or date range rather
than silently loading a partial corpus.

## Tests

```bash
uv run --with pytest pytest -q
```

The suite is fully offline. It covers XML parsing, pagination, auth/identity parameters,
inclusive cursor replay without duplicate fetches, failure-safe state, deletion markers,
resource schema, and an end-to-end SQLite dlt merge proving that an upstream deletion removes
the stored row.
