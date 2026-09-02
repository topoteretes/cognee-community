# cognee-community-connector-pubmed

Incrementally sync PubMed article titles, abstracts, and source URLs into
[cognee](https://github.com/topoteretes/cognee).

## Install

```bash
uv pip install cognee-community-connector-pubmed
```

## Usage

NCBI asks clients to identify a contact email. Set it once, then create a source
for a stable query:

```bash
export PUBMED_EMAIL="you@example.com"

from cognee_community_connector_pubmed import pubmed_source

source = pubmed_source('"traditional Chinese medicine"')

await cognee.remember(
    source,
    dataset_name="pubmed-tcm",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

Pass an optional `NCBI_API_KEY` to use NCBI's higher request allowance. The
connector applies the documented polite request spacing in either case.

## Incremental sync and deletion

The first run fetches every PMID matching the query. Later runs reuse dlt's
persisted `last_edat` cursor and fetch only records in PubMed's inclusive edit-date
window, plus newly discovered PMIDs. Because PubMed edit dates have day precision,
the boundary day is intentionally rechecked so late edits are not missed.

Every run also performs a lightweight PMID sweep. A previously known PMID that no
longer matches is emitted as a dlt hard-delete marker; cognee's orphan cleanup then
removes it from graph, vector, and relational stores. Keep the same query and use a
dedicated dataset for each source. `write_disposition="merge"` and
`max_rows_per_table=0` are required for correct incremental deletion handling.

## Test

```bash
uv run pytest tests/
```

The tests use fixture XML and never call NCBI.
