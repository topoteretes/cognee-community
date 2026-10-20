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
- Python 3.10 to 3.14.
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
   happens once. A sync never logs in by itself: when the refresh fails it raises
   `GraphLoginRequiredError`, and you run `login.py` again. Use one cache file per
   mailbox, or pass `account=` when a cache holds several.
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
| `mail_folders` | `OUTLOOK_MAIL_FOLDERS` (comma-separated) | `inbox`, `sentitems`; well-known names in any case, or folder ids. Deleted Items and Junk Email are refused |
| `include_mail` / `include_calendar` | – | both on |
| `include_mail_bodies` | – | on; off reads headers only and needs only `Mail.ReadBasic` |
| `mail_received_after` | `OUTLOOK_MAIL_RECEIVED_AFTER` | all mail; for example `2026-01-01T00:00:00Z` |
| `calendar_days_back` / `calendar_days_ahead` | – | 180 / 180 |
| `calendar_window_refresh_days` | – | 7, at least 1 |
| `time_zone` | – | `UTC`; an IANA zone like `Asia/Kolkata` that event times are written in |
| `client_id` | `OUTLOOK_CLIENT_ID` | – |
| `authority` | `OUTLOOK_AUTHORITY` | `https://login.microsoftonline.com/common` |
| `token_cache_path` | `OUTLOOK_TOKEN_CACHE_PATH` | filled once by `examples/login.py` |
| `account` | – | the only account in the cache; name one when the cache holds several |
| `access_token` | `OUTLOOK_ACCESS_TOKEN` | for quick tries only |
| `resource_name` | – | `outlook_items`. Lowercase letters, digits and underscores, up to 64. **Two mailboxes in one dataset need different names**, or they would share one table and one sync state |
| `check_active` | – | optional host callback, asked before every Graph request |
| `service` | – | a pre-built Graph `RESTClient`, for hosts and tests |

Building the source never touches the network; the first Graph request happens when
`remember` runs it.

After a run, `source.cognee_sync_stats` holds counts only, never subjects or content:
mail changed, deleted and deferred, and mail folders that failed; events changed,
deleted, left the window, failed and given up; the `scanned`, `deleted` and `failed`
totals the other connectors publish; and `failed_auth`, `failed_rate_limit`,
`failed_connection`, `failed_mail` and `failed_calendar`.

## How sync and forget-on-delete work

- **HTTP** goes through dlt's `RESTClient` with a client built for each run and closed
  after it. Pages are followed through `@odata.nextLink`, and a page link Graph sends
  twice stops that folder instead of looping. 429 throttling, 5xx errors and dropped
  connections are retried at most 3 times per request, waiting `Retry-After` but never
  more than 60 seconds, and all waits of one run share 2 minutes, since cognee holds its
  dlt staging lock during a sync. After that the request fails and the next run picks it
  up.
- **The token only goes to Graph.** The bearer token is attached only to requests for
  the `base_url` host; a page link that points anywhere else fails that folder instead.
- **Rows stream page by page.** New and changed messages and events are handed to cognee
  as each page arrives, so a big mailbox is never held in memory. Forgets and the new
  delta links wait until a round has finished; a round that fails halfway is read again
  next run, and `merge` makes the repeated rows harmless.
- **Mail:** one Graph delta query per selected folder
  (`mailFolders/{folder}/messages/delta`). The first run lists the folder; later runs
  get only what changed. The `@odata.deltaLink` only arrives on the last page, so it is
  saved in dlt state once the whole round has finished. `include_mail_bodies` and
  `mail_received_after` live inside that link, so the state records them, and changing
  either lists every folder again: turning bodies on fills them in, and a later
  `mail_received_after` forgets the older mail.
- **Calendar:** one delta query over a date window (`calendarView/delta`). The window is
  fixed for the life of its delta link, so every `calendar_window_refresh_days` a fresh
  round starts and is reconciled against the events already known.
- **Recurring meetings** become one document: the series master carries the title,
  description and "Repeats: daily from … until …". Graph also lists every occurrence,
  but only with its id and times, so those bare entries are skipped. Changed single
  occurrences (exceptions) carry full details and get their own document. When
  occurrences of a series show up in a round, its master is fetched by id, so an edited
  title or attendee list is picked up even when Graph does not list the master itself,
  as with a series that started before the window.
- **Times** are asked for in UTC and written in `time_zone`. All-day events are written
  as dates in the event's own zone, so a holiday in India is not shown a day early.
- **Immutable ids:** every request sends `Prefer: IdType="ImmutableId"`, so a message
  keeps its id when it moves between folders.
- **Moves are not deletes.** The connector keeps, per message, the synced folders it is
  in. A message moved from Inbox to Archive, with both synced, is updated in place, even
  if the two folders report the move in different runs. Folders are read one after the
  other, so a message that leaves a folder may have moved into one read earlier in the
  same run; when it left a folder other than the first, its forget waits one run, and
  the next run sees it arrive or forgets it.
- **Deleting mail in Outlook moves it to Deleted Items.** Deleted Items and Junk are not
  synced, so the message is in none of the synced folders any more and is forgotten.
  The same happens to mail moved to a folder you do not sync.
