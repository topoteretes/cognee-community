# cognee-community-connector-airflow

Sync Apache Airflow DAG metadata into cognee memory. Afterwards an agent can answer questions like:
- "what runs before `publish_daily_revenue`?"
- "who owns the DAG that failed last night?"
- "which DAG writes `analytics.daily_revenue`?"

The connector reads the **Airflow 3.x public REST API (`/api/v2`)**. Airflow 2's `/api/v1` is not supported. It is validated on Airflow 3.3.0.

## What gets ingested

There is one document per DAG:

- **DAG:** id, description, `doc_md`, owners, tags, schedule (`timetable_description`), paused/active state, and file location.
- **Tasks:** operator, owner, retries, trigger rule and `doc_md`.
- **Dependencies:** from `downstream_task_ids`, written as "`extract_orders` runs before `validate_orders`."
- **Inlets/outlets:** from Airflow Assets (`/api/v2/assets`), written as "`transform_revenue` writes dataset `retail_demo.analytics.daily_revenue` (asset `postgres://…/retail_demo/analytics/daily_revenue`)."
  - For SQL schemes (postgres, mysql, mssql, redshift, trino), the asset URI `<db>/<schema>/<table>` is also written as the dotted table name.
  - The dotted name is what catalogs such as DataHub use, so cognee can connect a DAG to the same table ingested from the catalog.
  - This relies on the explicit metadata in the asset URI, not on name similarity.

**Optional, off by default (`include_runs=True`):** one more document per DAG summarising its recent runs, bounded by `runs_window_days` and `max_runs_per_dag`. For each run it lists:
- the run state and timings
- the failed tasks
- the tasks skipped because an upstream task failed
- for both, the assets they therefore did not update

This is a recent-history window, not incremental sync.

Relationships are written out as sentences so that cognify extracts them as graph edges. dlt `references` hints are not turned into edges by cognee's document path.

## Install

```bash
pip install cognee-community-connector-airflow
```

## Use

```python
import cognee
from cognee_community_connector_airflow import airflow_source

source = airflow_source(
    base_url="http://localhost:8080",  # the Airflow API server, without /api/v2
    username="admin",
    password="...",  # or token="<JWT / deployment token>"
    dag_id_pattern="retail_*",  # optional scope: dag_ids=[...], tags=[...]
    include_runs=True,  # optional recent-run summaries
)
await cognee.remember(source, dataset_name="airflow", max_rows_per_table=0)
```

> **Always pass `max_rows_per_table=0`.** It means no row cap. A capped read of a full snapshot would make forget-on-delete remove live DAGs. cognee 1.6.x already defaults to `0`, but the `DLT_MAX_ROWS_PER_TABLE` env var can override that default.

### Authentication

| Option | When to use it |
|---|---|
| `token=` | A pre-issued JWT or a managed-deployment token. |
| `username=` / `password=` | Exchanged for a JWT via `POST /auth/token`, which works with the Simple and FAB auth managers. The connector logs in again on `401`, since JWTs expire after 24h by default. |

## Sync semantics

The connector uses a **full snapshot** (`write_disposition="replace"`). Airflow has no reliable "changed since" for DAG structure.

| Situation | Result |
|---|---|
| DAG edited | Its document changes, so cognee re-cognifies it. |
| DAG unchanged | Same content hash, so it is not re-processed. Volatile fields (next run, last parsed time) are kept out of the structure document. |
| DAG removed from its bundle | Airflow marks it **stale** and the connector excludes it (`exclude_stale=true`). It drops out of the snapshot, and cognee's orphan cleanup forgets it. |
| DAG paused | Still exists, so it is **kept**, with `State: paused`. |
| A request fails | Everything is fetched before the first row is yielded, so the run aborts and the previous snapshot stays in memory. Transient 429/5xx responses are retried first. |

`resource_name` (default `airflow_dags`) is the staging table that scopes forget-on-delete. Use one fixed name per Airflow deployment.

## Limits

- **Mapped and dynamic tasks** appear as their template task.
- **Inlets/outlets** only appear for tasks that declare Airflow Assets. Lineage that exists only in OpenLineage events is not read.
- **No log content is ingested.** Run summaries say which task failed, not why.
- **Pagination** uses `limit`/`offset`, which all 3.x versions support. The keyset cursor only exists from 3.3 onward, so the connector does not use it.

## Tests

```bash
uv sync --group dev
uv run pytest tests -ra
AIRFLOW_LIVE_URL=http://localhost:8088 AIRFLOW_LIVE_USERNAME=admin AIRFLOW_LIVE_PASSWORD=admin \
  uv run pytest tests -m live -ra
```
