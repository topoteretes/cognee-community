# cognee-community-connector-google-meet

A Google Meet data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync who joined your meetings, and when, plus their transcripts into memory, incrementally,
and forget a transcript when its Google Doc is deleted.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every finished conference and
every transcript becomes a **normal document** in cognee (it flows through chunking and LLM
entity extraction), via cognee's document-mode marker. Each conference document carries the
meeting link, written exactly like the Google Calendar connector writes it, so a conference
and its calendar event end up linked in the graph.

## Requirements

- cognee **1.6.3** (pinned). The connector relies on table-scoped document cleanup
  (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR`, `guarded_rows`, and reuses cognee's
  Google OAuth settings (`cognee.modules.integrations.google.GoogleSettings`).
- Python 3.11 to 3.13.
- A Google account that **organizes** the meetings: Meet only lists conferences where the
  signed-in user was the organizer.
- Transcripts need a Google Workspace edition that can transcribe. On a personal Gmail
  account the connector still syncs attendance; there are simply no transcripts.

## Install

```bash
uv pip install cognee-community-connector-google-meet
# or, from this monorepo:
cd packages/connector/google-meet && uv sync
```

## Setup

1. In the [Google Cloud Console](https://console.cloud.google.com), create a project (free)
   and enable the **Google Meet REST API** and the **Google Drive API**.
2. Under **Google Auth Platform**, set up the consent screen as **External**, keep it in
   **Testing**, and add your own Google account as a **test user**. Add only these scopes:
   - `https://www.googleapis.com/auth/meetings.space.readonly`
   - `https://www.googleapis.com/auth/drive.meet.readonly` (only Drive files Meet created; used
     to see whether a transcript Doc still exists)
3. Under **Clients**, create an OAuth client of type **Desktop app** and download its JSON.
4. Sign in once and see your recent conferences:

   ```bash
   export GOOGLE_MEET_CREDENTIALS_PATH=~/client_secret.json
   export GOOGLE_MEET_TOKEN_PATH=~/.cognee_google_meet_token.json
   uv run python examples/login.py
   ```

   A browser opens for consent. The token is cached in that file, written private, and
   refreshed on later runs. Apps left in **Testing** get refresh tokens that expire after 7
   days; publish the app, or sign in again, for longer use.
5. Set your `LLM_API_KEY` like any other cognee run.

**Headless or hosted:** instead of the browser flow, set `GOOGLE_MEET_REFRESH_TOKEN` with
`GOOGLE_MEET_CLIENT_ID` and `GOOGLE_MEET_CLIENT_SECRET`, or pass a ready-made
`access_token` (never refreshed).

## Usage

```python
import cognee
from cognee_community_connector_google_meet import google_meet_source

await cognee.remember(
    google_meet_source(),  # GOOGLE_MEET_* env vars for auth
    dataset_name="my_meetings",
    primary_key="id",
    write_disposition="merge",  # required: incremental upserts and deletes
    max_rows_per_table=0,  # no row cap
)

answer = await cognee.recall("Who joined yesterday's meetings?", datasets=["my_meetings"])
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py`
for the full flow.

**With Google Calendar:** Meet knows who joined but not what the meeting was called. Sync
the [Google Calendar connector](../google-calendar) into the **same dataset** and cognee can
answer "who actually joined the Falcon drone sync and when?": the title comes from Calendar,
the attendance from Meet, and the shared `Meeting link` line joins them.
`python examples/example.py --with-calendar` does both.

| Argument | Env var | Default |
|---|---|---|
| `include_transcripts` | – | `True`; turning it off later forgets the transcripts already synced |
| `check_drive_deletes` | – | `True`; with `False` only the Meet scope is asked and deleted Docs are not noticed |
| `settings` | `GOOGLE_MEET_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN`, `_CREDENTIALS_PATH`, `_TOKEN_PATH` | read from the environment |
| `access_token` | `GOOGLE_MEET_ACCESS_TOKEN` | for hosts and quick tries only |
| `resource_name` | – | `google_meet`; give each account its own name when several sync into one dataset |

After a run, `source.cognee_sync_stats` holds counts only, never names or content:
conferences, transcripts, deleted, pending and failed, plus `failed_auth`,
`failed_rate_limit`, `failed_conference`, `failed_transcript_check` or
`failed_conference_list` saying why.

### What a conference looks like in memory

```
Google Meet conference
Meeting link: https://meet.google.com/abc-mnop-xyz
Meeting code: abc-mnop-xyz
Started: 2026-10-17T10:02:00Z
Ended: 2026-10-17T10:45:12Z
Participants (times in UTC):
- Meera Joshi (signed in): 10:02 to 10:20, 10:25 to 10:45
- Ravi (guest): 10:05 to 10:45
- +91 *** *** 1234 (phone): 10:10 to 10:30
Transcript: yes
Recording: no
```

A transcript is its own document, one line per spoken entry:
`[10:03:12] Meera Joshi: The drone battery lasts 55 minutes.`

## Reused from cognee

- **`GoogleSettings`**: `GoogleMeetSettings` subclasses cognee's Google OAuth settings with
  the `GOOGLE_MEET_` prefix, so client id and secret are configured the same way as cognee's
  own Google integrations.
- **`GoogleAuthError`**: the same log-safe error for sign-in problems.
- **The Gmail and Drive service builders** are mirrored, not called: both hard-code their own
  scopes. `build_meet_services` follows `build_gmail_service` (installed-app flow, private
  0600 token file) and `build_meet_services_from_access_token` follows the hosted
  `build_*_service_from_access_token` helpers, with only the read-only Meet scopes.
- **The Google Calendar connector's pattern** (settings subclass, three ways in, a pure sync
  function over a state dict, the `_deleted` hard-delete column) is followed, but its code is
  not imported: two connector packages should not depend on each other.

