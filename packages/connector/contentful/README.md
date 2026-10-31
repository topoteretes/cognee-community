# cognee-community-connector-contentful

A Contentful Delivery connector for [Cognee](https://github.com/topoteretes/cognee).
Import published entries, asset metadata, and content models as searchable documents,
then reconcile edits and deletions on subsequent runs using Contentful's native Sync interface.

## Install and configure

Requires Python `>=3.10`, Cognee `>=1.6.3,<2`, dlt `>=1.30,<2`, and
httpx `>=0.28.1`. The lockfile retains Cognee `1.6.3` and dlt `1.30.0` for
reproducible development. Cognee must provide document synchronization and pipeline
scoping; the connector checks this contract before extracting content. Cognee's own
dependency requirements may restrict the dlt version that can be installed together.

From this repository:

```bash
cd packages/connector/contentful
uv sync --locked
```

For an application environment, install this directory with your package manager:

```bash
uv pip install ./packages/connector/contentful
```

Create a Contentful **Content Delivery API** token for your space and environment.
Use a concrete environment ID; aliases whose targets change are unsupported.
The factory requires explicit configuration; the example reads these environment variables:

```bash
export CONTENTFUL_DELIVERY_TOKEN="your-delivery-token"
export CONTENTFUL_SPACE_ID="your-space-id"
export CONTENTFUL_ENVIRONMENT_ID="master"  # optional; the example defaults to master
export CONTENTFUL_RESOURCE_NAME="contentful_documents"  # optional example default
export LLM_API_KEY="your-model-provider-key"
uv run python examples/example.py
```

Use Cognee's usual model/embedding configuration for providers other than its defaults.
The example imports nothing from Cognee and performs no synchronization when the
Contentful space or token is missing. With credentials it performs two foreground syncs
against the same source, prints extraction statistics, and searches the resulting dataset.
Published document text is submitted to your configured Cognee model provider.

## Usage

```python
import cognee
from cognee_community_connector_contentful import contentful_source

resource = contentful_source(
    space_id="your-space-id",
    token="your-delivery-token",
    resource_name="contentful_products",
    content_type_ids=["product"],
    locales=["en-US"],
)
await cognee.remember(
    resource,
    dataset_name="contentful_products",
    primary_key="id",
    write_disposition="merge",
    run_in_background=False,
    max_rows_per_table=0,
    self_improvement=False,
)
print(resource.cognee_sync_stats)

answer = await cognee.search(
    query_text="Which products are suitable for outdoor use?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["contentful_products"],
)
```

The factory returns a document-mode dlt resource. All arguments are keyword-only:

```python
contentful_source(
    *,
    space_id,
    token,
    resource_name,
    environment="master",
    content_type_ids=None,
    locales=None,
    include_assets=True,
    source_id="default",
    host="cdn.contentful.com",
    service=None,
    check_active=None,
)
```

| Argument | Behavior |
|---|---|
| `space_id`, `token` | Required explicit space ID and Delivery token. The factory never reads environment variables. |
| `resource_name` | Required staging table and pipeline scope name. Use lowercase snake case starting with a letter; names beginning with `_dlt` are reserved. Keep this name unique per logical source in a dataset. |
| `environment` | Concrete environment ID, default `master`. |
| `content_type_ids` | Select entry types and their model documents. Omitted means all types; an empty selection is invalid. |
| `locales` | Select locale keys in preferred title order. Omitted means all locales, with the environment default first and remaining keys sorted. Entry fields marked `localized: false` in the model retain their default-locale value under its original label. Localized fields (including titles) use only selected keys, even when only one translation exists; unknown fields also use only selected keys. Asset fields with a single locale retain their metadata. No locale fallback chains are followed. An empty selection is invalid. |
| `include_assets` | Include all published asset metadata by default; binaries are never downloaded. |
| `source_id` | Stable logical source identifier, default `default`. Use distinct IDs and resource names for independently managed selections. |
| `host` | Either `cdn.contentful.com` or `cdn.eu.contentful.com`. |
| `service` | Optional synchronous `httpx.Client` for testing or custom transport; the caller owns its lifecycle. |
| `check_active` | Optional synchronous callback invoked before every HTTP attempt, including retries and individual lookups, and between retry sleep steps of at most 0.25 seconds. Raise to cancel extraction; checkpoints are not advanced. |

Entry titles come from their content model's `displayField`, using the first usable selected
locale (or the actual environment default when no locales are selected), then the entry ID.
The default comes from the environment-scoped Delivery `/locales` inventory.
Entries include regional Contentful editor URLs, including `app.eu.contentful.com` for EU spaces. Document
text leads with the title and fields, then provenance. Rich text becomes plain text with
paragraph boundaries and linked entry/asset IDs. Field IDs and locale labels remain visible;
links are preserved without recursively expanding their content. Unordered fields are sorted,
while meaningful content-array order is retained.
Only fields declared `RichText` by their content model are interpreted as rich text; Object
fields remain canonical JSON even when they contain a `nodeType` key. Invalid or unsupported
rich-text structures are preserved as labeled JSON so other documents still synchronize.
Invalid identities, resource scopes, and collection metadata still fail extraction safely.

Asset documents contain localized titles/descriptions, filenames, MIME types, HTTPS URLs,
sizes, and available dimensions. Models are separate documents; unchanged models are not
rendered again. Changing an existing model's display field triggers a full refresh to update
entry titles; adding an unrelated model keeps the incremental Sync token. If selected entries
arrive before their model, the checkpoint records the missing model ID. When that model
appears, a full refresh repairs their titles and field rendering. Failed refreshes leave
this repair pending. Older checkpoints without this tracking perform one full refresh to establish it
and apply the corrected locale-selection policy. A changed environment
default locale also refreshes titles. This connector
reads published Delivery content; drafts, Preview access, binary extraction, and built-in
scheduling are outside its scope.
An older checkpoint without default-locale metadata performs one full refresh to apply the
correct title policy before resuming incremental runs.

The example also accepts comma-separated `CONTENTFUL_CONTENT_TYPE_IDS` and
`CONTENTFUL_LOCALES`, `CONTENTFUL_INCLUDE_ASSETS=true|false`, `CONTENTFUL_SOURCE_ID`,
`CONTENTFUL_DELIVERY_HOST`, `CONTENTFUL_DATASET`, and `CONTENTFUL_QUERY`.

## Synchronization and source ownership

Each `remember()` invocation performs one synchronization. Schedule repeated invocations
externally if you need periodic updates. Use **foreground**, **merge**, `id` primary keys,
and **no row cap** exactly as shown above. The resource uses dlt's default merge strategy and
an internal priority column so final reconciled rows deterministically replace provisional
rows. Replace, append, and alternate merge strategies are rejected.

The connector uses an unfiltered Contentful Sync stream and applies selections locally,
so upstream deletion events remain available. Live documents stream page by page; a temporary
SQLite spool holds event payloads needed for conflict resolution without accumulating the
whole delta's payloads in memory. Membership IDs remain O(N): dlt state stores the selected
document IDs and rewrites them on state-changing loads (roughly 369 KB of JSON for 10,000 IDs,
depending on ID length). Reconciliation also keeps ID sets in memory; the SQLite spool bounds
payload buffering, not total memory independently of source size.
Tombstones and the terminal checkpoint are accepted only after complete
traversal and reconciliation. Incomplete extraction does not commit a new checkpoint or load
its provisional rows. Stable document IDs distinguish space, environment, entity kind, and
upstream ID, preventing an entry and asset sharing an ID from colliding.

Source identity includes host, space, concrete environment, and `source_id`; it excludes
tokens and mutable selections. This ownership is persisted for `resource_name`; reusing a
name for a different source fails before any request. Rotating credentials preserves
checkpoints. Changing types, locales, or asset inclusion under the **same** source ID and
resource name triggers a complete selection refresh: selected documents are upserted and
previously selected documents that disappeared are deleted. Failed refreshes preserve the
previous selection and records.

Separate sources require separate resource names and source IDs, giving them independent
staging tables, membership, checkpoints, and cleanup scopes. Keep the same dataset, resource
name, and source ID on subsequent runs. Changing the dataset creates separate storage; it
does not retire the old source. If intentionally replacing a source, retire its old dedicated
dataset separately.

Contentful requests always use the configured environment route. Only official Delivery
hosts over HTTPS are accepted, and redirects are disabled. Valid continuation URLs contribute
their tokens, rather than controlling the next request's host or environment. Legacy same-space
continuations without an environment segment are supported; conflicting environments and unsafe
URLs abort synchronization. Tokens are sent in bearer headers and omitted from connector errors.
The connector does not install filters on process-wide HTTPX loggers.

Repeated identical Sync events are coalesced. Conflicting entry/asset events for the same ID
are resolved with a same-environment Delivery lookup using all locales. If the CDN returns an
older version, the lookup is retried once; an unambiguously newer Sync version can then win.
Malformed, wrong-scope, and unresolved conflicts fail the extraction rather than depending
on event arrival order.

Content models are fetched separately with validated `skip`/`limit` pagination. Reconciliation
requires two consecutive matching complete inventories within three passes. Missing previously
ingested models are confirmed with individual Delivery lookups before deletion. A lookup that
still finds the model retains the previous model. Duplicate IDs, malformed pages, changing
totals, and unstable inventories fail the cycle. This is a conservative consistency check,
not a transactional Contentful snapshot.

## Failure recovery and diagnostics

Request timeouts and transient retries are bounded; rate-limit retries honor Contentful's
reset header. Authentication failures and incomplete model listings fail extraction without
advancing managed state. A rejected/expired Sync token triggers at most one full refresh in
the same invocation and dataset. Only a complete successful refresh replaces the checkpoint
and membership, then reconciles deletions. A failed refresh preserves prior staging and state.
Rejected continuation pages during an initial traversal abort without restarting that initial
sync. Non-numeric or non-finite retry reset headers use bounded exponential backoff.

A successful dlt load is not the same as successful Cognee ingestion, cognification, or cleanup.
Keep dlt staging and pipeline state: retained tables allow the next unchanged incremental run
to reconcile loaded documents again and retry orphan cleanup. Cognee can log individual cleanup
failures without raising, so inspect your run logs and use the storage verification tests when
checking recovery. Do not delete staging to recover an ingestion failure.

After a dlt **load** failure, retry using the same dataset/resource. A pending load package may
be recovered first without fetching fresh Contentful changes; invoke the pipeline again after
successful package recovery to perform a new extraction. Do not globally prune Cognee to repair
this connector.

`resource.cognee_sync_stats` resets for each extraction and contains integer counters:

- `synced`: unique final live-document actions emitted by the extraction.
- `deleted`: previously selected documents removed by this extraction. Tombstones that only
  cancel provisional rows published and deleted within the same cycle do not increase this count.
- `failed_rate_limit`: HTTP attempts that received a rate-limit response, including retries.
- `failed_auth`: HTTP attempts that received an authentication/authorization failure.

These counters describe extraction, not successful Cognee ingestion or cleanup. Failed runs
may have partial statistics, and package recovery without fresh extraction does not create
new extraction counts.

The package exports `ContentfulSourceError`, `ContentfulAPIError`, `ContentfulAuthError`,
`ContentfulRateLimitError`, `ContentfulSyncTokenError`, and `ContentfulResponseError` so callers
can distinguish source, API, authentication, exhausted rate limits, rejected Sync tokens,
and invalid provider responses. Invalid configuration raises `ValueError`. Error messages are
sanitized; cancellation exceptions from `check_active` propagate unchanged. dlt may wrap extraction errors in its pipeline exceptions.

### Migrating the unreleased hashed staging names

Earlier versions of this unreleased connector generated `contentful_documents_<hash>` table
names. Explicit resource names do not automatically adopt or remove that staging state.
Use a new dedicated dataset with a readable resource name, complete its initial synchronization,
and verify search and deletions before explicitly retiring the old dedicated dataset. Renaming
a resource inside an existing dataset does not remove the old resource's documents. Never delete
shared staging or globally prune Cognee as a migration shortcut.

## Verification

Run from the package directory:

```bash
uv sync --locked
uv run pytest -q tests
uv run ruff check .
uv run ruff format --check .
uv build
uv run python -c 'from cognee_community_connector_contentful import contentful_source; print("Import OK")'
```

The automated tests use a mocked Contentful transport and model/embedding calls. Integration
tests use real local SQLite, Ladybug, and LanceDB storage to inspect document ingestion,
derived content, deletion, source isolation, and recovery. They require no Contentful token or
model-provider credentials. CI runs the full offline suite on Python 3.10–3.14 with the lockfile
and separately resolves the newest permitted dependencies. A passed offline suite establishes
local behavior; it does not establish authenticated Contentful compatibility.

Before claiming verified live compatibility, use a disposable Contentful environment and a
dedicated Cognee dataset to perform this lifecycle manually:

1. Publish two entries that share a referenced concept, one asset, and a model. Run the example
   and confirm scoped search and the document, graph, and vector records.
2. Edit an entry, asset metadata, and the model; rerun and confirm new content replaces old text.
3. Unpublish/delete one entry and the asset; rerun and confirm their obsolete content disappears
   while the surviving entry and its shared concept remain searchable.
4. Republish the same entry ID, rerun, then rerun with no changes. Confirm restored content and
   no duplicates.
5. Narrow selected types/locales and disable assets under the same source and resource names;
   verify removals. Expand again, and verify that another source's selection remains intact.
6. Repeat in a non-master environment and, if applicable, the EU Delivery region.

Record actual results separately from offline tests. Do not run this lifecycle against production
content or share the Delivery token in logs or reports.

## References

- [Contentful Sync interface](https://www.contentful.com/developers/docs/references/content-delivery-api/synchronization/)
- [Content types pagination](https://www.contentful.com/developers/docs/references/content-delivery-api/content-types/get-the-content-model-of-a-space/)
- [Locales inventory](https://www.contentful.com/developers/docs/references/content-delivery-api/locales/get-all-locales-of-a-space/)
- [EU web app and API regions](https://www.contentful.com/developers/docs/platform/eu-data-residency/)
- [Official JavaScript Sync implementation](https://github.com/contentful/contentful.js/blob/master/lib/paged-sync.ts)
- [dlt merge loading](https://dlthub.com/docs/general-usage/merge-loading)
- [Issue #4787](https://github.com/topoteretes/cognee/issues/4787)
