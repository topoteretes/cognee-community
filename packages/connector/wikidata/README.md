# cognee-community-connector-wikidata

A Wikidata data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync Wikidata items (or items from any Wikibase, such as a self-hosted Wikibase or
Wikibase.cloud) into memory, "ask Wikidata".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each item becomes a
**readable document** (label, description, aliases, and its statements with every
property, item value and unit resolved to a label) that flows through cognee's normal
cognify entity extraction, via cognee's document-mode marker. Sync is **incremental**
(driven by each item's revision id) and **forgets on delete and merge**.

## Requirements

- Python 3.10+ and cognee **1.6.3** or newer, below 2 (document-mode with per-row node sets).
- No account or API key: Wikidata is public and its data is CC0.

## Install

```bash
uv pip install cognee-community-connector-wikidata
# or, from this monorepo:
cd packages/connector/wikidata && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_wikidata import wikidata_source

await cognee.remember(
    wikidata_source(
        items=["Q42"],  # Douglas Adams
        expand_properties=["P800"],  # ... and his notable works
        user_agent="my-app/1.0 (me@example.com)",
    ),
    dataset_name="wikidata",
    primary_key="id",
    write_disposition="merge",  # REQUIRED: incremental upsert by item id
    max_rows_per_table=0,  # read back every synced item, not the first 50
)

answer = await cognee.recall("Which works is Douglas Adams known for?", datasets=["wikidata"])
```

Run it again with the same dataset to sync only what changed.

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`,
> which would drop everything an incremental run did not touch.

### Choosing what to ingest

Combine any of these; the union is synced, capped at `max_items` (default 500).

| Argument | Selects |
| --- | --- |
| `items=["Q42", "Q1"]` | these items (ids, entity URIs such as `http://www.wikidata.org/entity/Q42`, or item page URLs) |
| `expand_properties=["P800"]` + `expand_depth=1` | the items those items point at through these properties, up to `expand_depth` hops |
| `sparql="SELECT ?item WHERE { ... } LIMIT 200"` | the items a [Wikidata Query Service](https://query.wikidata.org/) query returns in `?item` (`sparql_variable` to rename) |

Expansion only follows the properties you list, and the selection never grows past
`max_items`, so a seed such as `Q5` (human) cannot pull in millions of items. The query
service stops queries after 60 seconds: keep queries `LIMIT`-ed.

### Options

| Argument | Default | Meaning |
| --- | --- | --- |
| `language` | `"en"` | language of labels, descriptions, aliases and text values |
| `include_external_ids` | `False` | also render external-identifier statements (VIAF, ISNI, …) |
| `sitelinks` | `["<language>wiki"]` | site ids whose links are kept (`[]` for none) |
| `full_sync` | `False` | re-fetch and re-render every selected item on this run |
| `api_url` | Wikidata | another Wikibase's `api.php` |
| `sparql_url` | WDQS | SPARQL endpoint; needed for `sparql` on another Wikibase |
| `user_agent` | generic | User-Agent; Wikimedia asks for contact details |
| `maxlag` | `5` | `maxlag` sent with every Action API request; "too busy" answers are retried |
| `resource_name` | from `api_url` | staging table and state key; one per Wikibase sharing a dataset |
| `check_active` | none | host authorization checkpoint, called while rows are produced |
| `service` | none | a pre-built `httpx.Client` (alias: `http_client`) |

The source reads no environment variables; `examples/example.py` shows reading
`WIKIDATA_API_URL` and `WIKIDATA_USER_AGENT` and passing them in.

## What a document contains

One document per item, keyed by its id (`Q42`):

```
# Douglas Adams (Q42)
British science fiction writer and humorist (1952–2001)

## Statements
- instance of: human
- occupation: novelist (start time: 1979)
- notable work: The Hitchhiker's Guide to the Galaxy
- date of birth: 1952-03-11
- place of burial: Highgate Cemetery (coordinate location: 51.566, -0.145)
- country of citizenship: United Kingdom
...

## Sitelinks
- enwiki: Douglas Adams (https://en.wikipedia.org/wiki/Douglas_Adams)
```

- **Labels everywhere.** Properties, item values, qualifiers and units are resolved to
  labels in `language`, in batches of 50, so a document reads as prose instead of ids.
  Labels are requested with language fallback: Wikidata now stores many labels only
  under `mul` (one label for all languages), and a plain `languages=en` request returns
  **no label at all** for items such as Douglas Adams.
- **Dates at their precision.** `+1979-00-00` with year precision is `1979`, not
  `1979-00-00`; decades, centuries, BCE dates and Julian-calendar dates are spelled out,
  so TEMPORAL search can use them. Qualifiers such as start and end time are kept.
- **Signal over noise.** External identifiers are left out by default: on Douglas Adams
  they are 274 of 354 statements. Deprecated statements (known to be wrong) are dropped,
  preferred ones listed first. When a text is given in many languages (a caption, a
  title), only the one in `language` is kept, if there is one.
- **Sitelinks** tie items to their Wikipedia articles, so items line up with pages
  ingested by the MediaWiki connector.

Every item carries the `wikidata:<server>` node set (e.g. `wikidata:www.wikidata.org`), so
`recall` can be scoped to it when several sources share a dataset.

## How sync and forget-on-delete work

1. **Every run** resolves the selection and asks `wbgetentities` for each item's
   `lastrevid`, 50 ids per request. Only items whose revision moved (or that are new)
   are fetched in full and re-rendered. An unchanged selection of 500 items costs about
   ten requests and no rendering. Expansion reuses the links stored for unchanged items.
2. **Deleted items** come back `missing` and are emitted as `_deleted` tombstones. dlt
   removes them on `merge`, and cognee's `orphan_cleanup` deletes them from the graph,
   vector and relational stores.
3. **Merged items** come back as redirects to the item they were merged into: the old id
   is tombstoned and the target is synced in its place.
4. **Items that left the selection** (a seed removed, a link removed upstream, a query
   result gone) are tombstoned too.
5. **Unknown seeds.** A seed id that does not exist (deleted, or never assigned: a typo)
   raises `WikidataScopeError` when it is first configured, instead of quietly syncing
   less. A seed that was synced and deleted later is forgotten as usual. Ids that never
   existed also make Wikidata fail a whole `wbgetentities` request (`no-such-entity`);
   the client drops that id and retries the rest of the batch.
6. A selection that comes back **empty** while items were synced before deletes nothing
   and leaves the sync state untouched (`failed_empty_scope`), so a broken query or an
   outage cannot wipe the dataset. When the API itself confirms every synced item is
   gone (deleted or merged), the deletions are real and go ahead.

A change of `language`, `include_external_ids`, `sitelinks` or the selection re-renders
everything once. Every API error aborts the run before its state is saved, so the next run
retries. A service that keeps rate-limiting the run (HTTP 429 or `maxlag` past the retries,
or a `Retry-After` over 60 seconds) stops it cleanly instead: what was synced stays, the
state is not advanced, and the next run resumes.

### Sync stats

After a run, `source.cognee_sync_stats` holds plain int counters: `items_selected`,
`items_changed`, `items_unchanged`, `deleted`, `redirected`, `truncated`, `requests`,
`full_sync` (with `full_sync_<reason>` for `initial`, `requested`, `render_changed` or
`selection_changed`) and `failed` (with `failed_rate_limit`, `failed_auth` or
`failed_empty_scope`).

### Limitations

- A label change on a *referenced* item (say, a renamed occupation) does not edit the
  items that point at it; their documents pick up the new label when they are next
  edited, or with `full_sync=True`.
- Lexemes (`L…`) and other entity types are not supported; items and properties are.
- References (sources of statements) are not rendered.

Wikidata content is public and CC0. On a private Wikibase, the documents hold whatever the
selected items contain, including any personal data recorded in their statements.

## API etiquette

Requests are sequential, batch 50 ids, send `maxlag` and a descriptive User-Agent, and
back off on `maxlag`, HTTP 429 and 5xx, honouring `Retry-After` up to 60 seconds. Set `user_agent` to
something with your contact details, as the
[Wikimedia User-Agent policy](https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy)
asks.

## Example

`examples/example.py` syncs Douglas Adams and his notable works and asks a question:

```bash
export LLM_API_KEY="sk-..."
uv run python examples/example.py
```

## Testing

```bash
uv run pytest tests/
```

The tests mock the Wikibase API (no network), using response shapes recorded from live
Wikidata, and cover rendering (time precision, units, qualifiers, `mul` label fallback,
language filtering), expansion and `max_items`, SPARQL selection, the revision cursor,
deletions, merges, never-assigned ids, the empty-selection guard, batching, retries, and
a real `dlt` merge that drops a tombstoned row. An end-to-end test runs the source through
`cognee.add` + `cognify` (LLM and embeddings mocked) and checks that deleting an item
upstream removes its entity from the graph.
