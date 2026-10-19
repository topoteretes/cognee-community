# cognee-community-connector-outlook

An Outlook mail and calendar data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync mail folders and your calendar into
memory through Microsoft Graph, incrementally, and forget mail and meetings when they are
deleted.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each message and event
becomes a **normal document** in cognee (it flows through chunking and LLM entity
extraction), via cognee's document-mode marker. Sender, recipients, organizer and
attendees are written into the content, so one person is linked across their emails and
their meetings.

## Requirements

- cognee **1.6.3 or newer** (below 2). The connector relies on table-scoped document
  cleanup (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR` and `guarded_rows`.
- Python 3.10 to 3.13.
- A Microsoft account: personal (Outlook.com, Hotmail) or work or school.

## Install

```bash
uv pip install cognee-community-connector-outlook
# or, from this monorepo:
cd packages/connector/outlook && uv sync
```

## Setup

1. **Register an app** in the [Microsoft Entra admin center](https://entra.microsoft.com)
   under **App registrations → New registration**:
   - Supported account types: **Accounts in any organizational directory and personal
     Microsoft accounts**.
   - No redirect URI is needed.
   - Under **Authentication**, set **Allow public client flows** to **Yes** (needed for
     the device-code login).
   - Under **API permissions**, add the **delegated** Microsoft Graph permissions
     `User.Read`, `Mail.Read` and `Calendars.Read` (or `Mail.ReadBasic` for headers only).

   Personal accounts need their own directory to register apps, which Microsoft gives
   you with an Azure account. Work or school accounts may need an admin to allow app
   registration or to grant consent. The same app can be shared with the Teams and
   SharePoint connectors.
2. **Sign in once.** Copy the **Application (client) ID**, then:

   ```bash
   export OUTLOOK_CLIENT_ID=<application (client) id>
   export OUTLOOK_TOKEN_CACHE_PATH=~/.cognee_outlook_token_cache.json
   uv run python examples/login.py
   ```

   It prints a code to enter at <https://microsoft.com/devicelogin>. MSAL caches the
   tokens in that file (written private) and refreshes them on later runs, so this
   happens once.
3. Set your `LLM_API_KEY` like any other cognee run.

**Quick try without an app registration:** sign in to
[Graph Explorer](https://developer.microsoft.com/graph/graph-explorer), consent to
`Mail.Read` and `Calendars.Read`, copy the token from its **Access token** tab and set
`OUTLOOK_ACCESS_TOKEN`. It expires after about an hour and is never refreshed.

## Usage

```python
import os

import cognee
from cognee_community_connector_outlook import outlook_source

await cognee.remember(
    outlook_source(
        mail_folders=["inbox", "sentitems"],
        client_id=os.environ["OUTLOOK_CLIENT_ID"],
        token_cache_path=os.environ["OUTLOOK_TOKEN_CACHE_PATH"],
    ),
    dataset_name="my_outlook",
    primary_key="id",
    write_disposition="merge",  # required: incremental upserts and deletes
    max_rows_per_table=0,  # no row cap; a mailbox exceeds the default 50
)

answer = await cognee.recall(
    "What did Meera email me before our Thursday sync?", datasets=["my_outlook"]
)
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py`
for the full flow. `outlook_source()` takes everything as arguments; the example reads
them from the `OUTLOOK_*` environment variables shown in the second column.

| Argument | Example env var | Default |
|---|---|---|
| `mail_folders` | `OUTLOOK_MAIL_FOLDERS` (comma-separated) | `inbox`, `sentitems`; well-known names or folder ids |
| `include_mail` / `include_calendar` | – | both on |
| `include_mail_bodies` | – | on; off reads headers only and needs only `Mail.ReadBasic` |
| `mail_received_after` | `OUTLOOK_MAIL_RECEIVED_AFTER` | all mail; for example `2026-01-01T00:00:00Z` |
| `calendar_days_back` / `calendar_days_ahead` | – | 180 / 180 |
| `calendar_window_refresh_days` | – | 7 |
| `client_id` | `OUTLOOK_CLIENT_ID` | – |
| `authority` | `OUTLOOK_AUTHORITY` | `https://login.microsoftonline.com/common` |
| `token_cache_path` | `OUTLOOK_TOKEN_CACHE_PATH` | none: log in on every run |
| `access_token` | `OUTLOOK_ACCESS_TOKEN` | for quick tries only |
| `resource_name` | – | `outlook_items`. Lowercase letters, digits and underscores, up to 64. **Two mailboxes in one dataset need different names**, or they would share one table and one sync state |
| `check_active` | – | optional host callback, asked before every Graph request |
| `service` | – | a pre-built Graph `RESTClient`, for hosts and tests |

After a run, `source.cognee_sync_stats` holds counts only, never subjects or content:
mail changed and deleted; events changed, deleted, left the window and failed; the
`scanned`, `deleted` and `failed` totals the other connectors publish; and `failed_auth`,
`failed_rate_limit`, `failed_connection`, `failed_mail` and `failed_calendar`.

## How sync and forget-on-delete work

- **HTTP** goes through dlt's `RESTClient`. Pages are followed through
  `@odata.nextLink`, and a page link Graph sends twice stops that part instead of
  looping. 429 throttling and transient 5xx errors are retried at most 3 times, waiting
  `Retry-After` but never more than 60 seconds, since cognee holds its dlt staging lock
  during a sync. After that the part stops and is retried next run.
- **Mail:** one Graph delta query per selected folder
  (`mailFolders/{folder}/messages/delta`). The first run lists the folder; later runs
  get only what changed. The `@odata.deltaLink` only arrives on the last page, so it is
  saved in dlt state once the whole round has finished.
- **Calendar:** one delta query over a date window (`calendarView/delta`). The window is
  fixed for the life of its delta link, so every `calendar_window_refresh_days` a fresh
  round starts and is reconciled against the events already known.
- **Recurring meetings** become one document: the series master carries the title,
  description and "Repeats: daily from … until …". Graph also lists every occurrence,
  but only with its id and times, so those bare entries are skipped. Changed single
  occurrences (exceptions) carry full details and get their own document. A series that
  started before the window may be listed only through its occurrences; its master is
  then fetched by id, so the meeting is not lost.
- **Immutable ids:** every request sends `Prefer: IdType="ImmutableId"`, so a message
  keeps its id when it moves between folders.
- **Moves are not deletes.** The connector keeps, per message, the synced folders it is
  in. A message moved from Inbox to Archive, with both synced, is updated in place, even
  if the two folders report the move in different runs.
- **Deleting mail in Outlook moves it to Deleted Items.** Deleted Items and Junk are not
  synced, so the message is in none of the synced folders any more and is forgotten.
  The same happens to mail moved to a folder you do not sync.
- **Calendar removals are checked by id.** Graph also reports an event as removed when it
  leaves the window. Before forgetting it, the connector looks the event up: a 404
  forgets it, otherwise it just left the window and is kept. Cancelled meetings are
  forgotten.
- **One unreadable event never stops the calendar.** When the lookup of a single event
  fails, with a 403 or a 5xx, the event is kept, counted in `events_failed`, and looked
  up again next run, while every other event syncs as usual. The same goes for a series
  master that fails to load. A 401, a 429 and a lost connection still end the run, since
  every later request would fail the same way.
- Deleted items are emitted as `{"id": ..., "_deleted": True}` with a `hard_delete`
  column. dlt removes them on `merge`, and cognee's orphan cleanup removes them from the
  graph and vector stores.
- **Errors never delete.** An expired delta token (`410 Gone`, `syncStateNotFound`)
  starts a fresh round for that folder or the calendar, reconciled like a snapshot. A
  folder that disappears is looked up first and only forgotten on a confirmed 404.
- **Mail and calendar fail separately.** When one of them gets a Graph error, the other
  still finishes and saves its state, and the failed one keeps its previous state and is
  retried next run. The run raises only when nothing finished. A 401, a 429 after the
  retries, or a lost connection end the run, since the other part would fail the same
  way. Mail folders stay all or nothing: a message moved between two synced folders
  would look deleted if only one of them were read.
- **Host checkpoint:** `check_active` runs before every Graph request, so a revoked
  connection stops the sync between two calls.

## Privacy

This connector reads personal data. Be clear about what it does:

**What is read**

- **Mail**, from the selected folders only (Inbox and Sent Items by default): subject,
  sender, To and Cc recipients, sent time, the folder name, the conversation id, whether
  it has attachments, the web link and the plain-text body. Attachments are **not**
  read. Bcc is never read. With `include_mail_bodies=False` no body is read, and the
  narrower `Mail.ReadBasic` permission is enough.
- **Calendar**, primary calendar only, within the date window: title, start and end,
  location, organizer, attendees with their response, the series id or repeat type, the
  online meeting link, the description and the web link.
- Nothing outside the selected folders, the `mail_received_after` date and the calendar
  window leaves the mailbox. The connector only reads: it never sends, moves, deletes or
  changes anything.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured
  LLM and embedding providers, like any document you add to cognee. Use local models if
  that matters to you.
- dlt state stores the delta links, message and event ids and which synced folder each
  message is in. It does not store subjects or bodies.
- Everyone who can read the cognee dataset can read the synced mail and events.
- Logs, errors and `cognee_sync_stats` contain counts and HTTP statuses only: no message,
  event or folder ids, and not the request URL with its delta token that `requests` puts
  in its own error text.
- The MSAL token cache holds a refresh token. It is written private (0600); never
  commit it. Revoke access any time at <https://account.live.com/consent/Manage> for
  personal accounts, or **My Apps** for work or school accounts, and delete the file.

**How deletion is honoured**

- Mail deleted in Outlook (moved to Deleted Items), mail moved out of the synced folders,
  deleted events and cancelled meetings are forgotten on the **next sync**. Run syncs in
  the foreground: forgetting happens as part of `remember`.
- Removing a folder from `mail_folders` forgets the mail only that folder covered.
- To remove everything at once, forget the whole dataset with `cognee.forget`.
- Turning `include_mail` off stops syncing mail but keeps what was already synced.

## Limitations

- Only the primary calendar: Graph v1.0 event delta covers that one.
- An event that leaves the date window is kept in memory as it was last seen and is no
  longer tracked; deleting it later is not noticed.
- Attachments are not ingested, only flagged.
- Subfolders of a selected folder are not synced; list them in `mail_folders` by id.

## Testing

```bash
uv run pytest tests/
```

No Microsoft account or API key is needed. `tests/fake_graph.py` is an in-memory
Microsoft Graph served over HTTP with `responses`, so dlt's real `RESTClient`, the
`@odata.nextLink` paginator and its retries run unchanged. It keeps a change log per
folder and for the calendar and replays it through delta tokens.

- `test_outlook_sync.py` covers paging, the `Prefer` headers and the bearer token,
  headers-only mode, the received-after filter, edits, moves between synced folders
  (also split across two runs), deletes through Deleted Items, moves out of scope,
  expired delta tokens, a deleted folder, dropping a folder from the config, auth and
  API errors that must not delete, throttling, the calendar window, a recurring series
  with its bare occurrences, events deleted versus moved out of the window, cancelled
  meetings, window refresh, failed event lookups kept and checked again next run, one
  unreadable event not stopping later runs, a series master retried, `Retry-After`
  capped at a minute, a repeated page link, all-day events ending on their last day, a stable attendee order, mail and calendar failing separately without leaking a
  half-finished round, auth, throttling and a lost connection ending the run,
  `check_active` before every request, and no ids or delta tokens in logs or errors.
- `test_outlook_source.py` covers the device-code login, the token cache file mode,
  ready-made tokens, scopes per option, folder normalization, settings as arguments only,
  an unsafe `resource_name` refused, `service`, and the resource markers.
- `test_outlook_forget.py` runs the real cognee pipeline (LLM and embeddings mocked,
  working with both cognee model variants, including the one used with Gemini) and
  checks that a deleted message and a deleted meeting leave the graph and the dataset
  while a message moved between synced folders stays, and that two mailboxes in one
  dataset never touch each other's mail. It fails if the tombstone or the `hard_delete`
  column is turned off.
