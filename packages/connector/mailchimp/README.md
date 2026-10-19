# cognee-community-connector-mailchimp

Sync a Mailchimp account into cognee memory: **campaigns (title, subject, audience
and the text of the email) and, if you want, the notes kept on contacts**, with
incremental sync and forget-on-delete.

Each campaign becomes one cognee document that goes through the full cognify
pipeline, so you can ask things like "what did our last campaign announce?" next
to your other sources.

## Requirements

- Python 3.10 to 3.13
- cognee 1.6.3 or later (document mode plus the per-row node-set column)
- A Mailchimp account (the free plan works) and an API key

## Install

```bash
uv pip install cognee-community-connector-mailchimp
# or, from this monorepo:
cd packages/connector/mailchimp && uv sync
```

## Usage

```python
import os

import cognee
from cognee_community_connector_mailchimp import mailchimp_source

await cognee.remember(
    mailchimp_source(
        os.environ["MAILCHIMP_API_KEY"],  # "...-us22"
        # optional:
        # audience_ids=["aaaaaaaaaa"],  # only campaigns sent to these audiences
        # folder_ids=["f1"],            # only campaigns in these folders
        # include_unsent=True,          # also drafts and other unsent campaigns
        # include_notes=True,           # also the notes kept on contacts
    ),
    dataset_name="mailchimp",
    write_disposition="merge",  # required, see below
)

print(await cognee.recall("What did our last campaign announce?", datasets=["mailchimp"]))
```

The source reads no environment variables itself; pass the key and the filters
as arguments (`audience_ids` and `folder_ids` take lists).

**Pass `write_disposition="merge"` to `remember()`.** `remember()` defaults to
`"replace"`, which overrides the source's own setting and breaks both the
incremental sync and deletes. Don't pass `primary_key`; the source already uses
`id`, which is what cognee expects for document sources.

Each row is tagged with a node set `mailchimp:<audience_id>`, so recall can be
scoped to one audience.

## How sync + forget-on-delete work

These rules come from testing the Mailchimp API on a live account.

- **The email text comes from `archive_html`.** Campaigns made in Mailchimp's
  current builder have no `plain_text` until they are sent, and the
  `plain_text` that appears after sending still holds raw merge tags
  (`*|UNSUB|*`, `*|LIST:COMPANY|*`) and the footer. `archive_html` has the merge
  tags resolved. The connector drops its hidden preheader, the footer block and
  the "View this email in your browser" link (which points to
  `campaign-archive.com` before sending and `mailchi.mp` after), and keeps the
  text the author wrote.
- **What counts as a change.** Campaigns have no "updated" field, and the API
  can only filter by send or create time, which misses drafts, renames and
  deletes. So every run lists every campaign (1000 per request, only the fields
  needed) and compares each one's status, send time, title, subject, preview
  text and audience with the previous run. A sent campaign cannot be edited, so
  its text is read once and again only when one of those fields changes (a
  rename, for example).
- **Drafts are opt-in** (`include_unsent=True`). Editing a draft changes nothing
  on the campaign itself, so drafts are read on every run and compared by a
  content hash; only real changes are sent to cognee. They are off by default
  because unfinished drafts often still hold the template's placeholder text.
- **A halted send is a draft again.** When Mailchimp stops a send (for example
  while it reviews a new account), the campaign goes back to `save` but keeps
  its send time, and its `archive_html` turns into plain text with one `<br />`
  per line. The connector reads that plain text with the same footer cleanup.
  By default (sent campaigns only) such a campaign is forgotten like any other
  that leaves the selection; with `include_unsent=True` it is kept as "draft; a
  send started on <date> was stopped".
- **Contact notes are opt-in** (`include_notes=True`). Adding, editing or
  deleting a note never changes the contact's `last_changed`, and notes have no
  date filter, so each run reads the notes of every contact in the selected
  audiences that has any (contacts without a `last_note` are skipped). A note is
  tied to the contact's name; the email address is requested and added only
  with `include_contact_email=True`. Addresses, phone numbers and other contact
  fields are never read.
- **Deleted campaigns and notes are forgotten** on the next sync. Mailchimp has
  no "deleted" status: a deleted campaign or note simply disappears and answers
  404. Whatever is missing from a complete listing gets a delete marker, and
  cognee's orphan cleanup removes it from memory.
- **A listing that changes while it is read proves nothing.** Listings are
  paged by offset, so a campaign or contact deleted between two pages shifts
  the next page back by one and an item is never listed. When a listing's
  total changes mid-read, that run forgets nothing (the deletes show up as
  `held_back`) and the next run decides.
- **A 404 is never a delete on its own.** A campaign or contact that vanishes
  while a run is reading it is skipped and decided by the next listing. Note
  that `/reports/{id}` keeps answering for deleted campaigns, so it is never
  used.
