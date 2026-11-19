# cognee-community-connector-fathom

A [Fathom](https://fathom.ai) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync your meeting notes, decisions and action
items into memory, so an agent can answer "what did we agree with Acme on pricing?".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`). Each meeting
becomes one prose document that goes through the normal cognify entity extraction. You get
**incremental re-sync** (only meetings whose text changed are re-processed) and
**forget-on-delete** (meetings deleted in Fathom are purged from memory on the next sync) with
no core changes.

## Requirements

- Python 3.10 to 3.14 (tested on 3.11).
- cognee 1.6.3 or later (below 2), for document sources with per-row node sets.
- A Fathom API key. API access is included on every Fathom plan.

## Install

```bash
uv pip install cognee-community-connector-fathom
# or, from this monorepo:
cd packages/connector/fathom && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_fathom import fathom_source

await cognee.remember(
    fathom_source(),  # FATHOM_API_KEY from the environment, or pass api_key=...
    dataset_name="meetings",
    primary_key="id",
    write_disposition="merge",  # required, the add pipeline defaults to "replace"
)

answer = await cognee.search(
    query_text="What did we agree with Acme on pricing?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["meetings"],
)
```

Run it again with the same dataset to sync only what changed. See `examples/example.py` for the
full flow.

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`, which
> would rewrite the table with only the meetings that changed and forget the rest. The source
> checks this before its first request and refuses to run otherwise.

## Options

| Argument | Default | |
| --- | --- | --- |
| `api_key` | `$FATHOM_API_KEY` | Created in Fathom under **User Settings → API Access**. |
| `include_summary` | `True` | Fathom's default summary. |
| `include_action_items` | `True` | Action items with assignee and completion state. |
| `include_transcript` | `False` | The full speaker-attributed transcript, as `[00:05:32] Speaker: text`. |
| `include_highlights` | `False` | Fathom's highlights. |
| `recheck_days` | `14` | Meetings created in this window are fetched again on every run. `0` turns it off. |
| `teams`, `recorded_by` | `None` | Only meetings of these teams / recorded by these e-mails. |
| `since` | `None` | Ignore meetings created before this date. |
| `max_delete_fraction` | `0.5` | See *Forgetting* below. |
| `resource_name` | `"fathom_meetings"` | Names the staging table and the sync state. |

Choose at least one of summary, action items, transcript and highlights. Changing which of them
you ingest re-renders every meeting on the next run.

## What gets ingested

One document per meeting, titled with the meeting title:

```markdown
# Acme pricing review

Recorded: 2026-10-14 15:01 UTC (45 min)
Meeting type: Customer call
Recorded by: Alice Johnson (Sales team)
Attendees: Alice Johnson (ourco.com), Jane Smith (acme.com, external)

## Summary
We agreed a ten percent discount for an annual commitment ...

## Action items
- [ ] Send Acme the revised proposal (Alice Johnson)
- [x] Confirm enterprise support terms (Priya Rao)
```

- **Provenance** is the meeting's `url`, which needs a Fathom login. The public `share_url` is
  never stored: it is a link anyone can open.
- **E-mail addresses are never stored**, neither in the text nor in metadata. Attendees appear as
  `Name (company.com)`, which is what lets a question about "Acme" find the call.
- **Node sets** group meetings for scoped recall: `fathom:internal` or `fathom:external`,
  `fathom:type:<meeting type>` and `fathom:team:<recorder's team>`.
- Rendering is deterministic: action items are sorted and nothing time-dependent is written, so a
  meeting that is fetched again but did not change keeps its document id and is not re-cognified.
- CRM matches are not ingested.

## How sync works

Fathom has no change feed and no deleted-meeting feed, so every run starts with a cheap
**inventory**: one walk over `GET /meetings` that asks only for action items (Fathom does not
count that as a heavy request) and keeps each meeting's `recording_id`, `created_at` and a
fingerprint of its action items. From it the run decides which meetings need the expensive
request (summary, transcript):

- meetings that were never ingested,
- meetings created within `recheck_days`: a summary can still be processing right after the call,
  and action items are ticked off in the days after it,
- meetings whose action items changed since the last run, **however old**. Completing an item on
  a call from last year is noticed without re-reading anything else.

Those are fetched with `created_after` / `created_before` windows planned from the inventory, so
one old meeting costs one request, not the history since it. A brand-new meeting that Fathom is
still processing (nothing in the selected sections yet) is held back instead of being ingested
as an empty shell; it is picked up once it has content or leaves the re-check window.

### Forgetting

A meeting that earlier runs ingested and the inventory no longer lists is emitted with the
`_deleted` hard-delete marker. dlt drops the row on `merge`, and cognee's `orphan_cleanup` removes
the meeting from the graph, vector and relational stores.

- **A second walk must confirm.** Fathom's cursors are page numbers, so a meeting deleted while
  the walk is running can shift the pages and hide a live one. A meeting is only forgotten if it
  is also missing from a second walk.
- **A failed listing forgets nothing.** The sync state is written only after the last page of the
  last pass, and every API error raises. dlt rolls the state back and memory stays as it was.
- **A mass delete is refused.** Losing access (the key's owner left a team, a deactivated user,
  narrower `teams`) looks exactly like deleting everything. If a run would forget more than 5
  meetings and more than `max_delete_fraction` of the known ones, it forgets nothing and reports
  `failed_delete_guard` in `source.cognee_sync_stats`. Pass `max_delete_fraction=1.0` once you
  know the deletion is intended.
- A meeting that stops being visible to the key (unshared, or the recorder left) is forgotten
  like a deleted one, and ingested again if it comes back.

`source.cognee_sync_stats` reports `listed`, `fetched`, `deferred`, `deleted` and `failed`.

### Rate limits

Fathom allows 60 requests a minute, and 30 a minute (sometimes 5) for the heavy ones. On HTTP 429
the client waits for `Retry-After` (at most 120 s a time, 6 times per request) and then raises;
5xx and network errors are retried 3 times with backoff. A run that raises leaves memory and
state untouched, so run it again later. The wait happens inside the sync, which holds cognee's
staging lock, so run a large first sync on its own.

Pages hold 10 meetings and the page size cannot be raised, so the inventory of an account with
3,000 meetings is 300 requests, about 5 minutes, on every run. For a big first sync, backfill in
slices with `since` (newest first: `since=` a year ago, then two years, ...); a smaller `since`
only adds meetings.

## Limitations

- Without `teams` or `recorded_by`, Fathom leaves out calls recorded by users outside your
  organization. Pass `recorded_by=[...]` to include them; that is Fathom's rule, not a filter here.
- An API key sees the meetings recorded by or shared with its owner, not other people's private
  calls. Give every API key its own `resource_name` and, ideally, its own dataset: two keys see
  different meetings and would forget each other's.
- Summary templates: the API returns the account's default summary only.
- OAuth apps cannot request `include_summary` / `include_transcript` on `/meetings`; this
  connector uses API keys.
- The dataset holds the content of your meetings. With cognee's access control (on by default)
  only the user who ran the sync, and users granted permission, can read it; keep it in its own
  dataset and share it only with people allowed to see those calls.

## Testing

```bash
uv run pytest tests/
```

The tests fake the Fathom API with [`responses`](https://github.com/getsentry/responses) (no live
key) and cover rendering, pagination, the incremental windows, the re-check, forget-on-delete,
the failed-listing and mass-delete guards, and 429 / 5xx handling. They include offline
end-to-end runs through a real `dlt` merge and `cognee.add`, and through `cognify` with a mocked
LLM, proving a deleted meeting is removed from the graph and not only from staging.
