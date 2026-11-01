# cognee-community-connector-klaviyo

A Klaviyo data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync an account's campaigns, flows and email templates into memory, so an agent can
answer "what discount did we offer in the fall sale?" or "what does a new subscriber
receive in their first week?".

It exposes a `dlt` resource you hand to `cognee.remember(...)` / `cognee.add(...)`. Every
record becomes a **normal document** that goes through cognify's entity extraction (via
cognee's document mode), not the deterministic dlt-row path. Syncs are incremental and
forget-on-delete.

## Requirements

- cognee **1.6.3** or later 1.x (document mode, `PIPELINE_SCOPE_ATTR`, `guarded_rows` and
  the `cognee_node_set` column). The source checks this and refuses older releases.
- A Klaviyo **private API key** with read access to Accounts, Campaigns, Flows and
  Templates. The free plan is enough.
- Every request pins Klaviyo API revision `2026-10-15` (see below).

## Install

```bash
uv pip install cognee-community-connector-klaviyo
# or, from this monorepo:
cd packages/connector/klaviyo && uv sync --group dev
```

## Usage

```python
import cognee
from cognee_community_connector_klaviyo import klaviyo_source

source = klaviyo_source()  # KLAVIYO_API_KEY
await cognee.remember(
    source,
    dataset_name="klaviyo",
    primary_key="id",
    write_disposition="merge",  # required, see below
    max_rows_per_table=0,  # no row cap, so cleanup sees the whole corpus
)
print(source.cognee_sync_stats)

results = await cognee.recall("Which campaigns promoted free shipping?", datasets=["klaviyo"])
```

`write_disposition="merge"` is required: `remember()` defaults to `"replace"`, which would
drop every record an incremental run does not re-emit.

### Options

| Argument | Default | What it does |
|---|---|---|
| `api_key` | `KLAVIYO_API_KEY` | A private key (`pk_...`). |
| `resources` | `("campaigns", "flows", "templates")` | Any of the three. |
| `include_archived` | `False` | Keep archived campaigns and flows. By default archiving one forgets it. |
| `resource_name` | `"klaviyo_documents"` | The dlt table name, which also scopes the sync state. Give each account its own name when several share a dataset. |
| `check_active` | `None` | Host authorization checkpoint, called before every request and around every row. Raise to stop the sync. |
| `service` (alias `session`) | `None` | A `requests`-like session, mainly for tests. Without one, a session is built per run and closed after it. |

## What is stored

One document per record, each in its own node set (`campaigns`, `flows`, `templates`),
with ids like `campaign:01J…`, `flow:XyZ12`, `template:AbC34`:

- **Campaigns:** name, description, and for each message its status, send time and
  channel, then the copy: subject, preview text, sender name and the email body (the
  template's HTML turned into text; its separate plain-text version only when there is
  no HTML, because Klaviyo does not regenerate it when the HTML is edited). SMS and push
  messages carry their text inline. A/B variations are listed one after the other.
- **Flows:** name, status and trigger, then every step in the order a profile meets it:
  emails (subject, preview text, body), SMS and push messages, waits ("Wait 2 days"),
  splits, A/B tests (with the message they send) and profile updates. Webhook steps are
  named, but their payload is not rendered.
- **Templates:** the account's template library: name, editor type and the body as text.
  A flow email works on its own copy of a template, which Klaviyo leaves out of the
  library listing; that copy is part of the flow's document, not a template document.

Email copy keeps Klaviyo's variables but not their filters or block tags:
`{{ first_name|default:'friend' }}` becomes `{{ first_name }}`, and
`{% unsubscribe %}` is dropped.

### Privacy

- Never read: profiles, lists, segments, metrics, events.
- Never stored: sender, reply-to, cc and bcc addresses; audience ids; tracking settings.
- The connector only reads. A read-only key is all it needs.

### Left out on purpose

Modification times and tracking or throttling settings change without the copy changing.
Every document column feeds cognee's content hash, so including them would re-cognify a
campaign for nothing. They are not rendered.

## How sync + forget-on-delete work

Each run has four steps, and the state is only written at the end. A run that fails
partway leaves the state as it was, so the next run starts from the same place.

1. **List.** Each kind is listed with `filter=greater-or-equal(updated…)` from the previous
   run's newest update time (minus a 60 second overlap), sorted by update time. After the
   first run, a second, light listing (sparse fieldsets: ids, archived flag and update
   time) holds every id, for the deletion sweep. Paging follows `links.next`; a page
   without a `data` list, a next link off `a.klaviyo.com`, or a repeated cursor aborts
   the run.
