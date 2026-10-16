# cognee-community-connector-mediawiki

A MediaWiki data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync any MediaWiki (Wikipedia, Fandom, or your company's internal wiki) into memory,
"ask my wiki".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each wiki page becomes a
**document** that flows through cognee's normal cognify entity extraction, via cognee's
document-mode marker. Sync is **incremental** (driven by the wiki's `recentchanges` feed)
and **forgets on delete**.

## Requirements

- cognee **1.6.3** or newer (document-mode with per-row node sets).
- A wiki with the Action API enabled (`api.php`), which is the default on every MediaWiki.

## Install

```bash
uv pip install cognee-community-connector-mediawiki
# or, from this monorepo:
cd packages/connector/mediawiki && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_mediawiki import mediawiki_source

await cognee.remember(
    mediawiki_source(
        "https://en.wikipedia.org/w/api.php",
        categories=["Nobel laureates in Physics"],
        user_agent="my-app/1.0 (me@example.com)",
    ),
    dataset_name="wiki",
    primary_key="id",
    write_disposition="merge",  # REQUIRED: incremental upsert by page id
    max_rows_per_table=0,  # read back every synced page, not the first 50
)

answer = await cognee.recall(
    "Who won the Nobel Prize in Physics for the photoelectric effect?", datasets=["wiki"]
)
```

Run it again with the same dataset to sync only what changed.

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`,
> which would drop everything an incremental run did not touch.

### Choosing what to ingest

Pick any combination; the union is synced. With none of them, the main (article)
namespace is synced.

| Argument | Selects |
| --- | --- |
| `namespaces=[0, 4]` | every page in these namespaces (`0` articles, `4` project pages, …) |
| `categories=["Physics"]` | the direct member pages of these categories (`Category:` prefix optional) |
| `titles=["Albert Einstein"]` | these pages; redirects are followed, so a renamed page stays selected |

Redirect pages and code pages (CSS, JavaScript, JSON, Lua modules) are never ingested.
For a large public wiki such as Wikipedia, select categories or titles; a whole namespace
there is millions of pages.

### Options

| Argument | Default | Meaning |
| --- | --- | --- |
| `content_format` | `"auto"` | `"extracts"` (TextExtracts plain text), `"parse"` (rendered HTML reduced to text), or `"auto"`: extracts when the wiki has the extension |
| `revision_history` | `5` | recent edits (time, author, summary) listed in each document; `0` for none |
| `overlap_seconds` | `600` | how far the change feed is replayed before the last run |
| `reconcile_after_days` | `7` | run a full enumeration at least this often (`None` to disable) |
| `full_sync` | `False` | force a full enumeration on this run |
| `username` / `password` | env | bot password for a private wiki (`MEDIAWIKI_USERNAME` / `MEDIAWIKI_PASSWORD`) |
| `user_agent` | env / generic | User-Agent; Wikimedia asks for contact details (`MEDIAWIKI_USER_AGENT`) |
| `maxlag` | `5` | `maxlag` sent with every request; "too busy" answers are retried |
| `resource_name` | from `api_url` | staging table and state key; unique per wiki so several wikis can share a dataset |

`api_url` falls back to `MEDIAWIKI_API_URL`.

## What a document contains

One document per page, keyed by the wiki's **page id** (not its title, so a rename updates
the same document instead of forgetting one and adding another):

- the page title and URL,
- its visible categories,
- the page text, **rendered by the wiki itself**: templates, transclusions and parser
  functions are expanded server-side, so no wikitext is hand-parsed. On wikis with the
  TextExtracts extension (Wikipedia and most large wikis) the plain-text extract is used;
  elsewhere the `action=parse` HTML is reduced to text (styles, edit links, reference
  markers, navboxes and hidden elements dropped; headings, lists and table rows kept),
- the latest `revision_history` edits: timestamp, author, edit summary and revision id.
  Authors and summaries the wiki has hidden (revision deletion) are left out.

Every page carries the `mediawiki:<server>` node set (e.g. `mediawiki:en.wikipedia.org`),
so `recall` can be scoped to one wiki when several share a dataset.

## How sync and forget-on-delete work

1. **First run.** The server time is captured first, then the scope is enumerated and every
   page is rendered.
2. **Later runs.** `list=recentchanges` (edits, new pages and log events) is replayed from
   the previous run's server time minus `overlap_seconds`, because the feed can receive
   entries slightly out of timestamp order. Entries that cannot touch the synced pages
   (an edit to an unselected page elsewhere on the wiki) are skipped without a lookup.
   Every page a remaining entry names (its page id, its title, a move's target, a history
   merge's destination) is re-checked against the live wiki in batches of 50 and
   re-rendered only if its revision or title changed. Replaying an entry twice therefore
   costs a lookup, never a duplicate document. Selected categories have their member lists
   diffed every run, so a page that joins or leaves a category through a template edit is
   caught too.
3. **Deletions.** A page the re-check reports missing (deleted), or that left the scope
   (moved out of a namespace, taken out of a category, turned into a redirect), is emitted
   as a `_deleted` tombstone. dlt removes it on `merge`, and cognee's `orphan_cleanup`
   deletes it from the graph, vector and relational stores. A restored page comes back.
4. **Reconciliation.** `recentchanges` is pruned after `$wgRCMaxAge` (90 days by default,
   30 on Wikimedia wikis). If the oldest entry still kept is newer than the replay window,
   entries may be lost, so the run does a full enumeration and diffs it against what was
   synced before. The same happens when the scope or rendering settings change, every
   `reconcile_after_days` (a safety net for entries delayed beyond the overlap), and with
   `full_sync=True`. An enumeration that returns no pages
   while pages were synced before deletes nothing, so a typo or an outage cannot wipe the
   dataset.

Every API error aborts the run before its state is saved, so the next run retries the same
window.

### Limitations

- A template edit changes the rendered text of pages that use it without editing them;
  those pages are re-rendered when they are next edited or on a full sync
  (`full_sync=True`, or a scope / rendering change).
- `categories` selects direct members, not subcategories.

## Private wikis

Create a bot password at `Special:BotPasswords` (grant: *basic rights*), then pass
`username="YourUser@botname"` and `password=...` (or set `MEDIAWIKI_USERNAME` /
`MEDIAWIKI_PASSWORD`). The connector logs in once per run and only reads. For SSO-protected
wikis, pass an `httpx.Client` that already carries the session as `http_client=`.

## API etiquette

Requests are sequential, batch up to 50 pages, send `maxlag` and a descriptive
User-Agent, and back off on `maxlag`, HTTP 429 and 5xx, honouring `Retry-After`. Set
`user_agent` to something with your contact details when syncing a Wikimedia wiki.

## Example

`examples/example.py` syncs a few Wikipedia pages and asks a question about them:

```bash
export LLM_API_KEY="sk-..."
uv run python examples/example.py
```

## Testing

```bash
uv run pytest tests/
```

The tests mock the Action API (no network) and cover the ingest path, HTML/extract
rendering, the `recentchanges` cursor and its overlap window, edits, moves, deletions,
restores, scope changes, the retention-gap fallback, the empty-enumeration guard, retries,
and a real `dlt` merge that drops a tombstoned row. An end-to-end test runs the source
through `cognee.add` + `cognify` (LLM and embeddings mocked) and checks that deleting a
page upstream removes its entity from the graph.
