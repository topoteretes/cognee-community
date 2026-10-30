# cognee-community-connector-pubmed

A PubMed data-source connector for [cognee](https://github.com/topoteretes/cognee): sync
biomedical literature into memory — "what does the literature say about CRISPR delivery
vectors?".

It exposes a `dlt` source you pass directly to `cognee.remember(...)`. You pick what to ingest
with a PubMed query; each matching article's metadata and abstract is rendered to Markdown and
ingested as a **normal document**, so it flows through cognee's `cognify` entity-extraction and
knowledge-graph pipeline rather than the raw relational schema path.

## Requirements

- **Python**: `>=3.10, <3.15`
- **Cognee**: `>=1.4.0` (requires document-mode `DOCUMENT_SOURCE_ATTR` support)
- **NCBI API key**: optional

## Install

```bash
uv pip install cognee-community-connector-pubmed
# or, from this monorepo:
cd packages/connector/pubmed && uv sync --all-extras
```

## Setup

PubMed is public, so the connector works without credentials. An API key is still worth
creating because it raises NCBI's rate limit from **3 to 10 requests per second**:

1. Sign in (or register) at <https://account.ncbi.nlm.nih.gov/>.
2. Open **Account settings** → **API Key Management** → **Create an API Key**.
3. Export it, plus a contact email (NCBI asks tools to send one so they can reach you
   before blocking misbehaving traffic) and your LLM key like any other cognee run:

```bash
export NCBI_API_KEY="your_ncbi_api_key"   # optional
export NCBI_EMAIL="you@example.org"       # optional, recommended
export LLM_API_KEY="sk-..."
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_pubmed import pubmed_source


async def main():
    await cognee.remember(
        pubmed_source(term="CRISPR gene therapy", mindate="2026/01/01"),
        dataset_name="pubmed_knowledge",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="Which delivery methods are used for CRISPR gene therapies?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["pubmed_knowledge"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
```

See `examples/example.py` for a full runnable script.

## Choosing what to ingest

```python
source = pubmed_source(
    term='"base editing"[tiab] AND humans[mh]',  # any PubMed search-box query
    mindate="2020/01/01",  # oldest Entrez date to ingest (YYYY/MM/DD), optional
    maxdate=None,  # newest Entrez date; default: today, advancing every run
    api_key=None,  # falls back to NCBI_API_KEY
    email=None,  # falls back to NCBI_EMAIL
    batch_size=100,  # PMIDs per EFetch request (max 200)
    detect_deletions=True,  # re-list the query's PMIDs each run to forget removed articles
    drop_retracted=True,  # skip retracted articles; forget known ones once retracted
)
```

`term` accepts everything the [PubMed search box](https://pubmed.ncbi.nlm.nih.gov/help/)
does: field tags (`[ti]`, `[au]`, `[mh]`, `[ta]`), Boolean operators, and explicit PMID lists
such as `38000001[uid] OR 38000002[uid]`. Broad queries can match millions of records, so
narrow them (or set `mindate`) before the first run.

Each article becomes one row in the `pubmed_articles` table (`primary_key="id"`,
`write_disposition="merge"`) with the id `pubmed:<PMID>`, plus `title`, `url`, `content`
(Markdown), `journal`, `publication_date`, `doi`, `pmcid` and `entrez_date`.

## Document format

```markdown
# Base editing of PCSK9 in non-human primates.

**PMID:** 38000001 | **DOI:** 10.1038/... | **PMCID:** PMC11000001 | **Journal:** Nature medicine (2024 Mar 15)
**Authors:** Jennifer A Doudna, DR Liu, Gene Editing Consortium
**Publication Types:** Journal Article

## Abstract
### Background
Hypercholesterolemia drives cardiovascular disease.

### Results
LDL cholesterol fell by 60%.

## Medical Subject Headings (MeSH)
- Gene Editing (major topic)
- PCSK9 Inhibitors

## Keywords
- base editing

## Affiliations
- University of California, Berkeley.
```

Structured-abstract labels (`BACKGROUND`, `METHODS`, ...) become `###` sections; unlabelled
abstracts are kept as one paragraph. Sections without data are omitted. Book chapters
(`PubmedBookArticle`, e.g. GeneReviews) are supported too.

## How sync works

### Search, then fetch

E-utilities is a two-step API: `esearch.fcgi` turns the query into PMIDs (filtered by
`datetype=edat`, `mindate`, `maxdate`), then `efetch.fcgi` returns the article XML in batches
of up to 200 PMIDs. All requests are sent as `POST`, so long queries and PMID lists are never
truncated and the API key never appears in a URL.

ESearch returns at most **10,000 PMIDs** per query. When a window matches more, the connector
splits it into smaller Entrez-date windows until each one fits, so large result sets are
listed completely instead of being silently cut off. (Only a single day with more than 10,000
matches can't be split further; the connector logs a warning asking you to narrow the query.)

### Incremental cursor (`edat`)

The Entrez date is the day a record entered PubMed. After a successful run the connector stores
the last synced day in `dlt.current.resource_state()["last_edat"]`, along with the PMIDs it has
ingested (`known_ids`). The next run only searches from that day onward and skips PMIDs it
already has, so unchanged articles are neither fetched nor re-cognified.

NCBI stamps Entrez dates in US Eastern time, so each run re-reads one extra day to catch
records added late on the previous Eastern day. If you change `term`, `mindate` or `maxdate`,
the cursor no longer applies and the whole window is rescanned. Articles you already have are
still not fetched again, and ones the new query no longer matches are forgotten.

### Forget-on-delete

On every run the connector re-lists the PMIDs the query currently matches and emits
`{"id": "pubmed:<PMID>", "_deleted": True}` for known articles that are gone, either removed
from PubMed or no longer selected by the query. With `drop_retracted=True` (the default), it
also checks the query against `retracted publication[pt]`. Retracting an article doesn't change
its Entrez date, so a date window alone would never notice. dlt hard-deletes those rows on merge
and cognee's `orphan_cleanup` removes them from the graph, vector and relational stores.

The re-listing costs about one request per 10,000 matching PMIDs. For very large selections you
can turn it off with `detect_deletions=False`.

**Failure safety:** state is written only after a whole run succeeds, so an interrupted run is
simply repeated. Any API error aborts the run instead of being read as "no articles". If the
query suddenly matches nothing while articles are known, the deletion sweep is skipped rather
than wiping memory.

### Rate limits and retries

Requests are throttled client-side to NCBI's limits: 3/s without a key and 10/s with one. HTTP
429 waits for `Retry-After` when present, falling back to exponential backoff. 5xx responses and
dropped connections are retried with exponential backoff, up to five attempts. An invalid API
key raises a clear error.

### Limitations

- Only metadata and abstracts are ingested, not full text (which lives in PMC).
- Corrections to an article's metadata after it was ingested aren't picked up, because
  revisions don't change the Entrez date.

## Testing

Run the test suite locally (no network access needed):

```bash
uv run pytest tests/ -v
```

The tests use a `FakeNCBISession` that mimics ESearch (Entrez-date windows and the 10,000-PMID
cap) and EFetch XML, and cover:

- XML parsing: structured abstracts, authors and affiliations, journal and date, DOI/PMCID,
  MeSH, book chapters, `DeleteCitation`, retraction flags
- Throttling, 429 `Retry-After` and 5xx/network backoff, API-key and error payload handling
- Window bisection above the 10,000-PMID cap
- Initial backfill, `mindate`/`maxdate` bounds, the incremental `edat` cursor, and no-op reruns
- Query changes, deletion and retraction tombstones, and the outage guard
- `DOCUMENT_SOURCE_ATTR` (`document_source_tag(source) == "pubmed"`)
- A dlt pipeline against an isolated SQLite staging database, proving `_deleted=True` rows are
  removed on merge
