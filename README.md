# cognee-community-connector-pubmed

Sync PubMed article titles, abstracts, and source URLs into [cognee](https://github.com/topoteretes/cognee).

## Install

```bash
uv pip install cognee-community-connector-pubmed
```

## Usage

NCBI asks clients to identify a contact email. Set it once, then run a full snapshot for a stable query:

```bash
export PUBMED_EMAIL="you@example.com"

from cognee_community_connector_pubmed import pubmed_source

source = pubmed_source('"traditional Chinese medicine"')
```

Pass an optional `NCBI_API_KEY` to use NCBI's higher request allowance. The connector applies the documented polite request spacing in either case.

`date_from` and `date_to` add an E-utilities `edat` range for finding recently edited articles:

```python
recent = pubmed_source("graph rag", date_from="2026/08/01", date_to="2026/08/31")
```

The source uses a full snapshot (`write_disposition="replace"`), so run the same unrestricted selection periodically to reconcile articles that no longer appear upstream. A date-limited discovery query is not a replacement for that reconciliation run.

## Test

```bash
uv run pytest tests/
```

The tests use fixture XML and never call NCBI.
