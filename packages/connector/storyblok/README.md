# cognee-community-connector-storyblok

A Storyblok data-source connector for [cognee](https://github.com/topoteretes/cognee): turn the
stories of a Storyblok space (product pages, docs, blog posts, help articles) into memory, so an
agent can answer from the content you publish.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every story becomes one document,
with the text of its nested blocks and rich text fields, that flows through cognee's normal
cognify entity extraction.

## Install

```bash
uv pip install cognee-community-connector-storyblok
# or, from this monorepo:
cd packages/connector/storyblok && uv sync
```

## Setup

1. Open your space in [Storyblok](https://app.storyblok.com).
2. Go to **Settings**, then **Access Tokens**, and copy a token: **Public** reads published
   content, **Preview** also reads drafts. A personal access token does not work here; the
   source reads through the Content Delivery API.
3. Note the space's region (`eu`, `us`, `ca`, `ap` or `cn`), shown in the space settings.
4. Pass them as `token=` and `region=`. The example reads them from `STORYBLOK_TOKEN` and
   `STORYBLOK_REGION`, together with your `LLM_API_KEY` like any other cognee run:

```bash
export STORYBLOK_TOKEN="..."
export STORYBLOK_REGION="eu"
```

## Usage

```python
import os

import cognee
from cognee_community_connector_storyblok import storyblok_source

await cognee.remember(
    storyblok_source(token=os.environ["STORYBLOK_TOKEN"], region="eu"),
    dataset_name="storyblok",
    primary_key="id",
    write_disposition="merge",  # required: the add pipeline defaults to "replace"
    max_rows_per_table=0,
)

print(await cognee.recall("What do our published pages say about pricing?"))
```

Run it again whenever you like: while nothing was published, unpublished or deleted a run
costs one request, only stories whose document changed are sent to cognee, and unpublished
or deleted stories are removed from memory.

### Options

| Argument | Default | What it does |
| --- | --- | --- |
| `token` | required | A space access token. Never read from the environment by the source itself. |
| `region` | `"eu"` | The space's region, which picks the API host. |
| `version` | `"published"` | `"draft"` also syncs unpublished changes (needs a preview token). |
| `starts_with` | none | Only stories under this path, for example `"blog/"`. |
| `content_type` | none | Only stories of this content type (component name). |
| `with_tag` | none | Only stories with this tag. |
| `language` | none | Only this language's translation of each story. |
| `full_resync` | `False` | Emit every story again. |
| `max_requests` | `1000` | Request budget of one run. |
| `resource_name` | derived | Staging table name, which scopes this sync's state and cleanup. |
| `check_active` | none | Host authorization checkpoint, called before every request and during retry waits. |

## What a document looks like

```text
Story: Pricing
Path: docs/pricing
Content type: page
Tags: billing, plans
First published: 2026-10-20 10:05 UTC
Last published: 2026-10-30 16:12 UTC

Pricing
Plans start at 10 EUR per seat.
Enterprise
Talk to sales for volume pricing.
```

The text comes from the story's content in field order: plain text fields, rich text
paragraphs, headings and list items, blocks nested in other blocks or embedded in rich text,
and an image's alt text and title. Block metadata (`component`, `_uid`, `_editable`), asset
files, links, URLs and ids are left out. The document links to the story in the Storyblok app.

Every title and content then goes through a scrubbing step (`redact.scrub`, the same as the
Terraform Cloud connector's) that redacts private keys, cloud and VCS tokens, JWTs, URL
credentials and secret-looking `key = value` pairs. Redaction counts land in
`cognee_sync_stats["redacted"]`.

## How the sync works

- **Cache version first.** Each run asks `GET /v2/cdn/spaces/me` for the space's cache version
  (`cv`), which Storyblok moves whenever something is published, unpublished or deleted. While
  syncing published content and that version has not moved since the last complete run, the run
  stops there: one request.
- **Then one listing.** Otherwise `GET /v2/cdn/stories` is read 100 stories a page with that
  `cv`, so the CDN answers fresh content. Drafts do not move the cache version, so
  `version="draft"` lists on every run.
- **Changed stories only.** Each story's rendered document is hashed; a story is emitted again
  only when that hash changes.
- **Forget-on-delete.** A story missing from a complete listing (unpublished, deleted, or
  outside a new selection) is removed from memory. A listing that comes back empty while
  stories were synced before forgets nothing until the next run confirms it, and a listing
  without its `stories` list, or with a story it cannot name, raises instead of forgetting
  anything.
- **One space per state.** The state remembers the space it was made with; a token for another
  space under the same `resource_name` raises instead of forgetting the first space's stories.
  Give each space its own `resource_name`.
- **The token stays out of logs.** The Content Delivery API takes its token as a query
  parameter, so the client adds it in the transport, below the layer httpx logs and puts in
  its errors.
- **A run is bounded.** 429 and 5xx are retried, honoring `Retry-After`. A `Retry-After` over
  60 seconds, a 429 that outlasts the retries, or more than five minutes of retry waits stops
  the run cleanly with `failed` and `failed_rate_limit`; a spent `max_requests` budget stops it
  with `failed` and `failed_budget`. A token rejected after the first page stops it with
  `failed` and `failed_auth`; a token rejected at the start raises. A run cut short forgets
  nothing and does not move the cache version, so the next run lists again. Any other error
  raises, so dlt keeps the previous state.

`cognee_sync_stats` always carries `scanned`, `emitted`, `unchanged`, `deleted`, `redacted`,
`failed`, `failed_auth`, `failed_budget` and `failed_rate_limit`.

## Privacy

Published stories are public by design, but a preview token also reads drafts. Nothing is
fetched until you construct a source and call `remember`, and anyone with read access to the
target dataset can read what was ingested.

## Development

```bash
cd packages/connector/storyblok
uv sync
uv run pytest tests/
```

The tests need no space or LLM key.
