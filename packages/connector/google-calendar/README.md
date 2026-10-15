# cognee-community-connector-google-calendar

A Google Calendar data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your calendars into memory, incrementally, and forget events when they are deleted.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each event becomes a **normal
document** in cognee (it flows through chunking and LLM entity extraction), via cognee's
document-mode marker. The calendar name, organizer and attendees with their responses are
written into the content, so the same person is linked across meetings.

## Requirements

- cognee **1.6.3** (pinned). The connector relies on table-scoped document cleanup
  (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR`, `guarded_rows`, and reuses cognee's
  Google OAuth settings (`cognee.modules.integrations.google.GoogleSettings`).
- Python 3.11 to 3.13.
- A Google account.

## Install

```bash
uv pip install cognee-community-connector-google-calendar
# or, from this monorepo:
cd packages/connector/google-calendar && uv sync
```

## Setup

1. In the [Google Cloud Console](https://console.cloud.google.com), create a project (free)
   and enable the **Google Calendar API**.
2. Under **Google Auth Platform**, set up the consent screen as **External**, keep it in
   **Testing**, and add your own Google account as a **test user**. Add the scope
   `https://www.googleapis.com/auth/calendar.readonly` only.
3. Under **Clients**, create an OAuth client of type **Desktop app** and download its JSON.
4. Sign in once and see your calendar ids:

   ```bash
   export GOOGLE_CALENDAR_CREDENTIALS_PATH=~/client_secret.json
   export GOOGLE_CALENDAR_TOKEN_PATH=~/.cognee_google_calendar_token.json
   uv run python examples/login.py
   ```

   A browser opens for consent. The token is cached in that file, written private, and
   refreshed on later runs. Apps left in **Testing** get refresh tokens that expire after 7
   days; publish the app, or sign in again, for longer use.
5. Set your `LLM_API_KEY` like any other cognee run.

**Headless or hosted:** instead of the browser flow, set `GOOGLE_CALENDAR_REFRESH_TOKEN`
with `GOOGLE_CALENDAR_CLIENT_ID` and `GOOGLE_CALENDAR_CLIENT_SECRET`, or pass a ready-made
`access_token` (never refreshed).

## Usage

```python
import cognee
from cognee_community_connector_google_calendar import google_calendar_source

await cognee.remember(
    google_calendar_source(calendar_ids=["primary"]),  # GOOGLE_CALENDAR_* env vars for auth
    dataset_name="my_calendar",
    primary_key="id",
    write_disposition="merge",  # required: incremental upserts and deletes
    max_rows_per_table=0,  # no row cap; a calendar exceeds the default 50
)

answer = await cognee.recall("Who is in my Thursday meetings?", datasets=["my_calendar"])
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py`
for the full flow.

| Argument | Env var | Default |
|---|---|---|
| `calendar_ids` | `GOOGLE_CALENDAR_IDS` (comma-separated) | `primary`; `examples/login.py` lists the ids |
| `settings` | `GOOGLE_CALENDAR_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN`, `_CREDENTIALS_PATH`, `_TOKEN_PATH` | read from the environment |
| `access_token` | `GOOGLE_CALENDAR_ACCESS_TOKEN` | for hosts and quick tries only |
| `resource_name` | – | `google_calendar_events`; give each account its own name when several sync into one dataset |

After a run, `source.cognee_sync_stats` holds counts only (scanned, skipped, deleted,
calendars), never titles or content.

## Reused from cognee

- **`GoogleSettings`**: `GoogleCalendarSettings` subclasses cognee's Google OAuth settings
  with the `GOOGLE_CALENDAR_` prefix, so client id and secret are configured the same way as
  cognee's own Google integrations.
- **`GoogleAuthError`**: the same log-safe error for sign-in problems.
- **The Gmail and Drive service builders** are mirrored, not called: both hard-code their own
  scopes. `build_calendar_service` follows `build_gmail_service` (installed-app flow, private
  0600 token file) and `build_calendar_service_from_access_token` follows the hosted
  `build_*_service_from_access_token` helpers, with only the calendar.readonly scope.
- **Token refresh** runs inside dlt's worker thread, so it uses google-auth's own refresh
  instead of cognee's async `refresh_access_token`.

## How sync and forget-on-delete work

- **Sync tokens:** the first run lists every event of each calendar; later runs pass that
  calendar's `nextSyncToken` and get only what changed. The token only arrives on the last
  page, so it is saved in dlt state once the whole round has finished. Sync-token requests
  keep the same parameters on every page and use no date window, as the API requires.
- **Recurring events** are listed with `singleEvents=False`: a series is one document with its
  `RRULE`, and a changed single occurrence is its own document.
- **Deleted events** come back with `status: cancelled` and are forgotten.
  - A **cancelled single occurrence** of a series only forgets that occurrence, never the
    series.
  - **Deleting a whole series** also forgets its changed occurrences.
- Deleted items are emitted as `{"id": ..., "_deleted": True}` with a `hard_delete` column.
  dlt removes them on `merge`, and cognee's orphan cleanup removes them from the graph and
  vector stores.
- **Errors never delete.**
  - `410 Gone` means the sync token expired: that calendar is listed again and reconciled
    like a snapshot, so events deleted meanwhile are still forgotten.
  - A calendar that disappears is relisted first and only forgotten on a confirmed 404.
  - 401, 403, 429 and 5xx raise after the client's retries, and dlt keeps the previous
    state.
- Removing a calendar from `calendar_ids` forgets the events of that calendar.

## Privacy

This connector reads personal data. Be clear about what it does:

**What is read**

- From the selected calendars only: title, calendar name, start and end with time zone,
  location, organizer, attendees with their name, email and response, the recurrence rule,
  status, event type, the Google Meet link, the description and the event link.
- Not read: attachments, conference details beyond the Meet link, reminders, colors, and
  other calendars. `workingLocation` events are skipped.
- The only scope is `calendar.readonly`. The connector never creates, changes or deletes
  anything in Google Calendar.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured LLM
  and embedding providers, like any document you add to cognee. Use local models if that
  matters to you.
- dlt state stores each calendar's sync token, the event ids and the calendar name. It does
  not store titles or descriptions.
- Logs and `cognee_sync_stats` contain counts only.
- The token file holds a refresh token. It is written private (0600); never commit it.
  Revoke access any time at <https://myaccount.google.com/permissions> and delete the file.

**How deletion is honoured**

- Deleted events, deleted series and cancelled occurrences are forgotten on the **next sync**.
  Run syncs in the foreground: forgetting happens as part of `remember`.
- Removing a calendar from `calendar_ids` forgets its events.
- To remove everything at once, forget the whole dataset with `cognee.forget`.

## Limitations

- The full history of each calendar is read on the first run; the sync-token API allows no
  date window.
- `workingLocation` events are skipped.
- Attachments are not ingested.

## Testing

```bash
uv run pytest tests/
```

No Google account or API key is needed. `tests/fake_calendar.py` is an in-memory Calendar
API in the style of cognee's own Gmail tests (`service.events().list(...).execute()`). It
keeps a change log per calendar and replays it through sync tokens, returning deleted events
as `cancelled` stubs like Google does.

- `test_google_calendar_sync.py` covers paging, the same parameters on every sync-token page,
  rendering, all-day events, omitted attendees, skipped event types, edits, deletes, a series
  as one document, a cancelled occurrence that keeps the series, a changed occurrence and its
  cancellation, deleting a whole series, an expired sync token, a deleted calendar, errors
  that never delete including one on a later page, two calendars with the same event id,
  and dropping a calendar from the config.
- `test_google_calendar_source.py` covers the settings inherited from cognee, the
  installed-app flow and its private token file, the auth choices, refusing an empty
  calendar list, and the resource markers.
- `test_google_calendar_forget.py` runs the real cognee pipeline (LLM and embeddings mocked,
  working with both cognee model variants) and checks that a deleted event leaves the graph
  while a series with one cancelled occurrence stays.
