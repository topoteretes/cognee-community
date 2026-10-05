# cognee-community-connector-openalex

An OpenAlex Works connector for [Cognee](https://github.com/topoteretes/cognee).
It fetches a scoped OpenAlex Works snapshot, decodes OpenAlex's abstract inverted
index, and sends each work through Cognee's normal document ingestion path.

## Install

```bash
pip install cognee-community-connector-openalex
```

## Usage

```python
import cognee
from cognee_community_connector_openalex import openalex_source

await cognee.remember(
    openalex_source(topic_id="T10001", mailto="you@example.com"),
    dataset_name="openalex_topic",
)
```

Scope a source with one or more of `doi`, `author_id`, `institution_id`, or
`topic_id`. Optionally supply `from_updated_date="YYYY-MM-DD"` to limit the
snapshot to recently updated works. `OPENALEX_API_KEY` and `OPENALEX_MAILTO`
are read from the environment when parameters are omitted.

## Sync behavior

The connector deliberately uses a complete scoped snapshot (`write_disposition="replace"`).
This is safe for deletion handling: a work no longer returned by the scope is removed
from staging, then Cognee's normal orphan cleanup removes its derived memory artifacts.

This MVP does not yet materialize author, institution, venue, or citation edges as
first-class graph records. Those enhancements should be added with their own explicit
identity and lifecycle tests.

## Test

```bash
cd packages/connector/openalex
uv run pytest tests
```
