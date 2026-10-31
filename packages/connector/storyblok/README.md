# cognee-community-connector-storyblok

A Storyblok data-source connector for [cognee](https://github.com/topoteretes/cognee): turn the
stories of a Storyblok space (product pages, docs, blog posts, help articles) into memory, so
an agent can answer from the content you publish.

**Status: work in progress.** This draft sets out the package and the design; the
implementation and tests follow in this PR.

## Planned design

- **Source:** the Storyblok Content Delivery API v2 (`/v2/cdn/stories`), authenticated with a
  space access token. Read-only. The region (EU, US, CA, AP, CN) picks the API host.
- **Documents:** one per story (`story:<id>`): name, full slug, content type, tags, publish
  dates and the story's text, flattened from its nested blocks and rich text fields. The URL
  points at the story in the Storyblok app.
- **Published or draft:** published content by default; `version="draft"` (with a preview
  token) syncs drafts too.
- **Incremental:** stories are listed 100 per page with `published_at_gt` from the last run,
  using the space's cache version (`cv`) so unchanged runs stay cheap; a story is emitted
  again only when its rendered document changes.
- **Forget-on-delete:** the complete list of story ids (`/v2/cdn/links`) is compared with what
  was synced; an unpublished or deleted story is emitted as a hard-delete tombstone. An empty
  listing forgets nothing until the next run confirms it.
- **Bounded runs:** 429 and 5xx are retried with `Retry-After`; a long rate limit or a spent
  request budget stops the run cleanly, reported in `cognee_sync_stats`.
- **Selection:** `starts_with` (a folder), `content_types` and `with_tag` narrow the sync.

Closes topoteretes/cognee#6143.
