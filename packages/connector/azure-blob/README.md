# cognee-community-connector-azure-blob

Read UTF-8 text documents from an Azure Blob Storage container prefix into
[Cognee](https://github.com/topoteretes/cognee). This package exposes a document-mode
dlt source: documents go through normal Cognee text processing and graph extraction.
Merge runs download new or changed blobs and remove previously ingested documents
that are deleted upstream or no longer eligible.

The implementation follows [GCS PR #452](https://github.com/topoteretes/cognee-community/pull/452)
and the extraction boundary of [S3 PR #209](https://github.com/topoteretes/cognee-community/pull/209),
using Azure's Last-Modified timestamps and ETags for change detection.
Implements [cognee issue #4762](https://github.com/topoteretes/cognee/issues/4762).

## Install

Requires Python 3.11–3.13. From this repository's root:

```bash
cd packages/connector/azure-blob
uv sync --locked
```

This installs the local package, `cognee==1.6.5`, dlt with SQLAlchemy, Azure's
`azure-storage-blob` SDK, and development tools. The Cognee pin follows GCS PR #452,
which verified final-document graph/vector cleanup on this version after finding
stale memory with 1.4.0. Do not downgrade if you require deletion synchronization.

## Azure setup and authentication

1. Use an Azure Storage account and a private blob container you are authorized to read.
2. Upload a small UTF-8 text file, such as `docs/support.txt`, containing
   `Support is available from 9 AM to 5 PM.`
3. Configure the account endpoint and credentials using one of the options below.
   A SAS must allow **read and list** for the selected container. The connector
   never uploads, modifies, or deletes Azure blobs; use your own authorized
   identity to change test fixtures.

See Microsoft's [Blob client documentation](https://learn.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.blobserviceclient)
and [SAS overview](https://learn.microsoft.com/en-us/azure/storage/common/storage-sas-overview).

### SAS token

```bash
export AZURE_STORAGE_ACCOUNT_URL="https://your-account.blob.core.windows.net"
export AZURE_STORAGE_SAS_TOKEN="your-container-or-account-sas-token"
```

Use the SAS query-string token, not a connection string or a container URL.
Keep credentials out of source files and Git.

### Account key

```bash
export AZURE_STORAGE_ACCOUNT_URL="https://your-account.blob.core.windows.net"
export AZURE_STORAGE_KEY="your-storage-account-key"
```

Credential precedence is explicit `credentials=`, then `AZURE_STORAGE_SAS_TOKEN`,
then `AZURE_STORAGE_KEY`. Unset a stale SAS variable before switching to a key.
The account endpoint comes from `account_url=` or `AZURE_STORAGE_ACCOUNT_URL`.
Missing configuration raises a helpful `ValueError`; Azure verifies credentials
and container access on the first request, not when the source is constructed.

A preconfigured `azure.storage.blob.BlobServiceClient` can be passed as `client=`.
It bypasses credential discovery and supplies the account endpoint through its
`url` property. The caller owns that client and must close it. Otherwise, the
connector creates and closes its own client during extraction, including on errors.
Stored document URLs and inventory keys exclude SAS query strings.

## Run the example

From this package directory, export the source scope and model credentials:

```bash
export AZURE_STORAGE_CONTAINER="your-container"
export AZURE_STORAGE_PREFIX="docs/"
export LLM_API_KEY="your-model-api-key"
uv run --locked python examples/example.py
```

The example waits for ingestion and graph processing, then searches the dedicated
`azure_blob_demo` dataset. Configure Cognee's LLM and embeddings before running it;
see [Cognee's documentation](https://docs.cognee.ai) for provider settings.
The example does not prune memory or modify Azure objects.

```bash
uv run --locked python examples/example.py \
  --container your-container \
  --prefix docs/ \
  --dataset azure_blob_demo \
  --question "What are the support hours?"
```

Run with `--help` for options, including size and count limits. Credentials are read
from the environment rather than command-line flags. The example does not explicitly
load `.env`; to use the repository root's file, run from the root:

```bash
uv run --env-file .env --project packages/connector/azure-blob \
  python packages/connector/azure-blob/examples/example.py
```

## Python usage

```python
import asyncio

import cognee
from cognee_community_connector_azure_blob import azure_blob_source


async def main():
    source = azure_blob_source(container_name="your-container", prefix="docs/")
    result = await cognee.remember(
        source,
        dataset_name="azure_blob_demo",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        run_in_background=False,
        self_improvement=False,
    )
    if result.status != "completed":
        raise RuntimeError(result.error or result.status)


asyncio.run(main())
```

The factory returns a dlt source; it is no longer the temporary client-setup
context manager from the initial scaffold.

| Argument | Default | Meaning |
| --- | --- | --- |
| `container_name` | Required | Azure container name. |
| `prefix` | Required | Non-empty blob name prefix, such as `docs/`. |
| `account_url` | Environment | Storage account endpoint, not a container/blob URL. |
| `credentials` | Environment | SAS token or account key. |
| `max_objects` | `1000` | Abort before downloads if the prefix exceeds this count. |
| `max_object_size` | `10485760` | Maximum blob size in bytes (10 MiB). |
| `client` | `None` | Caller-owned, preconfigured `BlobServiceClient`. |

## Sync behavior and limitations

- Each run consumes every listing page under the selected prefix. Incremental
  savings apply to downloads and content processing, not metadata listing.
- dlt resource state records each eligible blob's Last-Modified timestamp, ETag,
  and name. A merge rerun downloads only blobs whose timestamp/ETag pair changed.
  ETags are opaque identifiers and are compared for equality, never sorted.
  Azure metadata edits may change this pair and trigger a download too.
- Downloads target the current blob with an ETag `If-Match` condition. An overwrite
  between listing and download aborts extraction; rerun to read the current version.
  Historical versions and snapshots are not enumerated.
- In merge mode, previously ingested blobs absent from the completed listing
  are checked directly. Only `BlobNotFound` emits a deletion marker. A blob that
  still exists retains its prior document and inventory entry until the next sync.
  Other lookup errors and listing failures abort without advancing the inventory.
- Standard SDK retries handle retryable HTTP/network failures. Exhausted retries,
  invalid UTF-8, unreadable archive-tier blobs, and failed version checks propagate
  to dlt, leaving the prior loaded rows and inventory intact. Archived blobs are
  not automatically rehydrated or treated as deleted.
- Supported extensions (case-insensitive): `.txt`, `.md`, `.markdown`, `.json`,
  `.csv`, `.xml`, `.yaml`, `.yml`, `.log`, `.html`, `.htm`, `.rst`. Extraction decodes
  UTF-8 with an optional BOM; HTML/CSV/JSON remain text. PDFs, DOCX, images, audio,
  archives, and extensionless blobs are skipped before download. No OCR or binary
  document extraction is provided, matching the GCS/S3 text-only boundary.
- Unsupported and oversized blobs count toward `max_objects`. Empty folder
  placeholders (zero bytes, names ending in `/`) do not. Blobs exactly at the size
  limit, empty text, and Unicode are accepted. Previously ingested blobs that grow
  oversized or become unsupported produce deletion markers in merge mode.
- Stable, escaped account/container/blob URLs identify documents. Inventory scopes
  include account endpoint, container, and prefix. Cognee's pipeline scope marker
  also isolates its incremental state by scope and destination dataset. Give each
  source scope a **dedicated dataset** and preserve the local dlt pipeline state.
  When using dlt directly, use independent pipelines for independent destinations.
- Pass `write_disposition="merge"` through Cognee for incremental sync. Other
  dispositions download full snapshots so unchanged rows are retained. `replace`
  replaces staging; `append` does not synchronize deletions. Keep the write mode
  consistent across runs; incremental merge cannot recover history lost from state.
- Foreground ingestion (`run_in_background=False`) is required for Cognee orphan
  cleanup. A successful dlt load does not guarantee subsequent Cognee graph
  processing succeeded; inspect the returned Cognee status and retry failures.

## Tests and verification

```bash
uv run --locked pytest tests/ -q
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Tests use mocked Azure SDK clients with real temporary SQLite dlt destinations.
They cover credential precedence, client ownership, updates, unchanged reruns,
listing pagination/failures, ETag races, deletion confirmation and reappearance, final-row staging
cleanup, limits, encoding, persistent inventories, and account/dataset scoping.
They need no Azure credentials or model API calls. These tests verify connector
and staging behavior; they do not constitute live Azure or end-to-end graph/vector
validation.

Before release, use an authorized test container (or Azurite for service-level
checks) and valid model configuration to run the example. Repeat with an edit,
an unchanged rerun, a deletion while another document remains, and deletion of the
final document. Verify Cognee Data records, graph and vector contents as well as
search results. An empty dataset may raise `NoDataError`, which the example handles.
