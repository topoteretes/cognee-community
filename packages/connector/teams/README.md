# cognee-community-connector-teams

A Microsoft Teams data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your team channels into memory, so you can ask "what did the team decide?".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Every channel
thread (the root message plus its replies) is rendered to text and ingested as a **normal
document**. It goes through cognee's cognify entity-extraction pipeline, not the deterministic
dlt-row path. Re-syncs are **incremental**, and threads deleted in Teams are **forgotten** on the
next sync.

All Microsoft Graph I/O goes through dlt's REST API toolkit. The team and channel listing is a
declarative `rest_api` config. The per-channel message walk uses the same client config through
`RESTClient`, because it has to stop early at the cursor. Auth, paging and retries come from dlt
(`OAuth2ClientCredentials`, `JSONLinkPaginator` on `@odata.nextLink`, and `Retry-After`-aware
retries).

## Requirements

Pinned to cognee 1.6.4. The connector relies on document-mode (`DOCUMENT_SOURCE_ATTR`),
table-scoped document cleanup (`DOCUMENT_SYNC_VERSION`) and per-dataset pipeline state
(`PIPELINE_SCOPE_ATTR`). `teams_source` raises on a cognee that lacks table-scoped cleanup.

## Install

```bash
uv pip install cognee-community-connector-teams
# or, from this monorepo:
cd packages/connector/teams && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_teams import teams_source

await cognee.remember(
    teams_source(team_ids=["<team id>"]),  # MS_GRAPH_* credentials from env
    dataset_name="teams",
    write_disposition="merge",  # incremental: later runs only fetch what changed
)

answer = await cognee.search(
    query_text="What did we decide about the Q3 launch?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["teams"],
)
```

> **Pass `write_disposition="merge"` to sync incrementally.** The add pipeline defaults to
> `"replace"`. Under `"replace"`, dlt resets the connector's cursors every run, so each run walks
> every channel in full and loads a complete snapshot. That is still correct (deleted threads drop
> out and are forgotten), but it re-reads all history.

Choose what to ingest:

| Argument | Effect |
| --- | --- |
| `team_ids=[...]` | Sync these teams. Omit it to sync every team the app can list (needs `Team.ReadBasic.All`). |
| `channel_ids=[...]` | Only sync these channels within the selected teams. |

Only **standard** channels are read. 1:1 chats, group chats, private channels and shared
channels are never fetched. See `examples/example.py` for the full flow.

## Setup: Entra ID app registration

The connector authenticates as an app (OAuth2 client credentials), not as a user. One app
registration can serve the Teams, SharePoint and Outlook connectors. They all read the same
`MS_GRAPH_*` variables, and `graph_client_config()` builds the shared `rest_api` client block.

