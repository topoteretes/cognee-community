# cognee-community-connector-terraform-cloud

A Terraform Cloud data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your HCP Terraform / Terraform Cloud (TFC) **runs** into memory — "ask my infra".

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each TFC run — its
outcome, commit message, and plan log — is ingested as a document, incrementally and
with forget-on-delete. Plan logs are passed through a **secret-redaction** step before
anything reaches the graph or the embedding model.

## Requirements

> This connector requires a cognee release that ships the DLT ingestion subsystem used by
> the sibling connectors (`cognee.remember` + `orphan_cleanup`). The `cognee>=` pin in
> `pyproject.toml` targets 1.4.0+.

## Install

```bash
uv pip install cognee-community-connector-terraform-cloud
# or, from this monorepo:
cd packages/connector/terraform-cloud && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_terraform_cloud import terraform_cloud_source

await cognee.remember(
    terraform_cloud_source(
        organization="my-org",
        token="…",  # or TFC_TOKEN in the environment
        workspace_names=["prod"],  # omit to sync every workspace in the org
    ),
    dataset_name="terraform",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by run id
    max_rows_per_table=0,  # 0 = no row cap (see note below)
)

answer = await cognee.search(
    query_text="Which prod runs failed and why?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["terraform"],
)
```

Scope what you ingest with `workspace_names=[...]`; omit it to sync every workspace the
token can read. See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

A TFC run is **immutable** once created, so each run is keyed by its run id and ingested
exactly once (`write_disposition="merge"`). The **incremental cursor** is the run
`created-at` timestamp: each sync lists the most recent runs per workspace and emits only
runs created since the last run (plus any run new to the corpus, e.g. from a workspace you
just added to the selection). The cursor is persisted in dlt's per-resource state.

**Forget-on-delete** is scoped to the workspace disappearing: TFC has no deletion feed, so
each sync re-enumerates the organization's workspaces and any run whose workspace is gone is
emitted with the `_deleted` hard-delete marker. dlt removes those rows on `merge` and
cognee's existing `orphan_cleanup` then purges them from the graph + vector + relational
stores. Scoping deletion to a vanished workspace (rather than to a run ageing out of the
recent-runs window) means old runs are never mistaken for deletions. An empty workspace
sweep while runs were known is treated as a transient failure — deletion is skipped and
state preserved — so a network blip can never wipe the dataset.

## Secret redaction

Terraform marks *declared* sensitive attributes as `(sensitive value)` itself, but provider
credentials, `TF_VAR_*` echoes, connection strings, and raw key material routinely slip into
plan output. Every plan log is passed through `redact_secrets(...)` before it is emitted,
replacing the common secret shapes with `[REDACTED]`:

- PEM private-key blocks
- AWS access-key ids (`AKIA…` / `ASIA…`)
- Credentials embedded in URLs (`scheme://user:password@host`)
- `<sensitive-name> = <value>` assignments (keys containing `secret`, `password`, `token`,
  `api_key`, `access_key`, `client_secret`, `credential`, …)

It is intentionally conservative — it targets recognisable secret shapes rather than guessing
at high-entropy strings, so ordinary resource addresses and plan diffs stay intact.

## Setup

1. Create a Terraform Cloud API token (User settings → Tokens, or a team/organization token):
   <https://app.terraform.io/app/settings/tokens>
2. Export your connection details (the token is read-only here — the connector only issues
   `GET` requests):

   ```bash
   export TFC_TOKEN="…"
   export TFC_ORGANIZATION="my-org"
   # optional: export TFC_WORKSPACES="prod,staging"
   ```

3. Set your LLM key (`LLM_API_KEY`) like any other cognee run.

For Terraform Enterprise, pass `base_url="https://<your-host>/api/v2"`.

## Testing

```bash
uv run pytest tests/
```

The tests mock the TFC API (no live token) and cover secret redaction, pagination,
the incremental `created-at` cursor, workspace-scoped forget-on-delete, the transient
empty-sweep guard, and an end-to-end dlt merge that acts on the hard-delete marker.
