# cognee-community-connector-salesforce

A [Salesforce](https://www.salesforce.com) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync **Accounts, Contacts, Opportunities
and Cases** into memory, each together with the text people write around it - case
comments, tasks, and Chatter posts with their replies - so questions like "which open cases
put a renewal at risk, and what did we promise them?" have something to recall.

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each record is ingested as a
document (cognee's document mode), so it goes through cognify entity extraction, and edits
and deletes in Salesforce reach memory on the next sync.

## Requirements

Python 3.10 to 3.14, and cognee 1.6.4 or later (for per-row node sets).

## Install

```bash
uv pip install cognee-community-connector-salesforce
# or, from this monorepo:
cd packages/connector/salesforce && uv sync
```

## Setup

The connector signs in with OAuth 2.0's **client credentials flow** as a dedicated,
read-only integration user, so no password and no refresh token lives in its config.

1. **An integration user.** Setup → Users → New User, with the **Salesforce Integration**
   user license and the **Minimum Access - API Only Integrations** profile. It can't log in
   to the UI and the profile grants nothing on its own. No password is needed.
2. **Read access, granted explicitly.** Assign the user the **Salesforce API Integration**
   permission set license, then a permission set (with that license) granting:
   - Read and View All on Account, Contact, Opportunity and Case, and read on the fields of
     those four plus Task and Event;
   - **Access Activities**, if you want tasks. Without it Salesforce answers every Task
     query with `INVALID_TYPE` *"sObject type 'Task' is not supported"*, even when every
     Task field is readable.
   Granting field access from a script: address and geolocation *components* (`BillingStreet`
   of `BillingAddress`) can't take their own field permission; Salesforce rejects them with
   `INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST`, and only the compound field takes one.
3. **An External Client App.** Setup → External Client App Manager → New. Enable OAuth with
   the single scope **Manage user data via APIs (api)** and enable **only** the **client
   credentials flow** under its OAuth settings. (Per Salesforce's Spring '26 release notes,
   creating new *connected apps* is off by default.) The flow has a second switch on the app's
   **Policies** tab, under OAuth Policies; turn it on there too, and set its **Run As** user to
   the integration user. Salesforce's own warning applies: *anyone with the consumer key and
   consumer secret can access your org on behalf of the selected user* - keep the secret
   out of code and logs.
4. **Your My Domain URL** (Setup → My Domain), e.g. `https://acme.my.salesforce.com`.
   Salesforce's documentation says the client credentials flow needs the My Domain URL, not
   `login.salesforce.com`, so the connector refuses `login.` and `test.salesforce.com`.

## Usage

Inside an async function (or a notebook):

```python
import cognee
from cognee_community_connector_salesforce import salesforce_source

await cognee.remember(
    salesforce_source(
        login_url="https://acme.my.salesforce.com",
        client_id="...",  # the app's consumer key
        client_secret="...",  # and secret
    ),
    dataset_name="salesforce",
    primary_key="id",
    write_disposition="merge",  # required: the add pipeline defaults to "replace"
    self_improvement=False,  # no enrichment pass on every scheduled sync
)
```

See `examples/example.py` for the full flow, including a `GRAPH_COMPLETION` search.

| Argument | Default | |
| --- | --- | --- |
| `login_url` | required | the org's My Domain URL |
| `client_id`, `client_secret` | required | the External Client App's consumer key and secret |
| `resource_name` | `salesforce_<my domain>` | scopes sync state and cleanup; keep it fixed. Must be lowercase snake_case. Two sources with different selections of one org in one dataset each need their own |
| `objects` | all four | a **list** drawn from Account, Contact, Opportunity, Case |
| `where` | none | a SOQL condition per object, e.g. `{"Case": "IsClosed = false"}`; records that stop matching are forgotten. A filtered object's ids are listed on every run (see below) |
| `include_comments`, `include_tasks`, `include_chatter` | `True` | which child text goes into each document |
| `api_version` | `"66.0"` | the REST API version |
| `max_requests` | 2000 | requests one run may make (retries of a failed request and token re-mints are not counted); a big backfill finishes over several runs. It must fit one pass of the selection (16 for the defaults), or it is refused |

## What gets ingested

One document per record, with its own fields first and then the text around it, newest
first and bounded. This is the connector's actual output for the test case recorded on the
live org (the "(cognee test)" suffixes marked the test records there):

```
Case 00001026: Shipment tracking page shows stale locations (cognee test)
Status: New · Priority: High · Origin: Email · Open
Account: Alphacorp Logistics (cognee test) · Contact: Dana Okafor (cognee test) · Owner: Anmol Gupta
Description: Since the 2.4 release the tracking page lags by about 40 minutes for Alphacorp shipments.
Case comments (3, newest first):
  2026-10-29 (public, Anmol Gupta): Cache TTL lowered to 5 minutes in 2.4.1; Dana confirmed the lag is gone.
  2026-10-29 (internal, Anmol Gupta): Reproduced: the location feed cache TTL is 45 minutes.
  2026-10-29 (public, Anmol Gupta): Workaround sent to Dana: force refresh from the shipment page.
Tasks (1, newest first):
  2026-10-29 Completed: Call Dana about the tracking lag (cognee test) - Agreed to a weekly check-in until the cache fix ships.
Chatter (1 post, newest first):
  2026-10-29 Anmol Gupta: Escalating: Alphacorp says the lag cost them a missed delivery window on Tuesday (cognee test).
    reply Anmol Gupta: Engineering has a fix in 2.4.1, rolling out Thursday (cognee test).
    reply Anmol Gupta: Update: 2.4.1 is live for Alphacorp, delivery windows back to normal (cognee test).
```

At most 20 comments, 20 tasks and 20 posts per record, newest first, each post with its first
10 replies in order; long text fields are cut at 3,000 characters with a marker saying how
much was dropped. Chatter posts typed in the Salesforce UI are rich text (`IsRichText`): their
tags are dropped and entities such as `&amp;` turned back into characters. A plain-text body
is kept exactly as written.

Contacts, opportunities and cases carry their account's name, and cases their contact's: a
contact document that said only "Dana Okafor, Head of Operations" would lose the one fact
that makes it useful to recall. The cost is that a rename has to reach those documents too
(see below).

Each document carries one node set, `salesforce:<object>` (e.g. `salesforce:case`), so a
search can be scoped to one object type. There is deliberately no node set per account or
owner: their number would grow with the org.

## How sync works

**Records** are read by `SystemModstamp`, Salesforce's own "last touched" stamp (per
Salesforce's documentation it also moves on system and automation changes, which
`LastModifiedDate` does not; not reproduced here). The read is `SystemModstamp >= cursor`,
never `>`: on the org this was tested against, the sample accounts were bulk-created in one
second and share one `SystemModstamp`, and a run that stops between two pages of tied records
resumes from that stamp, so a strict cursor would skip the tied records it hadn't read yet. A few minutes are re-read
on each run in case a transaction committed late; a document is only rewritten when its
rendered text actually changed.

**Child text has its own cursors.** Recorded on a live org: adding a comment to a case did
**not** move the case's `SystemModstamp`, and neither did a Chatter post. So case comments,
tasks and each object's feed are read by their own stamps, and every changed child
re-renders its parent's document. A late Chatter reply *does* move its post's stamp, so one
cursor per feed is enough. Children are read with `queryAll`, which also returns rows still
in the Recycle Bin, so a deleted comment or task updates its parent too. The child cursors
start before the first record is read, so a first run cut short by its budget still catches
comments added to records it already read. Each child cursor moves up with every completed
run, quiet or not. The Recycle Bin is emptied after 15 days, and a comment purged from it no
longer shows in `queryAll`, so after more than 14 days without a run the records a stream
belongs to are read in full instead.

**Names shown in other documents.** Renaming an account moves only the account's
`SystemModstamp`, not its contacts', opportunities' or cases'. So whenever an account's or
contact's title changes, or one is deleted or comes back from the Recycle Bin, the documents
that show its name are rendered again (`AccountId IN (...)`, `ContactId IN (...)`; one query
per 200 names and dependent object), after the run's deletes. The names still to do are kept
in sync state, 200 at a time, so a run cut short carries on where it stopped.

**Ties inside a document.** Two comments on the live test case share a `CreatedDate` to the
second, and Salesforce promises no order among ties, so every child subquery breaks them on
`Id`. An order that changed between reads would rewrite an unchanged document.

**Chatter** comes from `<Object>Feed` queries (`CaseFeed`, `AccountFeed`, ...). Querying
`FeedItem` directly is refused for this kind of user (*"Implementation restriction: FeedItem
requires a filter by Id"*), and so is `FeedComment` (*"only supported for admin users"*).
Every case comment is also mirrored in the feed as a `CaseCommentPost` with an **empty**
body; only human posts (`TextPost`, `LinkPost`, `ContentPost`) are read, so a comment is never
counted twice. Feed tracking is set per object and can be off, and per Salesforce's
documentation an untracked object has no feed to query. So each run asks once
(`GET /sobjects/`, whose `feedEnabled` covers every object) and leaves Chatter out of the
objects without it; turning tracking on or off re-reads that object in full.

**Deletes** come from Salesforce's deleted-records list
(`/sobjects/<type>/deleted/?start=&end=`), with two behaviours this connector is built
around, both recorded:

- `latestDateCovered` is "guaranteed complete up to", not "nothing newer is shown". On a new
  org it lagged hours behind the request while a fresh delete was already listed. The next
  window starts there (re-reported deletes are harmless), but never before
  `earliestDateAvailable`.
- The list can refuse a window with one error code, `INVALID_REPLICATION_DATE`, and three
  messages: *"startDate before org replication enabled date"* on a new org, *"startDate
  cannot be more than 30 days ago"* on a stale cursor, and *"startDate must be at least one
  minute greater than endDate"* - the last seen in a connector run log when two runs were
  seconds apart, not saved as a recording; a recorded 40-second window across a minute
  boundary was answered normally. A window under a minute is not asked for at all; it waits
  for the next run. Either way, and whenever
  `earliestDateAvailable` is later than the window asked for, the connector lists every id of
  the object instead. A stored record missing from that list is fetched by id, and forgotten
  only on `404 NOT_FOUND` - which Salesforce answers identically for a deleted record and an
  id that never existed. Any other answer, a 5xx after retries included, keeps it and
  counts it in `failed`, so one record that keeps failing can't stop every run.

The deleted-records API accepts a start up to **30 days** back, while Salesforce keeps
deleted records in the Recycle Bin for **15 days**; deletes older than that may already be
gone from the list, which is another case the id comparison covers.

**Undeletes** heal themselves: a restored record comes back with a new, later
`SystemModstamp` and drops out of the deleted-records list, so the next sync re-ingests it.

**A filtered object** (`where`) has its matching ids listed on every run. A record can stop
matching without its `SystemModstamp` moving - a formula field, or a relative date such as
`CloseDate = THIS_MONTH` - so the listing is the selection: a stored record it misses is
forgotten (deleted or no longer matching, either way not selected), and a listed record that
was never read is read now. That costs one request per page of matching ids on every run
(Salesforce documents pages of up to 2,000 rows; the test org answered with 200-row pages).

**A changed selection** (different `objects` or `where`) forgets the records of objects no
longer selected, reads the rest in full again and compares ids, so records that left a
narrowed filter are forgotten too. The default `resource_name` does not depend on the
selection, so narrowing it cleans up under the same name. Two sources with different
selections of one org in one dataset must each get their own `resource_name`; sharing one,
each would forget the other's records on every run.

**Errors never delete.** A refused credential (`400 invalid_client` on the token,
`401` after a fresh token), a malformed page, or an outage raises, and dlt commits neither
rows nor state. A query page without its records list or done flag, or an unfinished page
with no next page, raises rather than reading as "no records".

**Tokens.** Client-credentials tokens come back with no `expires_in` and no refresh token, so
the connector mints a new one when Salesforce answers `401 INVALID_SESSION_ID` and retries
the request once.

**API allowance.** Each response's `Sforce-Limit-Info: api-usage=<used>/<limit>` is read,
and a run stops cleanly while 5% of the org's daily allowance is still left. That share is a
policy of this connector, not a Salesforce rule: the allowance is shared with every other
integration on the org (a Developer Edition org has 15,000 calls a day), and a sync that
spent it to the last call would break them until it resets. A run spends one request on its
token, one on the object list (for Chatter), one per query page (200 rows on the test org,
up to 2,000 per Salesforce's docs), one per 200 records re-rendered for changed children or
names, and one per deleted-records window per
object.

### Sync stats

`source.cognee_sync_stats` holds the counts of the last run: `scanned` (records rendered),
`skipped` (unchanged, no document rewritten), `deleted`, `failed` (records Salesforce sent in
a shape that couldn't be rendered, and records whose existence check got an error other than
404; also at least 1 whenever the run was cut short or the credential was refused), and `completed`, `True` only when the run finished. A run stopped by
`max_requests`, by the allowance read from `Sforce-Limit-Info`, or by Salesforce refusing a
request for the daily limit **returns normally** - `remember()`
succeeds - and only `completed: False` with `failed_budget`, `failed_api_limit` or
`failed_rate_limit` says otherwise; `failed_auth` marks a refused credential. Read these if
you need to know whether a sync finished.

### Limitations

- An owner's name in a document goes stale when the owner is renamed: a user rename moves
  none of the records they own, and re-reading every owned record on each rename isn't worth
  it. The next change to the record picks the new name up.
- Four standard objects. Custom objects and fields aren't read; every org has them, but which
  ones matter differs per org, and the issue scoped this to the standard four.
- Legacy Notes and Content Notes (`ContentNote` is not available to the integration user) are
  not read.
- A task moved to another record (its `WhatId` or `WhoId` changed) shows up in its new
  record's document, but stays in the old one until the old record changes: the task stream
  only reports where a task is now. Whether Salesforce moves the old record's stamp was not
  tested.
- With `objects` leaving out Account (or Contact), a rename of one is never seen, so the
  documents of the selected objects keep the old name until they change themselves.
- A comment or task deleted *and* emptied from the Recycle Bin before the next run is never
  seen (no API reports it any more), so it stays in its record's document until the record
  changes or is read in full.
- A record's document holds its newest 20 comments, tasks and posts; older ones still exist
  in Salesforce but aren't in memory.
- The records at the newest `SystemModstamp`, and those a few minutes before it, are read again
  on every run - the price of never skipping a tie. After a bulk import that can be many: on
  the test org, the run after 220 contacts were inserted in one second re-read those 224
  records (two 200-row pages) and rewrote none. It stops as soon as anything newer changes.
- Per-record REST and SOQL fit connector scale. A very large first backfill would be better
  served by Bulk API 2.0, which this connector does not use.
- **Who can read the dataset.** Documents carry names, case descriptions and **internal**
  case comments (`IsPublished = false` - the integration user can read them). Give the cognee
  dataset the same audience as the Salesforce org, and keep it in a dataset of its own.

## Verified against a real org vs. taken from the docs

Measured on a Developer Edition org (a Trailhead Playground) on 2026-10-29 and 30, as the
read-only integration user described above, with test records created and changed by an
admin: the token response (no `expires_in`, `issued_at` in epoch milliseconds), the
`invalid_client` and `INVALID_SESSION_ID` refusals, that a write is refused (`400
CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY`), every query shape above including nested `Feeds` with
`FeedComments` and `null` for an empty subquery, which objects this user can and can't read,
the `Access Activities` requirement, the shared `SystemModstamp`s, that child text doesn't
move its parent, that a reply moves its post, the deleted-records responses and the first two
`INVALID_REPLICATION_DATE` messages, the identical 404 for deleted and never-existing ids,
the undelete behaviour, and query paging: a 220-row listing came back as 200 rows with
`done: false` and a `nextRecordsUrl`, then 20 with `done: true` and no next link - and all
200 rows of the first page shared one `SystemModstamp`, as a bulk insert does. Asking for the
same page link again after the listing had finished returned that page again rather than an
error, which is why the connector refuses a repeated link itself. Also recorded: `feedEnabled`
in the object list for all four objects; Salesforce accepting `ORDER BY CreatedDate DESC, Id
DESC` in the comment, task and post subqueries and `CreatedDate, Id` in the reply subquery;
`IsRichText` on posts and replies (false for posts made through the API); and a 40-second
deleted-records window across a minute boundary being answered. The final connector ran
against that org through a real dlt pipeline: 315 documents on the first run, then 224
re-reads (the bulk-inserted contacts sharing the newest stamp) with none rewritten; its
dependents query (`AccountId IN`, one account) returned exactly that account's 220 contacts;
and a filtered source (`{"Case": "IsClosed = false"}`) ran twice the same way. An earlier
build's run log is where the one-minute refusal was seen.

Taken from Salesforce's documentation, not reproduced: that `SystemModstamp` moves on
automation changes, that the client credentials flow needs My Domain, that new connected apps
are off by default since Spring '26, that an object without feed tracking has no feed to
query (every object on the test org has it on), that UI posts are rich text, the `403 REQUEST_LIMIT_EXCEEDED`
answer to an exhausted daily allowance (reaching 15,000 calls on a test org wasn't worth
it), and the 15-day Recycle Bin retention.

## Testing

```bash
uv run pytest tests/
```

No network or LLM key needed: `tests/fake_salesforce.py` answers the connector's requests the
way the recorded org did, `tests/test_salesforce_dlt.py` drives a real dlt pipeline,
`tests/test_salesforce_forget.py` runs cognee end to end with a mocked LLM, and
`tests/test_salesforce_example.py` runs `examples/example.py` against the fake.