1. In the [Microsoft Entra admin center](https://entra.microsoft.com), go to **Identity →
   Applications → App registrations → New registration**. Give it a name, keep "Accounts in this
   organizational directory only", and register it.
2. From the app's **Overview** page, copy the **Directory (tenant) ID** and the **Application
   (client) ID**.
3. Under **Certificates & secrets → Client secrets → New client secret**, create a secret and
   copy its **Value** right away. It is shown only once.
4. Under **API permissions → Add a permission → Microsoft Graph → Application permissions**, add:

   | Permission | Why |
   | --- | --- |
   | `ChannelMessage.Read.All` | Read channel messages and replies |
   | `Channel.ReadBasic.All` | List the channels of a team |
   | `Team.ReadBasic.All` | List teams (only needed when `team_ids` is omitted) |

5. Click **Grant admin consent for \<tenant\>**. These permissions read messages across the
   organization, so **a tenant admin (Global Administrator or Privileged Role Administrator) must
   grant consent**. An end user cannot do it themselves. Until consent is granted, every call
   fails with `403 Forbidden`.
6. Export the credentials, plus your `LLM_API_KEY` as for any other cognee run:

   ```bash
   export MS_GRAPH_TENANT_ID="<directory (tenant) id>"
   export MS_GRAPH_CLIENT_ID="<application (client) id>"
   export MS_GRAPH_CLIENT_SECRET="<client secret value>"
   ```

   You can also pass `tenant_id=`, `client_id=` and `client_secret=` to `teams_source`.

To find a **team id**, open the team in Teams, choose **⋯ → Get link to team**, and copy the
`groupId=` value from the link. Channel ids (`19:...@thread.tacv2`) are in a channel's
**Get link to channel** URL, URL-encoded.

### Without tenant-wide admin consent: resource-specific consent

Microsoft Graph also supports resource-specific consent (RSC). With RSC, a **team owner** grants
the app access to a single team by installing it in that team. No tenant admin is involved,
although the tenant must allow RSC for apps (it is allowed by default). Use these
permissions in the Teams app manifest under `authorization.permissions.resourceSpecific`
(type `Application`) instead of the tenant-wide ones above:

| Permission | Replaces |
| --- | --- |
| `ChannelMessage.Read.Group` | `ChannelMessage.Read.All` |
| `ChannelSettings.Read.Group` | `Channel.ReadBasic.All` |

With RSC the app cannot list teams, so always pass `team_ids`, and only teams that installed the
app can be synced. See Microsoft's
[resource-specific consent guide](https://learn.microsoft.com/microsoftteams/platform/graph-api/rsc/resource-specific-consent)
for the manifest format.

## How sync and forget-on-delete work

- **One document per thread.** Each thread is stored with id `thread:<team>:<channel>:<root>`.
  It holds the root message and its replies in time order, each with author and timestamp.
  Mentions become `@Name` and attachments are listed by file name. A reply edit or deletion
  re-renders the whole thread. Unchanged threads keep a stable content-hash `data_id`, so they
  are not re-ingested or re-cognified.
- **Incremental cursor.** Graph removed the channel-message delta query from v1.0, and its
  replacement (`getAllMessages`) is a metered export API. The connector uses the free
  `GET /teams/{id}/channels/{id}/messages?$expand=replies` list instead. Graph documents that
  list as *sorted by the last modified date of the entire reply chain*, newest first. Each run
  walks a channel from the top and stops at the first thread whose chain timestamp is older
  than that channel's cursor, minus a 5-minute overlap for Graph's eventual consistency. An
  unchanged channel costs one request. The cursors are kept per channel in dlt's resource
  state.
- **Forget-on-delete.** The resource loads with `merge` and has an `_deleted` hard-delete
  column:
  - A deleted root message (Graph keeps it with `deletedDateTime` set) tombstones its thread.
  - A deleted reply only re-renders the thread without it.
  - A channel or team that is deleted, or dropped from `team_ids`/`channel_ids`, has every
    thread it contributed tombstoned.

  dlt removes tombstoned rows on `merge`. cognee's `orphan_cleanup` then removes them from the
  graph and vector stores.
- **Failure safety.** Errors are never swallowed: a `401`/`403`, retries exhausted on a
  `429`/`5xx`, or a network failure. The run aborts before dlt commits the new cursors, so
  nothing is deleted and the next run retries from the same point. A listing that suddenly
  returns no channels at all is treated as suspicious and forgets nothing.

**Limitations.** Graph only reports a deletion while the message stays in the listing with
`deletedDateTime` set. If a retention policy purges a message outright, its thread stays in
memory until its channel is removed or you re-sync into a fresh dataset. Threads with no text
(only images or cards) are not ingested. Use one `teams_source` per tenant per dataset: the
staging table and the cursors are shared by every team the source reads.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests need no tenant. Graph is served by an in-memory fake mounted as a `requests`
transport adapter, so the real dlt OAuth2, `rest_api`, paging and retry code runs. They cover:

- rendering
- the token flow and paging past 50 threads and past the inline reply page
- the chain-timestamp stop rule (an unchanged channel costs one request)
- edits, reply deletion, root deletion, channel removal and deselection, and team removal
- `Retry-After` handling, and safety on a failed walk
- a real `dlt` merge that proves a tombstone removes the row
- an end-to-end `cognee.add` + `cognify` run (LLM mocked) proving an unchanged re-sync makes no
  LLM call, an edited thread is replaced and re-extracted, and a deleted thread's entity (the last
  one included) leaves the graph
- a smoke run of the packaged `examples/example.py`
