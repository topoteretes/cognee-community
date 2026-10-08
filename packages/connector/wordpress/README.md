# cognee-community-connector-wordpress

A WordPress data-source connector for [cognee](https://github.com/topoteretes/cognee): sync a
WordPress site (self-hosted or WordPress.com) into memory, including posts, pages, custom post
types and their comments, so you can ask questions about your site.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each item becomes a **document**
that flows through cognee's normal cognify entity extraction, via cognee's document-mode marker.
Sync is **incremental** (`modified_after`, backed by a lightweight id sweep) and **forgets on
delete**.

## Requirements

- cognee **1.6.3** or newer (document mode with per-row node sets).
- A site with the REST API enabled (the default since WordPress 4.7; `modified_after` needs
  WordPress 5.7+).

## Install

```bash
uv pip install cognee-community-connector-wordpress
# or, from this monorepo:
cd packages/connector/wordpress && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_wordpress import wordpress_source

await cognee.remember(
    wordpress_source(
        "https://blog.example.com",
        username="editor",  # optional: only for private content
        application_password="abcd efgh ijkl mnop qrst uvwx",
    ),
    dataset_name="blog",
    primary_key="id",
    write_disposition="merge",  # REQUIRED: incremental upsert by item id
    max_rows_per_table=0,  # read back every synced item, not the first 50
)

answer = await cognee.recall("What did we announce about the 2.0 release?", datasets=["blog"])
```

Run it again with the same dataset to sync only what changed.

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`, which
> would drop everything an incremental run did not touch.

## Setup

1. **Public content** needs nothing but the site URL.
2. **Private content** (`statuses=["private", "publish"]`, drafts, ...) needs an **Application
   Password**: in the WordPress admin go to *Users → Profile → Application Passwords*, enter a
   name (e.g. `cognee`) and click *Add New Application Password*. Pass the user name and the
   generated password (spaces are fine) as `username=` / `application_password=`, or set
   `WORDPRESS_USERNAME` / `WORDPRESS_APP_PASSWORD`. WordPress only accepts Application Passwords
   over HTTPS (or on a site whose `WP_ENVIRONMENT_TYPE` is `local`). The credentials are
   checked once at the start of every run, so a rejected password fails with
   `WordPressAuthError` instead of silently syncing public content only.
3. **WordPress.com** sites (`*.wordpress.com` or a custom domain hosted there) are read through
   `public-api.wordpress.com` automatically. Application Passwords are a self-hosted feature, so
   WordPress.com sites sync their public content.

The site URL falls back to `WORDPRESS_URL`.

### Choosing what to ingest

| Argument | Default | Meaning |
| --- | --- | --- |
| `post_types` | `["post", "page"]` | post type slugs; custom types (`product`, `event`, `docs`, ...) work when the plugin exposes them over REST. An unknown slug fails and lists the site's types |
| `statuses` | `["publish"]` | anything else (`private`, `draft`, `future`, ...) needs an Application Password |
| `categories` | none | category slugs; types that have categories (posts) are narrowed to them, other types (pages) are synced in full |
| `tags` | none | tag slugs, applied the same way |
| `include_comments` | `True` | append each item's approved comments to its document |

Password-protected posts are never ingested. A category or tag slug that does not exist fails
the run instead of syncing nothing.

### Other options

| Argument | Default | Meaning |
| --- | --- | --- |
| `overlap_seconds` | `300` | how far the `modified_after` feed is replayed before the last run |
| `reconcile_after_days` | `7` | re-render every item at least this often (`None` to disable) |
| `full_sync` | `False` | re-render every item on this run |
| `user_agent` | env / generic | `User-Agent` header (`WORDPRESS_USER_AGENT`) |
| `resource_name` | from the URL | staging table and state key; unique per site, so several sites can share a dataset |

## What a document contains

One document per item, keyed by the post id (unique across every post type on a site):

- the title and link,
- the type, author, published and updated dates (UTC),
- its taxonomy terms: categories, tags, and custom taxonomies,
- the content, reduced from the rendered HTML to readable markdown-style text: headings,
  ordered/unordered (nested) lists, quotes, fenced code blocks, table rows and image alt text are
  kept; scripts, styles, embeds and share widgets are dropped,
- the approved comments (author, date, text; replies marked).

Every item carries the `wordpress:<host>` node set (e.g. `wordpress:blog.example.com`), so
`recall` can be scoped to one site when several share a dataset.

## How sync and forget-on-delete work

1. **First run.** The site's own clock (the `Date` header of the first response) is recorded,
   then every item in scope is fetched (100 per request) and rendered.
2. **Changes: `modified_after`.** Later runs ask each post type for items modified after the
   previous run's server time minus `overlap_seconds`. The timestamp is always sent **with an
   explicit UTC offset**: WordPress compares it with the site-local `post_modified` column, so a
   naive timestamp would be read in the site's timezone and skip hours of changes. An item is
   re-rendered only when its `modified_gmt` or its comments changed, so replaying the overlap is
   harmless.
3. **Deletions and blind spots: the id sweep.** `modified_after` never reports deletions: a
   trashed, unpublished, permanently deleted or out-of-category post simply stops appearing
   (anonymously, trash is not even visible). And a post scheduled in the editor goes live without
   its modification time moving, so `modified_after` never returns it. Every run therefore also
   lists ids and modification times only (`_fields=id,modified_gmt`). Ids the sweep finds that
   the feed missed are fetched by id. Ids that vanished are **asked for again by id** before they
   are forgotten (offset pagination can skip an item while the site changes underneath), and
   only confirmed ones become `_deleted` tombstones. dlt removes them on `merge`, and cognee's
   `orphan_cleanup` deletes them from the graph, vector and relational stores. A restored or
   republished item comes back.
4. **Comments.** An id-only sweep of the approved comments **of the items in scope** (never
   site-wide) re-renders the items whose comment set changed: new, deleted or unapproved
   comments.
5. **Safety.** A sweep that returns no items while items were synced before deletes nothing (a
   revoked password or a disabled REST route cannot wipe the dataset). Every API error aborts the
   run before its state is saved, so the next run retries. HTTP 429, 5xx and network errors are
   retried, honouring `Retry-After`.

A scope or rendering change (different `post_types`, `statuses`, `categories`, `tags` or
`include_comments`) reconciles everything: items that left the scope are forgotten.

### Limitations

- Editing a comment's text keeps its id, so it is picked up when the item itself changes, on a
  full sync, or by the periodic re-check (`reconcile_after_days`).
- Sites that disable the REST API (some security plugins do) cannot be synced.

## Example

`examples/example.py` syncs the `documentation` category of
[wordpress.org/news](https://wordpress.org/news) (or, with `WORDPRESS_URL` set, your whole site)
and asks a question about it:

```bash
export LLM_API_KEY="sk-..."
uv run python examples/example.py
```

## Testing

```bash
uv run pytest tests/
```

The tests use a fake WordPress REST API behind `httpx.MockTransport` (no network) that
reproduces the behaviour of a live WordPress 6.9 site: site-local `modified_after`, offset
pagination, trash hidden from anonymous requests, scheduled posts that go live without a new
modification time, and comment queries that fail when they name a password-protected post. They
cover discovery, rendering, the cursor and the sweep, every deletion path, comments, filters,
private content, retries, and a real `dlt` merge that drops a tombstoned row. An end-to-end test
runs the source through `cognee.add` + `cognify` (LLM and embeddings mocked) and checks that
deleting a post upstream removes its entity from the graph.
