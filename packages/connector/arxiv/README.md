# cognee-community-connector-arxiv

An arXiv data-source connector for [cognee](https://github.com/topoteretes/cognee). It
queries public arXiv metadata and abstracts, and sends papers through cognee's document
ingestion path.

## Install

```bash
uv pip install cognee-community-connector-arxiv
# or, from this repository:
cd packages/connector/arxiv && uv sync
```

arXiv access is public and needs no credentials. Cognee still needs its normal model and
storage configuration to process and search documents.

## Use

```python
import cognee
from cognee_community_connector_arxiv import arxiv_source

source = arxiv_source(
    categories=["cs.AI", "cs.LG"],  # optional; values are ORed
    authors=["Ada Lovelace"],  # optional; author terms are ORed
    submitted_date_range=("202601010000", "202601312359"),  # UTC, optional
)

await cognee.remember(
    source,
    dataset_name="arxiv-research",
    write_disposition="replace",
    max_rows_per_table=0,  # let cleanup compare the complete selected corpus
)
```

Choose at least one category or author. If both are provided, results must match a category
and an author. A submitted date range further narrows that selection. The range is a fixed
part of the query, not a saved delta cursor. See [`examples/example.py`](examples/example.py)
for a runnable end-to-end example. Author terms use arXiv's `au` search semantics.

## Snapshot and reconciliation behavior

Every run enumerates the complete configured query and uses DLT `replace`. Cognee's normal
document ingestion and orphan cleanup then reconcile the dataset against that snapshot;
records absent from a successfully completed query can be forgotten. A request, parse, or
pagination error aborts extraction, so the previous successful DLT snapshot remains in
place and is not used to infer deletions.

Cognee's current document cleanup skips reconciliation when the read-back contains no
documents, because an empty result cannot distinguish a genuinely empty query from a failed
or misconfigured read. In that case DLT records the completed empty snapshot, but previously
ingested graph documents can remain. For a non-empty snapshot, missing documents are
reconciled after the new documents are committed.

arXiv's query API allows at most 30,000 results per query and at most 2,000 results per
request. The connector checks feed totals, requested offsets, and actual entry counts. arXiv
reports `itemsPerPage` as the requested `max_results`, including on a short final page; the
connector therefore checks actual entries against the stable total and requested page size
rather than treating `itemsPerPage` as the number of entries actually present. If a provider
reports a smaller effective page size, the connector follows that size only when actual
entry counts match it consistently; a page-size change aborts the snapshot. A query that
exceeds the result limit or cannot be completely enumerated fails with guidance to narrow
the category, author, or submitted date range. It never treats a truncated result as an
authoritative snapshot. The API paginates by numeric offsets and does not provide a snapshot
token; if the reported result total changes during enumeration, the connector aborts.
Requests are serialized and spaced at least three seconds apart, as required by the
[arXiv API terms](https://info.arxiv.org/help/api/tou.html).

Because the API does not provide a snapshot token, it cannot signal a same-size membership
change that happens during paging. The connector checks each page's offset, reported total,
entry count, and duplicate IDs; arXiv says its search results are refreshed on its daily
submission cycle. See the [API manual](https://info.arxiv.org/help/api/user-manual.html).

Reconciliation tracks membership in the configured arXiv query. A paper missing from a
successfully completed snapshot can be reconciled out of cognee. However, arXiv is archival:
a public paper cannot necessarily be removed completely, and withdrawal creates a new
version while earlier versions remain available. If arXiv continues returning a withdrawn
paper, this connector keeps it; withdrawal is not treated as deletion. See arXiv's
[withdrawal policy](https://info.arxiv.org/help/withdraw.html).

Use a dedicated cognee dataset for each configured selection. Changing the selection while
reusing a dataset changes what the next snapshot contains; see the empty-snapshot cleanup
behavior above if the new selection returns no papers.

## Tests

```bash
uv run pytest tests/
```

The tests are offline and use representative Atom responses, fake API pages, and a local
SQLite DLT destination. No arXiv credentials or model API calls are used.
