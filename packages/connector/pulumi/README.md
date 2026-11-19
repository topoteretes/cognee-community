# cognee-community-connector-pulumi

A Pulumi Cloud data-source connector for [cognee](https://github.com/topoteretes/cognee): turn
your stacks and their update history into memory, so you can ask "which stacks were updated
after Friday's release, and why did the payments-prod update fail?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every stack and every finished
update (`pulumi up`, refresh, destroy, import) becomes one document that flows
through cognee's normal cognify entity extraction. An update's `config` and `environment`
are never read. Previews are not part of a stack's update history in Pulumi Cloud, so they are
not synced.

## Install

```bash
uv pip install cognee-community-connector-pulumi
# or, from this monorepo:
cd packages/connector/pulumi && uv sync
```

## Setup

1. Sign in at [app.pulumi.com](https://app.pulumi.com).
2. Create an access token: your avatar, **Personal access tokens**, **Create token**. A team or
   organization token works too, and is the better choice for a shared sync.
3. Note the organization name. For a personal account it is your user name, the part after
   `app.pulumi.com/` in the console's address.
4. Pass them as `organization` and `token=`. The example reads them from `PULUMI_ORG` and
   `PULUMI_ACCESS_TOKEN` (the variable the Pulumi CLI uses), together with your `LLM_API_KEY`
   like any other cognee run:

```bash
export PULUMI_ACCESS_TOKEN="pul-..."
export PULUMI_ORG="acme"
```

## Usage

```python
import os

import cognee
from cognee_community_connector_pulumi import pulumi_source

await cognee.remember(
    pulumi_source(os.environ["PULUMI_ORG"], token=os.environ["PULUMI_ACCESS_TOKEN"]),
    dataset_name="pulumi",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
    max_rows_per_table=0,
    self_improvement=False,
)

answer = await cognee.recall("Why did the payments-prod update fail?", datasets=["pulumi"])
```

Run the same call again to sync only what changed. See `examples/example.py`.

### Choosing what to ingest

| Argument | Default | Meaning |
| --- | --- | --- |
| `organization` | required | The Pulumi Cloud organization. |
| `projects` | all | Only stacks of these projects. |
| `tag_name` | none | Only stacks carrying this tag, with `tag_value` if given. |
| `tag_value` | none | The value `tag_name` must have. |
| `since_days` | `90` | How far back the first sync of a stack reads its updates. |
| `full_resync` | `False` | Render every stack and update again, within `since_days`. |
| `api_url`, `console_url` | Pulumi Cloud | Addresses of a self-hosted Pulumi Cloud. |
| `max_requests` | `1000` | Request budget of one run. A bigger organization finishes over several runs. |
| `resource_name` | `pulumi_<organization>` | Staging table, which scopes this sync's state and cleanup, so two organizations in one dataset never touch each other's documents. A short hash is added when `projects` or a tag filter is set. Keep the same explicit name when you change the selection in place, since a new name is a separate sync. A name you pass must be lowercase snake_case (letters, digits, `_`) of at most 64 characters. |

## What is ingested

| Document | Id | Content |
| --- | --- | --- |
| Stack | `stack:<stack id>` | organization, project, stack name, resource count, tags (sorted) |
| Update | `update:<stack id>:<version>` | kind, result, version, start and finish time, resource changes by operation, resource count after, who requested it, the commit (repository, short sha, first line of its message) when it came from a VCS, and the update's message |

A stack's `lastUpdate` stays out of its document, since it moves on every update and the
update documents already cover it.

**Never ingested:** an update's `config` (stack configuration) and `environment` (environment
variables set during the update), the token that requested it, policy pack configuration, and
a stack's own `config`. All of them can hold secrets, so documents are built from an allowlist
of fields and these are never read. On top of that, every text field (the update message, the
commit message, names) goes through a scrubbing step that redacts private keys, cloud and VCS
tokens, JWTs, URL credentials and secret-looking `key = value` pairs, quoted values with
spaces included. The count of redactions is reported in `cognee_sync_stats["redacted"]`.

The update summary endpoint only reports the result, times and resource count, so there is no
per-resource change tree to add.

## How sync and forget-on-delete work

- **Stack listing.** Each run lists the organization's stacks with
  `GET /api/user/stacks`, following `continuationToken`, once per selected project.
- **Changed stacks only.** A stack whose `lastUpdate` and resource count did not move is
  skipped without another request. A changed stack has its details read (`GET` the stack, for
  its tags) and its update history.
- **Version cursor.** Update versions only increase, so each stack keeps the highest version it
  read. The history is read newest first, 100 updates a page, and stops at the cursor, so only
  newer updates are fetched. The first sync of a stack reads back `since_days`.
- **Finished updates only.** An update is ingested once it has a final result (`succeeded` or
  `failed`). One still running is kept as pending and read again on the next runs until it
  finishes.
- **Daily check.** Tags can change without an update, and an update that never saved a
  checkpoint leaves `lastUpdate` alone, so a stack's details and the newest page of its history
  are read again once a day.
- **Renames.** A renamed stack keeps its id; its document and its updates are rendered again
  with the new name.
- **Forget-on-delete.** A stack missing from a complete listing (removed with
  `pulumi stack rm`, no longer visible to the token, or outside a new selection) is removed
  from memory together with all of its updates. A listing that comes back empty while stacks
  were synced before forgets nothing until the next run confirms it, and a listing without its
  `stacks` list, or with a stack it cannot name, raises instead of forgetting anything. A stack
  that answers 404 after the listing is left as it is (`unreadable`), and the next listing
  decides.
- **A run is bounded.** 429 and 5xx are retried, honoring `Retry-After`. A `Retry-After` over
  60 seconds, a 429 that outlasts the retries, or more than five minutes of retry waits stops
  the run cleanly with `failed` and `failed_rate_limit`; a spent `max_requests` budget stops it
  with `failed` and `failed_budget`, since the sync is incomplete either way. A token rejected
  after progress stops it with `failed` and `failed_auth`; a token rejected on the first
  request raises. The stacks finished so far are kept, and the next run starts after the last
  one. Any other error raises, so dlt keeps the previous state.
- **Host checks and cleanup.** `check_active` is called before every request and around each
  row. The client built from `token` also calls it during retry waits, which are slept in
  one-second steps, so revoked access stops a long wait within a second. That client is closed
  at the end of every run, also on errors; a `service` you pass in stays yours to close.

`cognee_sync_stats` always carries `scanned` stacks, `emitted`, `unchanged` and `deleted`
documents, `pending` updates, `unreadable` stacks, `redacted` secrets, and `failed` plus
`failed_auth`, `failed_budget` and `failed_rate_limit`, `0` when unused.

**Requests per run:** one per page of the stack listing (per project), then nothing for an
unchanged stack. A changed stack adds two (its details and the first page of its history),
plus one per further history page and one per pending update.

## Privacy

This reads your stacks' names, tags and update history, including who ran each update and
the commit it came from. Nothing is fetched until you call `remember`, and anyone with read
access to the target dataset can read what was ingested, so keep Pulumi in its own dataset.
The token is sent only to the API address in the `Authorization` header, never in URLs, and is
never written into documents, state or error messages.

## Testing

```bash
uv run pytest tests/
```

The tests need no Pulumi account. They run the client against a mocked HTTP transport, check
rendering and the no-secrets rule (secrets planted in update `config` and `environment`, the
requesting token and policy configs never reach a document), drive the sync against a fake
Pulumi API with the shapes of Pulumi's OpenAPI spec (the stack gate, the version cursor,
history paging, running updates, removed and renamed stacks, empty and malformed listings,
filters, budget, rate limit and revoked-token stops), run it through a real dlt pipeline, and
run `cognee.remember` end to end to prove a removed stack's updates leave the graph.