- **A failed sync changes nothing.** Any API error stops the run. The sync state
  is only saved when the load succeeds, and cognee only forgets things after a
  successful run, so the next sync simply retries.
- **Mass deletes wait for a second run.** When a run finds more than half of
  what it has synced gone (and at least 10 items), which is what a wrong
  audience or folder selection looks like, it keeps those items and reports
  them as `held_back`. The next run forgets them if the same items are still
  gone; anything that came back is kept. No run ever stops on this, so a
  scheduled sync needs no manual step. `allow_mass_delete=True` forgets such a
  batch at once; `max_delete_ratio` tunes the threshold.
- **Archiving is not deleting.** An archived campaign stays in memory: as a sent
  campaign ("Sent on ...; archived in Mailchimp") when it was sent, otherwise
  as an unsent one (with `include_unsent=True`). A canceled send is handled like
  a halted one.
- **Own sync state.** Each source gets its own dlt pipeline and state, named
  after its `resource_name` (default `mailchimp_<data centre>`, through
  `PIPELINE_SCOPE_ATTR`), and a run refuses to continue if the stored state
  belongs to another account.
- **Host contract.** `cognee_sync_stats` holds counts only (scanned, changed,
  unchanged, deleted, skipped, held_back, plus failed, failed_auth and
  failed_rate_limit set to 1 when a run stops on that kind of error). `service`
  takes a pre-built `httpx` client (`http_client` is the same thing), and an
  optional `check_active` callable lets a host stop a running sync.

## Setup

1. In Mailchimp, open your profile icon, then **Account & billing**, **Extras**,
   **API keys**, and **Create A Key**. Copy it right away; it is shown once.
2. The key ends with your data centre, such as `-us22`. The connector reads the
   data centre from the key and only ever talks to that host.
3. Find audience ids under **Audience**, **All contacts**, **Settings**,
   **Audience name and defaults** ("Audience ID").

Run the full demo with `uv run python examples/example.py`. With an LLM key
(`LLM_API_KEY`) recall writes an answer; without one, cognee runs in its keyless
mode (local GLiNER extraction and fastembed embeddings, downloaded on first use)
and recall returns the matching passages.

## Limitations

- Every run lists all campaigns (one request per 1000). With drafts on, every
  draft's text is read each run; with notes on, there is one request per contact
  that has notes.
- Images are dropped from the text. Automations, landing pages, templates and
  campaign reports are not synced.
- Two Mailchimp accounts on the same data centre share the default
  `resource_name` (`mailchimp_<data centre>`). Give each its own
  `resource_name`; a run refuses to continue when the stored state belongs to
  another account.
- Anyone who can read the cognee dataset can read the synced campaign text and,
  with `include_notes=True`, the contact notes and names. Keep Mailchimp data in
  its own dataset and share it as you would the Mailchimp account itself.
- Mailchimp allows 10 simultaneous connections per account and sends no
  rate-limit headers; the connector reads sequentially and retries 429 and 5xx
  responses using `Retry-After` or exponential backoff. A `Retry-After` above
  60 seconds stops the run instead (cognee holds its dlt staging lock while a
  source runs); the next sync retries.

## Behaviour seen live, not in Mailchimp's docs

The connector relies on these, all checked on a live account. If Mailchimp
changes one, this is where to look first.

- Editing a draft's body changes no field on the campaign; renaming a sent
  campaign changes only `settings.title`.
- A contact has `last_note` only while it has at least one note, and adding,
  editing or deleting a note never moves the contact's `last_changed`.
- `plain_text` is empty for builder campaigns until they are sent and still
  holds raw merge tags afterwards.
- The "View this email" link points to `campaign-archive.com` before sending and
  to `mailchi.mp` after.
- After a halted send, `archive_html` is plain text with one `<br />` per line.
- Deleted campaigns answer 404 on `/campaigns/{id}` but not on `/reports/{id}`.

## Testing

```bash
uv run pytest tests/
```

The tests use an in-memory fake of the Mailchimp API (no network, no key),
modelled on what a live account returns: offset paging, drafts that change
silently, sending (status, send time, `plain_text` appearing, the view link
moving to `mailchi.mp`), renames, deletes and 404s, notes with `last_note`, and
the auth errors. The text tests run on two real `archive_html` captures from a
live account (a builder campaign and a halted send), with their ids replaced.
They cover the HTTP client, the email text, change detection, drafts, sends,
renames, deletes, notes, the guards, and a real dlt pipeline run including a
failed run that must change nothing.

`tests/test_mailchimp_forget.py` goes through cognee itself (`add` + `cognify`,
with the LLM and embeddings mocked): a deleted campaign and a deleted note must
lose their Data record and leave the knowledge graph, while the others keep
their Data ids.
