# cognee-community-connector-discourse

A [Discourse](https://www.discourse.org/) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync forum threads into memory, so an agent
can answer from your community's questions, answers and announcements.

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Each
**topic is one document**: the opening post and every reply, fetched as markdown from
`/raw/{topic_id}`, so a thread is remembered as a whole conversation. Topics are ingested as
**normal documents** (they flow through cognee's cognify entity-extraction pipeline, not the
deterministic dlt-row path), via cognee's document-mode marker.

## Requirements

Requires a cognee release with "document-mode" (`DOCUMENT_SOURCE_ATTR` and the
`resolve_dlt_sources` routing that reads it), first shipped in cognee 1.4.0.

## Install

```bash
uv pip install cognee-community-connector-discourse
# or, from this monorepo:
cd packages/connector/discourse && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_discourse import discourse_source

# meta.discourse.org's "comparison" category: ~12 threads comparing Discourse to other tools.
await cognee.remember(
    discourse_source("https://meta.discourse.org", category_ids=[63], request_delay=0.5),
    dataset_name="discourse",
)

answer = await cognee.search(
    query_text="How does Discourse compare to Slack for community support?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["discourse"],
)
```

See [`examples/example.py`](examples/example.py) for the full flow.

## Options

| Argument | Default | Description |
| --- | --- | --- |
| `base_url` | `DISCOURSE_BASE_URL` | Forum URL, e.g. `https://meta.discourse.org`. |
| `api_key`, `api_username` | `DISCOURSE_API_KEY`, `DISCOURSE_API_USERNAME` | Only for private forums. Pass both or neither. |
| `category_ids` | `None` | Ingest topics in these categories. |
| `tags` | `None` | Ingest topics with these tags. Combined with `category_ids` as a union; a topic matching both is ingested once. With neither, the whole forum (`/latest`) is ingested. |
| `include_subcategories` | `True` | Discourse lists a parent category **together with all its subcategories**, which can be far larger than the parent alone. `False` restricts `category_ids` to the categories themselves. |
| `request_delay` | `0.0` | Seconds to wait before each request. Use `0.5` or more on forums you don't run. |
| `incremental` | `True` | Re-fetch only topics that changed since the last run (see below). `False` fetches every topic on every run. |
| `full_refresh_every` | `7` | Re-fetch every topic on every Nth run, to pick up edits to existing posts. `None` disables it. |
| `force_full_refresh` | `False` | Re-fetch every topic on this run. |

**Check the size of your scope first.** Unscoped, the connector ingests the whole forum, and
a category that looks small can contain hundreds of topics through its subcategories. Each
new topic costs one request to fetch (plus one per 100 posts), and cognify then runs an LLM
over it.

## Private forums

Create an API key in the Discourse admin panel (**Admin → API → New API Key**), scoped to
read access, and pass it with the username it should act as:

```python
discourse_source(
    "https://forum.example.com",
    api_key=os.environ["DISCOURSE_API_KEY"],
    api_username="system",
)
```

The connector only ingests regular topics; private messages are skipped even if the key can
see them.

## How sync and forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with exactly
the topics currently listed on each run. A deleted (or no longer visible) topic drops out of
Discourse's listings, so it is absent from the snapshot and cognee's existing
`orphan_cleanup` removes it from the graph and vector stores. Unchanged topics keep a stable
content-hash `data_id`, so they are not re-ingested or re-cognified.

A topic deleted between listing and fetching (`403`/`404` on `/raw`) is skipped, which is the
correct outcome. **Any other error aborts the run**, leaving memory untouched, rather than
letting a partial snapshot forget live topics. Rate limits (`429`, honouring `Retry-After`),
server errors and network failures are retried with backoff first.

## How incremental sync works

Fetching every topic body on every run is the expensive part, but skipping topics would
shrink the snapshot and forget live ones. So the connector still **lists and yields every
topic** each run (listing is cheap: 30 topics per request, metadata only), and only skips
the body fetch:

- Each topic's signature `(bumped_at, posts_count, highest_post_number)` is compared with the
  previous run. A new reply, a deleted reply, or a bump changes it, and the topic is
  re-fetched.
- Unchanged topics reuse the markdown cached from the previous run. Title changes still
  apply immediately, since the title comes from the listing.
- Deleted topics are dropped from the cache, and an aborted run commits nothing.

The cache lives in dlt **source** state (dlt resets *resource* state on every run of a
`replace` resource). cognee runs every dlt source through one shared pipeline, so the cache
is keyed by forum URL + scope, and different forums or scopes never share cached content.

**Known limitation: edits to existing posts.** Editing a post changes none of the signature
fields, so incremental runs keep the cached text until the periodic full refresh
(`full_refresh_every`, every 7th run by default) or a run with `force_full_refresh=True`.
Lower `full_refresh_every` if edits matter more than request volume.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake Discourse client and `httpx.MockTransport` (no network or live forum)
and cover listing pagination and scoping, `/raw` paging, the retry policy, auth
configuration, full-snapshot forget-on-delete through real dlt pipelines, and incremental
sync: unchanged topics are not re-fetched, changed ones are, the full-refresh cadence, a
failed run keeping the previous cache, and per-scope cache isolation.
