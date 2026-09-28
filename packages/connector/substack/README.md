# cognee-community-connector-substack

A Substack data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync a newsletter's public RSS feed into memory — "ask this newsletter".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Posts are
rendered to plain text and ingested as **normal documents** (they flow through cognee's
cognify entity-extraction pipeline, not the deterministic dlt-row path), via cognee's
document-mode marker.

## Install

```bash
uv pip install cognee-community-connector-substack
# or, from this monorepo:
cd packages/connector/substack && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_substack import substack_source

await cognee.remember(
    substack_source(publication="example"),  # example.substack.com
    dataset_name="substack",
)

answer = await cognee.search(
    query_text="Summarize what this newsletter has covered recently.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["substack"],
)
```

Pass `publication="example"` for a `example.substack.com` feed, or `feed_url=...` directly
for a custom domain (e.g. `"https://news.example.com/feed"`). No authentication is
required — Substack feeds are public. See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
exactly the posts currently in the feed each run. Substack's RSS feed has no delete
signal of its own — an unpublished post simply disappears from the feed — so an absent
post falls out of the snapshot and cognee's existing `orphan_cleanup` removes it from
the graph and vector stores. Unchanged posts keep a stable content-hash `data_id`, so
they are not re-ingested or re-cognified.

**Known limitation:** a Substack RSS feed only lists the publication's most recent posts
(about 20-25 on substack.com). Because forget-on-delete works by "absent from the
current snapshot," an older post that is still published but has aged out of that
window is indistinguishable from one that was unpublished — it will be forgotten too.
This connector suits newsletters that publish less often than the feed's window fills
up; for a high-volume publication, treat it as a rolling window over recent posts
rather than a permanent archive.

**Paywalled posts:** Substack truncates `content:encoded` to the free preview for
subscriber-only posts. Such a post is still ingested (with whatever preview text the
feed provides) and its content is suffixed with a `[This post is truncated ...]` note
so a partial post is never mistaken for the whole thing.

## Testing

```bash
uv run pytest tests/
```

The tests use fixture RSS feed text (no live network) and cover feed parsing, HTML
rendering, the paywall-truncation marker, and full-snapshot forget-on-delete (edit /
removal on re-sync). They require a cognee build that includes document-mode (see the
pinned `cognee` version in `pyproject.toml`).
