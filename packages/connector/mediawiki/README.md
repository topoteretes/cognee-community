# cognee-community-connector-mediawiki

A MediaWiki data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync any MediaWiki (Wikipedia, Fandom, or your company's internal wiki) into memory,
"ask my wiki".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each wiki page becomes a
**document** that flows through cognee's normal cognify entity extraction, via cognee's
document-mode marker. Sync is **incremental** (driven by the wiki's `recentchanges` feed)
and **forgets on delete**.

## Requirements

- Python 3.10+ and cognee **1.6.3** or newer, below 2 (document-mode with per-row node sets).
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
| `categories=["Physics"]` | the direct member pages of these categories (prefix optional, in English or the wiki's language: `Kategorie:Physik` works on a German wiki) |
| `titles=["Albert Einstein"]` | these pages; redirects are followed, so a renamed page stays selected |

A category or title that does not exist (a typo) raises `MediaWikiScopeError` instead of
quietly syncing nothing. A category counts as existing when it has a page *or* members, as
many wikis have categories without a description page. Only names added to the
configuration are checked, so a page that was synced and deleted later is forgotten as
usual.

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
| `full_sync` | `False` | force a full enumeration and re-render every page on this run |
| `username` / `password` | none | bot password for a private wiki |
| `user_agent` | generic | User-Agent; Wikimedia asks for contact details |
| `maxlag` | `5` | `maxlag` sent with every request; "too busy" answers are retried |
| `resource_name` | from `api_url` | staging table and state key; unique per wiki so several wikis can share a dataset |
| `check_active` | none | host authorization checkpoint, called while rows are produced |
| `service` | none | a pre-built `httpx.Client` |

The source reads no environment variables; `examples/example.py` shows reading
`MEDIAWIKI_API_URL`, `MEDIAWIKI_USER_AGENT`, `MEDIAWIKI_USERNAME` and `MEDIAWIKI_PASSWORD`
and passing them in.

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

**Privacy:** the recent-edits section puts editor names (the IP address, for anonymous
editors) and edit summaries into memory. Hiding them later with revision deletion does
not update a document until its page is edited again or a full sync runs. Pass
`revision_history=0` to keep editors and summaries out of memory entirely.

Every page carries the `mediawiki:<server>` node set (e.g. `mediawiki:en.wikipedia.org`),
so `recall` can be scoped to one wiki when several share a dataset.

## How sync and forget-on-delete work

1. **First run.** The server time is captured first, then the scope is enumerated and every
   page is rendered.
2. **Later runs.** What is checked depends on how pages were selected:
   - **`titles` and `categories`** are compared against listings the run reads anyway:
     the titles are resolved every run (to follow renames) and the category member
     lists are diffed every run, both with each page's revision, title and `touched`
     timestamp. Only pages that differ are fetched, so an unchanged 10,000 page
     category costs about 20 listing requests and re-syncing one Wikipedia article
     takes 3. A page that joins or leaves a category, even through a template, is
     caught too.
   - **`namespaces`** are covered by the change feed: `list=recentchanges` is replayed
     from the previous run's server time minus `overlap_seconds` (the feed can receive
     entries slightly out of timestamp order) and filtered to the selected namespaces
     (`rcnamespace`). A move is logged under its *source* namespace, so moves and history
     merges are also read from the move and merge logs, unfiltered: a draft moved into
     the articles is still caught. Every page an entry names (its id, its title, a
     move's target, a history merge's other page) is re-checked in batches of 50.
     Replaying an entry twice therefore costs a lookup, never a duplicate document.

   A page whose revision, title or `touched` timestamp moved is re-rendered. `touched`
   also moves on purges, cache updates and link changes, not only when a template
   changed, so a re-render that produces the same text is not ingested again (a content
   hash is kept per page); it only costs the rendering requests.
3. **Deletions.** A page the re-check reports missing (deleted), or that left the scope
   (moved out of a namespace, taken out of a category, turned into a redirect), is emitted
   as a `_deleted` tombstone. dlt removes it on `merge`, and cognee's `orphan_cleanup`
   deletes it from the graph, vector and relational stores. A restored page comes back.
4. **Reconciliation.** `recentchanges` is pruned after `$wgRCMaxAge` (90 days by default,
   30 on Wikimedia wikis). If the oldest entry still kept is newer than the replay window,
   entries may be lost, so the run does a full enumeration and diffs it against what was
   synced before. The same happens when the scope or rendering settings change, every
   `reconcile_after_days` (a safety net for entries delayed beyond the overlap), and with
   `full_sync=True`. The full enumeration compares `touched` too, so the periodic
   reconcile re-renders pages a template changed; `full_sync=True` re-renders and
   re-ingests every page.
5. **Empty scopes.** When a full enumeration finds no pages while pages were synced
   before, it depends on how they were selected. For `titles` and `categories` the
   emptiness is confirmed by the API itself (the titles are reported missing, the
   categories have no members), so the old pages are forgotten. So are they when the
   scope was just changed (switching to an empty category forgets the old pages). A
   namespace that lists nothing could be an outage or a permissions change, so the first
   such run deletes nothing and leaves the cursor where it is (`failed_empty_scope`), and
   a second consecutive empty listing is believed and forgets them.

Every API error aborts the run before its state is saved, so the next run retries the same
window. A wiki that keeps rate-limiting the run (HTTP 429 or `maxlag` past the retries, or a
`Retry-After` over 60 seconds) stops it cleanly instead: what was synced stays, and the pages
already compared, rendered or forgotten are recorded, so the next run continues from there
instead of starting over; the cursor is not advanced until a run completes.

**Upgrading** from a version that did not track `touched`: synced pages without it are
compared on revision and title only, so the first run after the upgrade does not re-render
everything.

### Sync stats

After a run, `source.cognee_sync_stats` holds plain int counters, all present on every run:

| Counter | Meaning |
| --- | --- |
| `pages_scanned` | pages whose metadata was compared this run (listed or named by the feed) |
| `pages_changed` / `pages_unchanged` / `deleted` | what happened to them |
| `pages_rendered_unchanged` | re-renders that produced the same text, so were not ingested |
| `feed_read`, `changes_replayed` | whether the change feed was read (0 for title and category scopes) and how many entries it returned |
| `requests` | API requests made |
| `full_sync` | 1 for a full sync, with `full_sync_<reason>`: `initial`, `requested`, `scope_changed`, `render_changed`, `periodic` or `retention_gap` |
| `failed` | 1 when the run failed or stopped early, with `failed_<reason>`: `rate_limit`, `empty_scope` (both finish the run), or `auth`, `scope`, `api`, `error` (these also raise) |

### Limitations

- A template edit changes the rendered text of pages that use it without editing them;
  pages selected by `titles` or `categories` pick that up on the next run (their
  `touched` timestamp is re-checked every run); pages selected through `namespaces` pick
  it up on the next periodic reconcile (`reconcile_after_days`) or with `full_sync=True`.
- `categories` selects direct members, not subcategories.

## Private wikis

Create a bot password at `Special:BotPasswords` (grant: *basic rights*), then pass
`username="YourUser@botname"` and `password=...`. The connector logs in once per run and
only reads. For SSO-protected wikis, pass an `httpx.Client` that already carries the
session as `service=`.

## API etiquette

Requests are sequential, batch up to 50 pages, send `maxlag` and a descriptive
User-Agent, and back off on `maxlag`, HTTP 429 and 5xx, honouring `Retry-After` up to
60 seconds. Set
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
restores, scope changes, the retention-gap fallback, the empty-enumeration guard (which
must not move the cursor), template re-renders caught through `touched`, the
namespace-filtered feed plus the move log, typo'd categories and titles, category order,
a stalled continuation, retries (including a `Retry-After` past the cap), non-JSON answers,
login errors, and real `dlt` merges: one that drops a tombstoned row, and two wikis with
their own `resource_name` sharing a dataset. An end-to-end test runs the source
through `cognee.add` + `cognify` (LLM and embeddings mocked) and checks that deleting a
page upstream removes its entity from the graph.