## How sync and forget-on-delete work

- **Cursor:** Meet has no change feed. The first run lists every conference Meet still keeps
  (30 days). Later runs list the conferences that started from one hour before the newest
  finished one, and skip the ones already synced. The cursor only moves forward.
- **Pending:** a conference that is still running, or whose transcript is still being
  written, is not final. It waits in a pending list, is read again by id on every run, and is
  synced once it is done.
- **Two kinds of rows:** a conference document and a transcript document. They are separate
  on purpose: deleting a transcript forgets the words but keeps the attendance record.
- **Deleted transcripts:** Meet keeps listing a transcript after its Google Doc is deleted, so
  Drive is the delete signal. Every run checks the synced Docs; a trashed or missing Doc is
  forgotten. Deleted items are emitted as `{"id": ..., "_deleted": True}` with a
  `hard_delete` column. dlt removes them on `merge`, and cognee's orphan cleanup removes them
  from the graph and vector stores.
- **Expiry is not a delete:** Meet deletes a conference record 30 days after it ends. The
  connector then stops tracking it, and what cognee learned stays. A transcript keeps being
  watched after that, because its Doc lives on in Drive.
- **Errors never delete.** A conference that fails stays pending and is read again on the
  next run; a transcript Doc that cannot be checked stays watched. Every API call happens
  before the first row is yielded.
- **One bad conference does not block the others.** When Google answers with an HTTP error,
  the other conferences finish and save their state. The run raises only when nothing
  finished, because dlt rolls back the state of a failed run. A 401, a 429 and a 403 with a
  rate-limit reason end the run, since every later call would fail the same way; any other
  403 only fails its own conference. A failing conference list always raises.
- **Host checkpoint:** `check_active` runs before every Google request, so a revoked
  connection stops the sync between two calls.

## Privacy

This connector reads personal data. Be clear about what it does:

**What is read**

- For conferences you organized: the meeting link and code, start and end time, each
  participant's display name and kind (signed in, guest, phone with a partly hidden
  number), and when they joined and left. Whether a recording exists, as yes or no.
- For transcripts: each spoken entry's speaker, time and text, and the Doc's link.
- From Drive: only whether a transcript Doc still exists (`id` and `trashed`), and only for
  files Meet created.
- Not read: recordings themselves, smart notes, chat, email addresses, and meetings that
  someone else organized.
- The scopes are `meetings.space.readonly` and `drive.meet.readonly`. The connector never
  creates, changes or deletes anything in Meet or Drive.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured LLM
  and embedding providers, like any document you add to cognee. Use local models if that
  matters to you.
- dlt state stores the cursor time, conference ids with their expiry time, and transcript
  ids with their Doc ids. It does not store names or text.
- Logs, errors and `cognee_sync_stats` contain counts and HTTP statuses only: no conference,
  space or Doc ids, meeting codes or names, and not the request URLs that googleapiclient
  puts in its own error text.
- The token file holds a refresh token. It is written private (0600); never commit it.
  Revoke access any time at <https://myaccount.google.com/permissions> and delete the file.

**How deletion is honoured**

- A transcript whose Doc is deleted or trashed is forgotten on the **next sync**. Run syncs
  in the foreground: forgetting happens as part of `remember`.
- Turning `include_transcripts` off forgets the transcripts already synced.
- To remove everything at once, forget the whole dataset with `cognee.forget`.

## Limitations

- Only conferences the signed-in user **organized** are listed; that is how the Meet API
  works.
- Meet keeps conferences for 30 days, so the first run reaches back 30 days at most.
- Signed-in participants come with a display name only. Mapping them to an email address
  would need the People API and another scope; the shared meeting link is what ties a
  conference to its calendar event and its attendees' emails instead.
- Times are written in UTC.
- Recordings are noted, never downloaded. Smart notes are not synced.

## Testing

```bash
uv run pytest tests/
```

No Google account or API key is needed. `tests/fake_meet.py` is an in-memory Meet and Drive
API in the style of the Google Calendar connector's tests
(`meet.conferenceRecords().list(...).execute()`). It answers like the live API did in a
manual check: lists come newest first, an empty list is `{}`, and every list call pages.

- `test_google_meet_sync.py` covers the conference and transcript rendering (signed-in,
  guest and phone participants, a rejoin as two sessions, unknown speakers), paging on every
  list call, the overlap window, the cursor never moving back, running conferences and late
  transcripts, several transcripts, a deleted or trashed Doc forgotten while the conference
  stays, expiry that deletes nothing, Meet and Drive errors that never delete including one
  on a later page, a failing conference that stays pending while the others finish, auth and
  rate limits (also a 403 with a rate-limit reason) that end the run but keep what finished,
  `check_active` before every request, a participant order that does not depend on Google's,
  no ids or names in logs or errors, no Drive calls when checks are off, and turning
  transcripts off.
- `test_google_meet_source.py` covers the settings inherited from cognee, the installed-app
  flow and its private token file, the scopes for each option, a cached token that lacks a
  scope, the auth choices, and the resource markers.
- `test_google_meet_forget.py` runs the real cognee pipeline (LLM and embeddings mocked,
  working with both cognee model variants) and checks that a transcript whose Doc was
  deleted leaves the graph, while the other transcript and the attendance record stay.
