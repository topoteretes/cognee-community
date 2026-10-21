# cognee-community-connector-backstage

A [Backstage](https://backstage.io) data-source connector for
[cognee](https://github.com/topoteretes/cognee). It turns your software catalog
(who owns which service, which APIs a service provides and consumes, which system
it belongs to, and who is on each team) into memory, so an agent can answer
"Who owns the billing service?" or "Which services consume the payments API?".

It syncs one document per catalog entity, with relations written as plain
sentences, plus opt-in TechDocs pages. Syncs are incremental, and entities
removed from the catalog are forgotten.

## Requirements

- Python 3.11–3.13
- `cognee>=1.6.2,<2`, `dlt[sqlalchemy]`, `requests`
- A Backstage backend that serves the catalog API (`/api/catalog`), and the
  TechDocs API if you sync docs

## Install

```bash
# Not published on PyPI yet; install from this monorepo:
cd packages/connector/backstage && uv sync
```

## Setup

1. Find your Backstage backend URL, usually the same origin as the frontend
   (e.g. `https://backstage.example.com`).
2. If your instance requires auth, add a static external-access token in
   `app-config.yaml` and give it read access to the catalog (and TechDocs):

   ```yaml
   backend:
     auth:
       externalAccess:
         - type: static
           options:
             token: ${COGNEE_BACKSTAGE_TOKEN}
             subject: cognee-connector
   ```

   Instances that allow guest reads need no token. The public demo at
   https://demo.backstage.io does, which makes it a quick way to try the connector.

3. Export the URL, the token if you have one, and your `LLM_API_KEY`:

```bash
export BACKSTAGE_BASE_URL=https://demo.backstage.io
export BACKSTAGE_TOKEN=...        # optional
export LLM_API_KEY=sk-...
```

## Usage

```python
import cognee
from cognee_community_connector_backstage import backstage_source

await cognee.remember(
    backstage_source(
        kinds=["Component", "API", "System", "Group", "User"],  # optional
        namespaces=["default"],  # optional; all namespaces by default
        techdocs=["component:default/backstage-demo"],  # optional
    ),
    dataset_name="backstage",
    write_disposition="merge",  # required, see below
    self_improvement=False,
)
```

See `examples/example.py` for a runnable version.

| Argument | Meaning | Default |
|---|---|---|
| `base_url` | The Backstage backend | `BACKSTAGE_BASE_URL` |
| `token` | Static external-access token, sent as `Authorization: Bearer` | `BACKSTAGE_TOKEN`, or none |
| `kinds` | Entity kinds to sync | Component, API, System, Domain, Resource, Group, User |
| `namespaces` | Namespaces to sync | all |
| `techdocs` | Entity refs (`kind:namespace/name`) whose TechDocs pages are synced | none |
| `annotations` | Annotation keys rendered into documents | a few safe keys |
| `check_active` | Host checkpoint, called before every request and before every row is published | none |
| `service` (alias `session`) | Pre-built `requests`-compatible session (tests, proxies); not closed by the source | one per run, closed afterwards |

After a run, `source.cognee_sync_stats` holds counts only: `scanned`, `skipped`
(unchanged), `changed`, `deleted`, `failed` (malformed entities), `techdocs_pages`,
`requests`, and `complete` (1 once the run finished).

## What a document looks like

```text
Backstage component queue-proxy.
Queue Proxy
Type: website
Lifecycle: production
Tags: go, website
queue-proxy is owned by the group Team B.
queue-proxy is part of the system podcast.
```

Each entity's description, type, lifecycle, tags, links and allowlisted
annotations come first, then one sentence per relation (`ownedBy`, `partOf`,
`providesApi`, `consumesApi`, `dependsOn`, `memberOf`, `hasMember`, and so on).
Relation targets are named by their title or display name when they are in the
synced catalog, so cognify links them to the same entities. Annotations are only
rendered when listed in `annotations=` (by default a few safe keys such as
`github.com/project-slug`), because most of them are internal URLs and
integration ids.

## How sync and forget-on-delete work

Pass `write_disposition="merge"` to `remember`: this source only emits documents
that changed and deletes by emitting `_deleted` tombstone rows, which only
`merge` applies.

- **Incremental sync.** Each run lists the selected entities through
  `/api/catalog/entities/by-query` (cursor paging, with the kind and namespace
  filters applied by Backstage) and renders them. A hash of each rendered
  document is kept in dlt state, so only documents that changed are emitted.
  Backstage has no change feed, and hashing the rendered text also catches an
  entity whose relation target was renamed.
- **Deletions.** An entity missing from the listing, because it was deleted or is
  no longer in the selected kinds or namespaces, is tombstoned and cognee's
  orphan cleanup forgets it.
- **TechDocs.** For each entity ref in `techdocs`, the small
  `techdocs_metadata.json` is checked on every run. The site's search index is
  downloaded again only when its build timestamp changes, and becomes one
  document per page. Pages that disappear, docs that are removed, and refs
  dropped from `techdocs` are tombstoned.
- **Failures.** 429 and 5xx responses are retried, honouring `Retry-After` (capped at
  60 seconds). Any other failed request aborts the run before anything is emitted, so
  a partial listing never looks like a deletion. A 401 or 403 error explains the
  token setup, and a web page in place of JSON points at a wrong `base_url`. Paging
  stops with an error if Backstage hands out the same cursor twice.
- **Safety net.** If the listing comes back empty for the same kinds and namespaces
  as the last run, which is what a token that lost its read permission looks like,
  the run stops instead of forgetting everything. Choosing a different selection
  that is really empty does forget the old entities. Entities without a kind or
  name are skipped and counted in `failed`.

**Limits:** each run lists every selected entity, so narrow large catalogs with
`kinds` and `namespaces`. `Location` and `Template` entities are skipped by
default because they are catalog plumbing. API definitions (`spec.definition`)
are not rendered.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake `requests` session, so no instance is needed. They cover
rendering, filters and cursor paging, retries and error hints, change detection,
deletions and scope changes, TechDocs pages and builds, and full dlt `merge`
runs into a temporary SQLite destination.
