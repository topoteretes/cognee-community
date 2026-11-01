# cognee-community-connector-moodle

A Moodle data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your courses' sections, pages, books, assignments, glossaries, announcements and
(optionally) forum discussions into memory — "ask my course".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (only items whose content changed are re-processed) and
**forget-on-delete** (items hidden or deleted in Moodle are purged from memory on the next
sync) with no core changes.

## Requirements

- Python 3.10 to 3.14.
- cognee 1.6.3 or later (below 2), for table-scoped document cleanup and per-row node sets.
- A Moodle web service token whose service exposes the read functions listed under
  **Setup**. The built-in "Moodle mobile web service" does. Moodle 4.x and 5.x work; the
  connector checks the functions at startup and names any that are missing.

## Install

```bash
uv pip install cognee-community-connector-moodle
# or, from this monorepo:
cd packages/connector/moodle && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_moodle import moodle_source

await cognee.remember(
    moodle_source(
        resource_name="moodle_main",
        base_url="https://moodle.example.edu",
        token="...",
        course_ids=[7],  # omit to sync every visible course the token's user is enrolled in
    ),
    dataset_name="moodle_courses",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by item id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Which week covers dynamic programming, and what is due that week?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["moodle_courses"],
)
```

Choose what to sync with `include=("pages", "books", "assignments", "glossaries", "announcements")`
(the default) and add `"forums"` to it to ingest discussion threads. The course overview and
its sections are always synced. Re-running `remember(...)` with the same dataset syncs only
items changed since the last run and forgets items that were hidden or deleted. See
`examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced items on the second sync.

`resource_name` is required and names one Moodle account's sync: it keys the sync state and
the cleanup of deleted items. Give every Moodle account its own name, even in one dataset —
two accounts sharing a name would forget each other's documents. `base_url` and `token` fall
back to `MOODLE_BASE_URL` and `MOODLE_TOKEN`. Pass `check_active` to let a host stop a sync
whose access was revoked; it is called before every request.

## What gets ingested

What the token's user can see, as one document per item, each tagged with its course as a node
set (`moodle:course:<id>`):

- **Course**: name, dates, summary and the list of sections.
- **Sections**: the summary and the activities in order, with text-and-media areas inlined, so
  "what belongs to week 3?" has an answer.
- **Pages**: the page text, with the section it sits in.
- **Books**: the book intro and chapter list, plus one document per chapter with its text.
- **Assignments**: name, due / submissions-from / cut-off dates and the instructions.
- **Glossaries**: the glossary intro plus one document per approved entry.
- **Announcements**: every post of the course announcements forum.
- **Forums** (opt-in): the forum intro and one document per discussion with its threaded posts.
  Authors are rendered as "Participant 1", "Participant 2", ... unless `anonymize_authors=False`.

Grades, submissions, quiz questions, participant lists and messages are never read. HTML is
converted to text that keeps headings, paragraphs and list items; links keep their text and
lose their address, so `webservice/pluginfile.php` file links never reach memory.

## How sync + forget-on-delete work

Each run reads the course contents (which activities exist and which the user can see) and
the per-type listings, which carry every item's `timemodified`. Only items whose stamp moved
or that are new are rendered again — book chapters are downloaded and forum posts fetched only
then — and a document is emitted only when the rendered text changed, so a metadata-only edit
costs no re-cognify.

An item that stops appearing in a course's complete listing — deleted, hidden, or restricted
from the user — is emitted with the `_deleted` hard-delete marker, dlt drops it on `merge`, and
cognee's `orphan_cleanup` removes it from the graph + vector + relational stores. Hiding a book
or glossary forgets its chapters or entries too. Removing a course from `course_ids` or a kind
from `include` forgets its documents. Keep `resource_name` and the dataset fixed across runs.

A run never deletes on partial information: when it is cut short (request budget, a function
that keeps failing) it keeps the progress it made, reports it in `source.cognee_sync_stats`,
and tombstones nothing; the next run resumes. An item whose read fails is kept as it is,
counted in `failed` and retried on the next run, and the rest of the sync goes on. A selected
course the user is no longer enrolled in is skipped the same way; remove it from `course_ids`
to forget it.

## Limitations

- The connector sees exactly what the token's user sees. A teacher token also sees hidden
  activities; a student token does not. Use a dedicated account with the access you want in
  memory.
- Forum posts are written by students, so forums are off by default and anonymised when on.
  Announcements (the course news forum) are on by default. With cognee's access control (on by
  default) only the user who ran the sync, and users granted permission on the dataset, can
  read it.
- A discussion is cut at 500 posts. Book chapters need the service's "Can download files"
  option (the mobile service has it); a chapter that cannot be downloaded is counted in
  `failed` and retried.
- Moodle has no change feed, so each run re-reads the listings. Those are one call per
  activity type per course, so a quiet run on a normal site is a handful of requests.

## Setup

1. **Get a token.** If your site has the "Moodle mobile web service" enabled (most do),
   open **Preferences → Security keys** and copy the key for that service. Otherwise an
   administrator creates a token under **Site administration → Server → Web services → Manage
   tokens**, for an external service that includes:
   `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `core_course_get_contents`,
   `mod_page_get_pages_by_courses`, `mod_book_get_books_by_courses`,
   `mod_assign_get_assignments`, `mod_glossary_get_glossaries_by_courses`,
   `mod_glossary_get_entries_by_letter`, `mod_forum_get_forums_by_courses`,
   `mod_forum_get_forum_discussions` and `mod_forum_get_discussion_posts`, with file downloads
   allowed.
2. Pass it as `token` (read-only: the connector only lists and reads course material), with the
   `https://` address of your site as `base_url` (`http://localhost:8000` is accepted for a
   local moodle-docker site), plus your `LLM_API_KEY` like any other cognee run.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests fake the Moodle web services (no live token) and include offline end-to-end runs
through a real `dlt` merge and through `cognee.add` and `cognify`, proving a deleted page is
removed from the graph, not only from staging.
