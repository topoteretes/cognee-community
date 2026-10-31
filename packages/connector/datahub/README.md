# cognee-community-connector-datahub

Sync [DataHub](https://datahub.com) dataset metadata into cognee memory. Afterwards an agent can answer questions like:
- "who owns `orders_daily`, and which domain is it in?"
- "which datasets is `daily_revenue` derived from?"
- "which columns are tagged PII?"

The connector reads DataHub's **GraphQL API** (`/api/graphql`). It is validated against self-hosted DataHub 1.5.0.6. DataHub Cloud uses the same API at `https://<tenant>.acryl.io/gms`, but has not been validated.

## What gets ingested

There is one document per dataset:
- **Identity:** URN, fully qualified name (taken from the URN, e.g. `retail_demo.analytics.daily_revenue`), platform, environment and container path.
- **Documentation:** the description (the edited one first, then the ingested one) and documentation links.
- **Schema fields:** name, native type and description, plus field-level tags and glossary terms.
- **Context:** ownership (owner and ownership type), domain, tags and glossary terms.
- **Lineage:** 1-hop upstream lineage. Fetching each dataset's 1-hop upstreams in the chosen scope is enough to rebuild the lineage graph for that scope.

Relationships are written out as sentences, so cognify extracts them as graph edges:

```
Dataset retail_demo.analytics.daily_revenue is owned by Revenue Analytics.
Revenue Analytics is the Business Owner of dataset retail_demo.analytics.daily_revenue.
Dataset retail_demo.analytics.daily_revenue belongs to domain Retail Demo.
Dataset retail_demo.raw.orders is upstream of retail_demo.analytics.daily_revenue; ...
  Column email of dataset retail_demo.raw.customers is tagged retail_demo.pii.
```

Missing metadata is stated, never invented ("has no owner recorded in DataHub").

**Out of scope for v1:**
- column-level lineage
- dashboards, charts, pipelines and data products as separate entities
- assertions and usage statistics
- Kafka streaming

## Install

```bash
pip install cognee-community-connector-datahub
```

## Use

```python
import cognee
from cognee_community_connector_datahub import datahub_source

source = datahub_source(
    base_url="http://localhost:8080",  # DataHub GMS
    token="<personal access token>",
    platforms=["postgres"],  # optional scope: domains=[urn], containers=[urn]
    frontend_url="http://localhost:9002",  # optional, for links stored with each document
)
await cognee.remember(source, dataset_name="datahub", max_rows_per_table=0)
```

> **Always pass `max_rows_per_table=0`.** It means no row cap. A capped read of a full snapshot would make forget-on-delete remove live datasets.

**Large catalogs need scoping.** Use `platforms`, `domains`, `containers` or `query`, so you ingest a meaningful slice rather than the whole catalog. The connector pages through the scope with `scrollAcrossEntities` and `scrollId` (20 per page), so it is not capped at 10k results the way `searchAcrossEntities` is.

### Authentication

Use a personal access token, sent as `Authorization: Bearer <token>`.

In self-hosted DataHub, token auth must be enabled first:
1. Set `METADATA_SERVICE_AUTH_ENABLED=true` on GMS and the frontend.
2. Create a token under **Settings → Access Tokens**.

## Sync semantics

The connector takes a **full snapshot within the chosen scope** (`write_disposition="replace"`). DataHub's GraphQL API has no global "changed since" cursor, so it does not claim incremental sync.

| Situation | Result |
|---|---|
| Owner, tag, term, description, schema or lineage changed | The rendered document changes and is re-processed. |
| Dataset unchanged | Same content hash, not re-processed. Volatile aspects (usage, profiles, timestamps) are never rendered. |
| Dataset soft-deleted (`status.removed`) | Excluded, so it drops out of the snapshot and cognee's orphan cleanup forgets it. |
| Dataset moved out of scope | Also absent from the snapshot, so it is forgotten from this memory. |
| Request failed or partial (HTTP error, or GraphQL `errors` next to partial data) | The run aborts before anything is loaded. A dataset that is only temporarily unreadable is never forgotten. Transient 429/5xx responses are retried first. |

`resource_name` (default `datahub_datasets`) is the staging table that scopes forget-on-delete. Use one fixed name per DataHub instance and scope.

## Limits

- Up to 100 upstream edges are read per dataset. The document says when there are more.
- The `containers` filter matches datasets directly inside a container.

## Tests

```bash
uv sync --group dev
uv run pytest tests -ra
DATAHUB_LIVE_URL=http://localhost:8080 DATAHUB_LIVE_TOKEN=... uv run pytest tests -m live -ra
```