- **Calendar removals are checked by id.** Graph also reports an event as removed when it
  leaves the window. Before forgetting it, the connector looks the event up: a 404
  forgets it, otherwise it just left the window and is kept. Only events that were
  stored are looked up and forgotten: deleting a series reports every occurrence, and
  those were never ingested. Cancelled meetings are forgotten.
- **One unreadable event never stops the calendar.** A lookup that fails with a 4xx, or
  answers with something that is not JSON, keeps that event, counts it in
  `events_failed`, and asks again next run, while every other event syncs as usual.
  After the first 5xx the remaining lookups wait for the next run instead of each
  waiting on a struggling Graph. After 3 runs of failed lookups the connector gives up:
  a removal Graph reported is forgotten, a series master stops being fetched, and both
  count in `events_given_up`. A 401, a 429 and a lost connection still end the run,
  since every later request would fail the same way.
- Deleted items are emitted as `{"id": ..., "_deleted": True}` with a `hard_delete`
  column. dlt removes them on `merge`, and cognee's orphan cleanup removes them from the
  graph and vector stores.
- **Errors never delete.** An expired delta token (`410 Gone`, `syncStateNotFound`)
  starts a fresh round for that folder or the calendar, reconciled like a snapshot. A
  synced folder that disappears is looked up first and only forgotten on a confirmed
  404. A folder in `mail_folders` that was never synced and does not exist raises
  `OutlookFolderNotFoundError`, naming it, since that is a typo rather than a deletion.
- **Mail folders and the calendar fail separately.** A folder that gets a Graph error
  keeps its delta link and is read again next run, while the other folders and the
  calendar sync as usual; its count is in `mail_folders_failed`. While a folder fails,
  mail that left the others is not forgotten yet, since it may have moved into the
  failing one, but for 3 runs at most, so a folder that never recovers cannot block
  deletes for good. The run raises only when nothing finished. A 401, a 429 after the
  retries, or a lost connection end the run, since everything else would fail the same
  way.
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
- The connector's own log lines, its errors and `cognee_sync_stats` contain counts and
  HTTP statuses only: no message, event or folder ids, and not the request URL with its
  delta token that `requests` puts in its own error text. dlt's own logger is another
  matter: at INFO it logs request paths with folder and event ids, and at DEBUG the full
  URL with the delta token. A delta token is not a credential without the bearer token,
  but keep dlt's log level at WARNING if logs leave your machine.
- The MSAL token cache holds a refresh token. It is written private (0600), and an
  existing file is made private when the cache is saved; never commit it. Revoke access any time at <https://account.live.com/consent/Manage> for
  personal accounts, or **My Apps** for work or school accounts, and delete the file.

**How deletion is honoured**

- Mail deleted in Outlook (moved to Deleted Items), mail moved out of the synced folders,
  deleted events and cancelled meetings are forgotten on the **next sync**, or the one
  after for mail that left a folder other than the first one listed, or while a folder
  fails (see above). Run syncs in the foreground: forgetting happens as part of
  `remember`.
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

- `test_outlook_sync.py` covers paging and rows streamed page by page, the `Prefer`
  headers and the bearer token, headers-only mode, the received-after filter and
  changing it or the bodies option later, edits, moves between synced folders (also
  split across two runs, and into a folder read earlier), deletes through Deleted Items,
  moves out of scope, expired delta tokens, a deleted folder, an unknown folder, dropping
  a folder from the config, one failing folder holding back deletes without stopping the
  others, auth and API errors that must not delete, throttling and one wait budget per
  run, the calendar window, a recurring series with its bare occurrences, deleting a
  whole series, a changed series master fetched again, events deleted versus moved out
  of the window, cancelled meetings, window refresh, failed event lookups kept, asked
  again and given up after 3 runs, lookups that answer HTML, state saved by the previous
  version, `Retry-After` capped at a minute, a repeated page link, times in a chosen
  zone, all-day events east and west of UTC, a stable attendee order, mail and calendar
  failing separately without leaking a half-finished round, auth, throttling and a lost
  connection ending the run, `check_active` before every request, and no ids or delta
  tokens in the connector's logs or errors.
- `test_outlook_source.py` covers the device-code login only through `login()`, a sync
  that needs a new login raising instead of waiting, several accounts in one cache, the
  token never sent to another host, the token cache file made private even when it
  exists, building the source with no network, ready-made tokens, scopes per option,
  folder normalization, Deleted Items and Junk refused, settings that cannot work
  refused, settings as arguments only, an unsafe `resource_name` refused, `service`, a
  client per run that is closed after it, and the resource markers.
- `test_outlook_forget.py` runs the real cognee pipeline (LLM and embeddings mocked,
  working with both cognee model variants, including the one used with Gemini) and
  checks that a deleted message and a deleted meeting leave the graph and the dataset
  while a message moved between synced folders stays, and that two mailboxes in one
  dataset never touch each other's mail. It fails if the tombstone or the `hard_delete`
  column is turned off.
