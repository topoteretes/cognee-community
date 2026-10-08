# cognee-community-connector-contentful

A Contentful Delivery connector for [Cognee](https://github.com/topoteretes/cognee).
Import published entries, asset metadata, and content models as searchable documents,
then reconcile edits and deletions on subsequent runs using Contentful's native Sync interface.

## Install and configure

Requires Python 3.11–3.13, Cognee `>=1.6.3,<1.7`, dlt `>=1.30,<1.31`, and
httpx `>=0.28,<0.29`. The lockfile retains Cognee `1.6.3` and dlt `1.30.0` for
reproducible development. Cognee must provide document synchronization and pipeline
scoping; the connector checks this contract before extracting content.

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
Set the following before running the example:

```bash
export CONTENTFUL_DELIVERY_TOKEN="your-delivery-token"
export CONTENTFUL_SPACE_ID="your-space-id"
export CONTENTFUL_ENVIRONMENT_ID="master"  # optional; defaults to master
export LLM_API_KEY="your-model-provider-key"
uv run python examples/example.py
```

Use Cognee's usual model/embedding configuration for providers other than its defaults.
The example imports nothing from Cognee and performs no synchronization when the
Contentful space or token is missing. Running it with credentials reads your published
content and submits document text to your configured Cognee model provider.

## Usage

```python
import cognee
from cognee_community_connector_contentful import contentful_source

await cognee.remember(
    contentful_source(content_type_ids=["product"], locales=["en-US"]),
    dataset_name="contentful_products",
    primary_key="id",
    write_disposition="merge",
    run_in_background=False,
    max_rows_per_table=0,
    self_improvement=False,
)

answer = await cognee.search(
    query_text="Which products are suitable for outdoor use?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["contentful_products"],
)
```

The factory exposes:

```python
contentful_source(
    space_id=None,
    *,
    token=None,
    environment=None,
    content_type_ids=None,
    locales=None,
    include_assets=True,
    source_id="default",
    host="cdn.contentful.com",
    client=None,
)
```

| Argument | Behavior |
|---|---|
| `space_id`, `token`, `environment` | Fall back to `CONTENTFUL_SPACE_ID`, `CONTENTFUL_DELIVERY_TOKEN`, and `CONTENTFUL_ENVIRONMENT_ID`; environment defaults to `master`. |
| `content_type_ids` | Select entry types and their model documents. Omitted means all types; an empty selection is invalid. |
| `locales` | Select exact locale keys. Omitted means all locales; an empty selection is invalid. There is no implicit locale fallback. |
| `include_assets` | Include all published asset metadata by default; binaries are never downloaded. |
| `source_id` | Stable logical source name, default `default`; use distinct names for independently managed selections. |
| `host` | Either `cdn.contentful.com` or `cdn.eu.contentful.com`. |
| `client` | Optional synchronous `httpx.Client` for testing or custom transport; the caller owns its lifecycle. |

Entry documents preserve field IDs, locale labels, structured rich text, linked entry/asset
IDs, and provenance. Links are preserved without recursively expanding their content.
Asset documents contain localized titles/descriptions, filenames, MIME types, HTTPS URLs,
sizes, and available dimensions. Models are separate documents so a schema edit does not
require reprocessing every unchanged entry. This connector reads published Delivery content;
drafts, Preview access, binary extraction, and built-in scheduling are outside its scope.

The example also accepts comma-separated `CONTENTFUL_CONTENT_TYPE_IDS` and
`CONTENTFUL_LOCALES`, `CONTENTFUL_INCLUDE_ASSETS=true|false`, `CONTENTFUL_SOURCE_ID`,
`CONTENTFUL_DELIVERY_HOST`, `CONTENTFUL_DATASET`, and `CONTENTFUL_QUERY`.

## Synchronization and source ownership

Each `remember()` invocation performs one synchronization. Schedule repeated invocations
externally if you need periodic updates. Use **foreground**, **merge**, `id` primary keys,
and **no row cap** exactly as shown above: replace or append dispositions are rejected
because they cannot preserve the incremental document contract.

The connector uses an unfiltered Contentful Sync stream and applies selections locally,
so upstream deletion events remain available. It follows every page before accepting the
terminal token. Stable document IDs distinguish space, environment, entity kind, and upstream
ID, preventing an entry and asset sharing an ID from colliding.

Source identity includes host, space, concrete environment, and `source_id`; it excludes
tokens and mutable selections. Rotating credentials preserves checkpoints. Changing types,
locales, or asset inclusion under the **same** `source_id` triggers a complete selection
refresh: selected documents are upserted and previously selected documents that disappeared
are deleted. The previous selection remains intact if the refresh fails.

Separate `source_id` values have independent staging tables, membership, checkpoints, and
cleanup scopes. Use the same dataset and source ID on subsequent runs. Changing `source_id`,
space, environment, or dataset creates a separate source; it does not retire the old source.
If intentionally replacing an entire source, retire its old dedicated dataset separately.

Contentful requests always use the configured environment route. Only official Delivery
hosts over HTTPS are accepted, and redirects are disabled. Valid continuation URLs contribute
their tokens, rather than controlling the next request's host or environment. Legacy same-space
continuations without an environment segment are supported; conflicting environments and unsafe
URLs abort synchronization. Tokens are sent in bearer headers and omitted from connector errors.

Repeated identical Sync events are coalesced. Conflicting entry/asset events for the same ID
are resolved with a same-environment Delivery lookup using all locales; unresolvable or
contradictory responses abort the cycle instead of relying on event order.

Content models are fetched separately with cursor pagination. Reconciliation requires two
consecutive matching complete inventories within three passes. Missing previously ingested
models are confirmed with individual Delivery lookups before deletion. Duplicate IDs,
repeated cursors, malformed pages, inconsistent inventories, and contradictory lookups fail
the cycle. This is a conservative consistency check, not a transactional Contentful snapshot.

## Failure recovery

Extraction completes and validates a cycle before producing rows or advancing managed state.
Request timeouts and transient retries are bounded; rate-limit retries honor Contentful's
reset header. Authentication errors, invalid Sync tokens, and incomplete model listings fail
the run without deliberately removing existing memory.

A successful dlt load is not the same as successful Cognee ingestion, cognification, or cleanup.
Keep dlt staging and pipeline state: retained tables allow the next unchanged incremental run
to reconcile loaded documents again and retry orphan cleanup. Cognee can log individual cleanup
failures without raising, so inspect your run logs and use the storage verification tests when
checking recovery. Do not delete staging to recover an ingestion failure.

After a dlt **load** failure, retry using the same dataset/source. A pending load package may
be recovered first without fetching fresh Contentful changes; invoke the pipeline again after
successful package recovery to perform a new extraction.

An invalid/expired Sync token does not trigger an automatic destructive reset. Preserve the
existing dataset and pipeline state while investigating. For a full rebuild, synchronize into a
fresh dedicated dataset, verify its search results and deletion lifecycle, then explicitly retire
the old dataset. Do not globally prune Cognee to repair this connector.

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
model-provider credentials. CI runs the full offline suite on Python 3.11–3.13 with the lockfile
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
5. Narrow selected types/locales and disable assets under the same source ID; verify removals.
   Expand again, and verify that another source ID's selection remains intact.
6. Repeat in a non-master environment and, if applicable, the EU Delivery region.

Record actual results separately from offline tests. Do not run this lifecycle against production
content or share the Delivery token in logs or reports.

## References

- [Contentful Sync interface](https://www.contentful.com/developers/docs/references/content-delivery-api/synchronization/)
- [Delivery pagination](https://www.contentful.com/developers/docs/references/content-delivery-api/overview/#cursor-pagination)
- [Official JavaScript Sync implementation](https://github.com/contentful/contentful.js/blob/master/lib/paged-sync.ts)
- [Issue #4787](https://github.com/topoteretes/cognee/issues/4787)
