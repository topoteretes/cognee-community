# cognee-community-connector-sharepoint

A SharePoint / OneDrive data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync document libraries into memory — incrementally, with forget-on-delete.

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path. Files are ingested as **documents** (routed through normal chunking + LLM graph
extraction) via cognee's document-mode marker.

## Requirements

- Python 3.11–3.13 and `cognee==1.4.2`.
- An Entra ID (Azure AD) app registration with the `Files.Read.All` **application** permission and
  admin consent. See **Setup** below.

## Install

```bash
uv pip install cognee-community-connector-sharepoint
# or, from this monorepo:
cd packages/connector/sharepoint && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_sharepoint import sharepoint_source

await cognee.remember(
    sharepoint_source(site_id="contoso.sharepoint.com,<site-guid>,<web-guid>"),
    dataset_name="my_sharepoint",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by item id
)

answer = await cognee.search(
    query_text="Summarize the specs in the engineering library.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["my_sharepoint"],
)
```

Every argument falls back to an environment variable:

| Argument | Environment variable | Default |
| --- | --- | --- |
| `site_id` | `SHAREPOINT_SITE_ID` | — |
| `drive_ids` | `SHAREPOINT_DRIVE_IDS` (comma-separated) | all libraries on the site |
| `tenant_id` | `MICROSOFT_TENANT_ID` | — |
| `client_id` | `MICROSOFT_CLIENT_ID` | — |
| `client_secret` | `MICROSOFT_CLIENT_SECRET` | — |
| `max_file_size_mb` | `SHAREPOINT_MAX_FILE_SIZE_MB` | `25` |

The credentials use the `MICROSOFT_` prefix rather than `SHAREPOINT_` on purpose: the same Entra ID
app registration is meant to be shared with the planned Teams and Outlook connectors.

Pass `site_id` to sync every document library on a site, or `drive_ids` to pick specific libraries.
See `examples/example.py` for the full flow.

## Supported files

`.pdf`, `.docx`, `.xlsx`, `.txt`, `.md`, `.csv`, up to `max_file_size_mb` (25 MB by default).

Anything else — `.pptx`, the legacy binary `.doc` / `.xls`, images, archives — is skipped with a
warning naming the file, as is any file over the size cap or any file that fails to parse. A skip
never fails the sync.

Graph can convert Office formats server-side (`?format=pdf`), which would cover `.pptx` and the
legacy formats with no extra parsers, but its least-privileged application permission is
`Files.ReadWrite.All`. This connector stays read-only instead.

## How sync + forget-on-delete work

Incremental sync uses a Microsoft Graph delta link per document library, persisted in dlt's
per-resource state. The first run enumerates the library with a tokenless `delta` call and stores
the returned `@odata.deltaLink`; later runs follow that link and receive only what changed. Graph
documents `delta` as the only way to enumerate a drive completely, so it is used for the initial
read as well.

Items carrying Graph's `deleted` facet are emitted with the `_deleted` hard-delete marker; dlt drops
them on `merge` and cognee's `orphan_cleanup` purges them from the graph, vector, and relational
stores.

Row ids are `"{drive_id}:{item_id}"`, because DriveItem ids are unique within a drive but not across
drives.

### Limitations

- Scope is a site and its document libraries. Folder-level scoping is not supported: delta responses
  omit `parentReference.path`, so a subtree cannot be filtered client-side.
- Graph can invalidate a delta link at any time, answering `HTTP 410`. The connector drops the link
  and re-enumerates the library from scratch. Live files are re-emitted and merged idempotently, but
  a file deleted *during* the invalidation window is never reported as deleted, so it survives in
  memory until it is removed again.
- Graph may replay an item several times within one delta feed; only the last occurrence is used.

## Setup

You need an Entra ID app registration. If you have never made one, follow this exactly — it takes
about five minutes and requires a Microsoft 365 tenant administrator for step 4.

1. **Register the app.** Go to [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID**
   → **App registrations** → **New registration**. Give it a name (for example
   `cognee-connector`), leave "Supported account types" on *Accounts in this organizational
   directory only*, leave the Redirect URI blank, and click **Register**.

2. **Copy the ids.** On the app's **Overview** page, copy **Application (client) ID** into
   `MICROSOFT_CLIENT_ID` and **Directory (tenant) ID** into `MICROSOFT_TENANT_ID`.

3. **Add the permission.** Go to **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Application permissions** (not *Delegated* — this connector runs unattended) → search for
   `Files.Read.All` → check it → **Add permissions**.

4. **Grant admin consent.** On the same page click **Grant admin consent for &lt;tenant&gt;** and
   confirm. Application permissions do nothing until this is done; a global administrator or
   privileged role administrator has to click it. The permission's status column should read
   *Granted*.

5. **Create a client secret.** Go to **Certificates & secrets** → **Client secrets** → **New client
   secret**, set an expiry, and click **Add**. Copy the **Value** column immediately — it is shown
   only once — into `MICROSOFT_CLIENT_SECRET`.

6. **Find the site id.** Call Graph with the site's path:

   ```bash
   GET https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/Marketing
   ```

   The `id` in the response is the three-part value (`hostname,site-guid,web-guid`) that goes into
   `SHAREPOINT_SITE_ID`. To sync only some libraries, call `GET /sites/{siteId}/drives` and put the
   drive ids you want into `SHAREPOINT_DRIVE_IDS`.

7. **Export everything**, plus your `LLM_API_KEY` like any other cognee run:

   ```bash
   export MICROSOFT_TENANT_ID="..."
   export MICROSOFT_CLIENT_ID="..."
   export MICROSOFT_CLIENT_SECRET="..."
   export SHAREPOINT_SITE_ID="contoso.sharepoint.com,<site-guid>,<web-guid>"
   export LLM_API_KEY="sk-..."
   ```

`Files.Read.All` is the least-privileged application permission for all three Graph calls this
connector makes: `GET /sites/{siteId}/drives`, `GET /drives/{driveId}/root/delta`, and
`GET /drives/{driveId}/items/{itemId}/content`. `Sites.Read.All` also works but is a higher
privilege than needed.

## Testing

```bash
uv run --with pytest pytest tests/
# the end-to-end dlt test additionally needs duckdb:
uv run --with pytest --with duckdb pytest tests/
```

The tests mock Microsoft Graph (no msal, no network, no live tenant) and cover the ingest path,
skip-with-a-reason for unsupported and oversized files, the incremental cursor, forget-on-delete,
delta-link invalidation, and the dlt resource wiring.