2. **Confirm deletions.** An id that left the selection (archived campaigns stay in
   Klaviyo's listings, flagged archived) is fetched by id before it is forgotten. It counts
   as gone only when Klaviyo answers 404, or answers archived while archived records are
   out of scope. One that still answers in scope is kept, and any
   other failure aborts the run, so a flaky listing can never delete anything. An
   emptied listing is confirmed one id at a time, never read as "delete everything".
3. **Render.** A record is fetched in full and rendered when:
   - its update time moved since it was last rendered (the overlap lists the newest
     records again, but their unchanged time keeps them from being fetched again);
   - it is a campaign with a **draft or scheduled** message. Editing a message's subject
     or its email does not move the campaign's own update time, so these are read on
     every run. Klaviyo does not let a sent campaign be edited, so sent ones are left
     alone until they change;
   - it uses an email **template that changed**. The templates each campaign and flow use
     are kept in the state, and templates updated since the last run (one cheap listing)
     mark the campaigns and flows that use them, also when the `templates` resource is
     not selected;
   - it is a flow whose **email copy was edited**. A flow email has its own copy of a
     template: Klaviyo leaves it out of the template listing, and editing it does not move
     the flow's update time. So each copy is asked for its update time on every run, one
     light request per flow email (the templates endpoint allows 750 requests a minute);
   - it is in scope but was never rendered (for example archived records, once
     `include_archived` is turned on), or a listing left it out although it is still
     there;
   - it failed to load last run, or the connector's rendering changed since the last run
     (a render version in the state; only records whose text came out different are
     re-ingested).

   A fingerprint of the rendered text is kept, and a record whose text did not change is
   skipped. A record that fails to load (a 5xx after retries) is kept and retried next
   run. Three in a row stop the run as an outage, and a 401 or 403 stops it at once.
4. **Delete and commit.** Confirmed deletions are emitted as `_deleted` rows (dlt's
   hard-delete column), and cognee's orphan cleanup removes their documents, graph nodes
   and vectors. A resource dropped from `resources` is forgotten the same way.

**API revision.** Klaviyo versions its API by date and changes response shapes between
revisions (2026-10-15 moved a campaign's fields under `definition` and its copy into
message variations), so every request sends `revision: 2026-10-15`. Klaviyo supports a
revision for two years after release.

**Rate limits and waits.** Klaviyo limits each endpoint (flows: 3 requests a second, 60 a
minute). 429 and 5xx answers and network errors are retried up to 5 times, with
`Retry-After` on a 429 and exponential backoff with jitter otherwise. cognee holds its
dlt staging lock during a sync, so a single wait over 60 s, or more than 120 s of waiting
in one run, ends the run instead.

**Errors and secrets.** Error messages name only the HTTP status and the endpoint template
(`/campaigns/{id}`). They never contain a record id, the key or a response body.
Klaviyo's error code is on `KlaviyoAPIError.detail`. A key containing whitespace or
control characters, or one that is not a private key, is refused up front without being
echoed.

**One account per `resource_name`.** The state remembers the account id (from
`/accounts`), and a second account under the same `resource_name` is refused rather than
having the first account's records read as deleted.

### Stats

`source.cognee_sync_stats` holds the last run's counts: `listed`, `rendered`, `unchanged`,
`deleted`, `kept` (records retried next run) and `failed`.

## Setup

1. In Klaviyo, open **Settings > Account > API keys** and create a **private** key. Give it
   **Read Access** to Accounts, Campaigns, Flows and Templates, and no other access.
2. Export it:

   ```bash
   export KLAVIYO_API_KEY="pk_..."
   ```

Then run the example twice, changing something in Klaviyo in between:

```bash
uv run python examples/example.py
```

## Testing

```bash
uv sync --group dev
uv run pytest tests
```

The tests need no key and no network:

- `tests/fake_klaviyo.py` is an in-memory account that answers like the REST API
  (JSON:API documents, cursor pages, filters, sparse fieldsets, includes).
- `test_klaviyo.py` covers rendering, template-tag cleanup, headers and the pinned
  revision, retries and waits, error messages, paging, the cursor and its overlap, draft
  campaigns, template edits reaching their owners, deletion and its safeguards, and two
  runs through a real `dlt` pipeline.
- `test_klaviyo_cognee.py` runs the source through `cognee.add()`, and through
  `cognify()` with the LLM mocked, to check that a deleted campaign leaves the graph.
