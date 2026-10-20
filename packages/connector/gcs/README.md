# cognee-community-connector-gcs

Read UTF-8 documents from a Google Cloud Storage bucket prefix into
[Cognee](https://github.com/topoteretes/cognee). The connector exposes a dlt source
for `cognee.remember()` / `cognee.add()`, using Cognee's normal document-processing
path. Repeated merge loads download new or changed content and emit deletion
markers for objects that disappear or become oversized.

## Install

Requires Python 3.11–3.13. From this repository's root:

```bash
cd packages/connector/gcs
uv sync
```

This installs the local package, `cognee==1.6.5`, dlt, and Google's official
`google-cloud-storage` SDK into the package's `.venv`.

## Google Cloud setup

1. Select or create a Google Cloud project with billing enabled and enable the
   Cloud Storage API. Create a private test bucket, or use one you can access.
2. Upload a small UTF-8 file at `docs/connector-test.txt`, containing a sentence
   such as `The test project's support hours are 9 AM to 5 PM.`
3. Create a service account under **IAM & Admin → Service Accounts**.
4. Open the bucket's **Permissions → Grant access**, enter the service-account
   email, and grant **Storage Object Viewer** (`roles/storage.objectViewer`).
   This provides object listing and reading permissions; the connector does not
   need permission to upload or delete GCS objects. Use your own authorized
   identity to edit the test files.
5. Configure authentication using one of the options below.

See Google's [bucket setup guide](https://docs.cloud.google.com/storage/docs/discover-object-storage-console)
and [bucket IAM guide](https://docs.cloud.google.com/storage/docs/access-control/using-iam-permissions).
The connector reads private objects; public bucket access is unnecessary.

### Authentication

By default, the connector uses Google's Application Default Credentials (ADC).
For a local service-account key setup, open the service account's
**Keys → Add key → Create new key → JSON → Create**, then point ADC at the
downloaded file:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/absolute/path/to/service-account.json"
```

The value is a file path, not a token or the JSON contents. Keep the key out of
Git and logs. The SDK obtains access tokens from the configured credentials.

Alternatively, pass `credentials_path="/absolute/path/to/service-account.json"`
to `gcs_source()`. An invalid explicit path fails rather than falling back to
another identity. A supplied `client` takes precedence over that path.

ADC also supports local service-account impersonation and attached service
accounts on Google Cloud, avoiding a downloaded private key. For example, with
the Google Cloud CLI installed and permission to impersonate the account:

```bash
# Unset GOOGLE_APPLICATION_CREDENTIALS first if it points at a different identity.
gcloud auth application-default login \
  --impersonate-service-account=SERVICE_ACCOUNT_EMAIL
```

See Google's [key creation guide](https://docs.cloud.google.com/iam/docs/keys-create-delete),
[ADC documentation](https://docs.cloud.google.com/docs/authentication/application-default-credentials),
and [service-account impersonation instructions](https://docs.cloud.google.com/storage/docs/authentication#service_account_impersonation).

### If key creation is blocked

Ask an organization administrator with **Organization Policy Administrator**
(`roles/orgpolicy.policyAdmin`) access to check both constraints:

- `iam.disableServiceAccountKeyCreation` (legacy)
- `iam.managed.disableServiceAccountKeyCreation`

For an approved test-project exception, select that project under
**IAM & Admin → Organization policies**, open each enforced constraint, and use
**Manage policy → Override parent's policy → Add a rule → Enforcement: Off →
Set policy**. Confirm the effective status is **Not enforced** for both; leave
the organization-wide restriction in place. Changes can take up to 15 minutes
to propagate before key creation succeeds. See Google's
[organization policy instructions](https://docs.cloud.google.com/resource-manager/docs/manage-baseline-constraints).

## Run the example

From `packages/connector/gcs`, export the selected scope and your model credentials:

```bash
export GCS_BUCKET="your-bucket-name"
export GCS_PREFIX="docs/"
export LLM_API_KEY="your-model-api-key"
uv run python examples/example.py
```

`GCS_BUCKET` is a bucket name without `gs://`. `GCS_PREFIX` must be non-empty.
The example reads these two variables; the source function receives explicit
bucket/prefix arguments. The shell exports must be present in the terminal
running the command; the example does not explicitly load a `.env` file.

Alternatively, put `GOOGLE_APPLICATION_CREDENTIALS` (the absolute JSON file path),
`GCS_BUCKET`, `GCS_PREFIX`, and your model credentials in the repository root's
`.env`, then run from the repository root:

```bash
uv run --env-file .env --project packages/connector/gcs python packages/connector/gcs/examples/example.py
```

The example waits for ingestion and graph processing before searching the
`gcs_demo` dataset. It does not prune existing data. Cognee model/embedding
configuration must be valid; `LLM_API_KEY` is the usual default-provider setup.
For other providers, follow [Cognee's documentation](https://docs.cognee.ai).

Arguments can override the example's settings:

```bash
uv run python examples/example.py \
  --bucket your-bucket-name \
  --prefix docs/ \
  --credentials-path /absolute/path/to/service-account.json \
  --dataset gcs_demo \
  --question "What are the support hours?"
```

Run `uv run python examples/example.py --help` for options. Reuse the same scope,
dataset, and local state when re-running the example.

## Python usage

```python
import asyncio

import cognee
from cognee_community_connector_gcs import gcs_source


async def main():
    source = gcs_source(bucket_name="your-bucket-name", prefix="docs/")
    await cognee.remember(
        source,
        dataset_name="gcs_demo",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        run_in_background=False,
        self_improvement=False,
    )


asyncio.run(main())
```

### Configuration

| Argument | Default | Meaning |
| --- | --- | --- |
| `bucket_name` | Required | Name of the bucket to read. |
| `prefix` | Required | Non-empty object name prefix, such as `docs/`. |
| `credentials_path` | `None` | Explicit service-account JSON path; otherwise use ADC. |
| `max_objects` | `1000` | Abort before downloading if the listing exceeds this count. |
| `max_object_size` | `10485760` | Skip objects larger than 10 MiB; previously ingested ones leave staging. |
| `client` | `None` | Preconfigured Google Storage client, also useful for tests. The caller owns it. |

## Sync behavior and limitations

- Each run lists the complete prefix. The standard GCS listing API has no
  updated-since filter; the saving is in downloads and content processing.
- dlt resource state stores each eligible object's `updated`, `generation`, and
  `metageneration`, scoped by bucket and prefix. Merge runs download only new or
  changed content generations. Metadata-only changes update state without
  downloading content or emitting document metadata updates.
- Stable `gs://bucket/object` IDs identify documents. Confirmed missing objects
  and previously ingested oversized objects produce hard-delete markers in
  merge mode. GCS objects themselves are never deleted by this connector.
- Keep `write_disposition="merge"` on `remember()` / `add()` for incremental
  loading. Other dispositions use a full-download snapshot fallback; `replace`
  replaces staging, while `append` does not provide deletion synchronization.
- Failed listings, downloads, decoding, or generation checks abort extraction.
  Rerun after resolving the failure. dlt persists inventories with successful
  loads; a successful dlt load does not guarantee subsequent Cognee graph
  processing succeeded.
- Only UTF-8 text is decoded, with an optional BOM. File extensions are not
  filtered; PDFs, images, DOCX, and other binary formats are not extracted and
  may fail decoding. OCR, scanned-document recognition, image understanding,
  and audio/video processing are not supported. Keep the selected prefix limited
  to supported text files.
- Preserve dlt's pipeline state between runs. Losing it loses the previous
  inventory, so previously deleted objects cannot be inferred from it.
- Use independent dlt pipelines for separate accounts or destination datasets.
  Inventory namespacing alone covers only bucket and prefix; this connector
  does not establish account/dataset isolation automatically.
- **Cognee version:** this package pins 1.6.5. Live final-document deletion
  validation on this version cleared staging, Data records, graph nodes/edges,
  and document/chunk vectors. Cognee 1.4.0 skipped core orphan cleanup when the
  corpus became empty, leaving stale memory; do not downgrade to that version
  if you need end-to-end deletion synchronization. Use foreground ingestion
  (`run_in_background=False`) so core orphan cleanup can run.

## Verify changes

1. Run the example with two text documents in the selected prefix and search for
   their contents.
2. Re-run unchanged; no content downloads should be needed in merge mode.
3. Overwrite one document, re-run, and verify its new content is searchable.
4. Delete one document using an authorized GCS identity, re-run, and verify its
   downstream removal while another document remains.
5. Test final-document deletion separately. Check Data records and graph/vector
   state, not only generated answer wording. Searching an emptied dataset may
   raise `NoDataError`, which is the expected absence of searchable memory.

## Offline tests

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
```

Tests use mocked GCS clients and temporary SQLite dlt destinations. They require
no Google credentials or live bucket and cover updates, unchanged runs, deletion
markers, size limits, listing failures, and persistent inventories. A real-bucket
test is still needed to validate service-account access and the complete Cognee
ingestion/search/cleanup flow.
