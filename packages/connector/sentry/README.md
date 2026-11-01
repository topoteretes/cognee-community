# cognee-community-connector-sentry

A [Sentry](https://sentry.io) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync your organization's **grouped
issues** — their status, their counts and a bounded sample of their stack traces — into
memory, so questions like "what has been failing in checkout since the deploy, and did we
ever resolve it?" have something to recall.

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each issue is ingested as
a document (cognee's document mode), so it goes through cognify entity extraction.

## Requirements

Python 3.10 to 3.14, and cognee 1.6.4 or later (for per-row node sets).

## Install

```bash
uv pip install cognee-community-connector-sentry
# or, from this monorepo:
cd packages/connector/sentry && uv sync
```

## Setup

1. In Sentry, an owner or manager creates an **internal integration** (Settings →
   Developer Settings → New Internal Integration). Internal integrations are part of
   Sentry's integration platform and their token belongs to the organization, not to one
   person, so the sync does not break when someone leaves.
2. Give it only these permissions: **Organization: Read**, **Project: Read**,
   **Issue & Event: Read** (`org:read`, `project:read`, `event:read`, the scopes Sentry's
   API reference lists for these endpoints; not re-derived by trial on the live org). A
   personal auth token with the same scopes works too.
3. Note the data region: tokens are region-bound, so pass `host="de.sentry.io"` for the
   EU region, or the base URL of a self-hosted install.

## Usage

Inside an async function (or a notebook):

```python
import cognee
from cognee_community_connector_sentry import sentry_source

await cognee.remember(
    sentry_source(
        auth_token="sntrys_...",
        org_slug="acme",
        host="sentry.io",  # or "de.sentry.io", or a self-hosted base URL
        project_slugs=["backend-api"],  # optional; default is every readable project
    ),
    dataset_name="sentry",
    primary_key="id",
    write_disposition="merge",  # required: the add pipeline defaults to "replace"
    self_improvement=False,  # no enrichment pass on every scheduled sync
)
```

See `examples/example.py` for the full flow, including a `GRAPH_COMPLETION` search.

| Argument | Default | |
| --- | --- | --- |
| `auth_token` | required | internal integration token, or a user auth token |
| `org_slug` | required | the organization to sync |
| `host` | `sentry.io` | data region host, or a self-hosted base URL |
| `resource_name` | `sentry_<org slug>`, plus a selection suffix when `project_slugs` is set | scopes sync state and cleanup; one per organization. Must be lowercase snake_case: dlt normalizes a table name, so `Sentry_A` and `sentry-a` would write to one table while keeping two states |
| `project_slugs` | all readable | only sync these projects |
| `event_sample` | 1 | events sampled per issue for the stack trace (0–3); a slot repeating an event already sampled is skipped |
| `max_frames` | 5 | in-app frames kept per sampled event (1–50, so the document stays bounded) |
| `stats_period` | none sent | `statsPeriod` for the listing, a count and a unit, e.g. `"14d"` |
| `max_requests` | 2000 | reads one run may make (a read retried through an outage counts once); a big backfill finishes over several runs |

Changing `event_sample` or `max_frames` re-renders every document on the next run,
because those change what a document says. Changing `stats_period` forces a re-render
pass, but only rewrites the documents whose text actually changed.

`project_slugs` is different, because the default resource name includes the selection.
That is deliberate: two sources for the same organization with different selections in one
dataset would otherwise share one state and tombstone each other's issues on every run,
for ever. The cost is that **editing `project_slugs` on a source that uses the default name
starts a fresh scope**: a new dlt table, and the previous selection's documents left behind
in the dataset rather than forgotten. To have issues that leave the selection forgotten
instead, pass a fixed `resource_name` and keep it across the change — then the documents it
keeps are not re-rendered (their text did not change), issues that leave are forgotten, and
issues that join are ingested.

## What gets ingested

One document per **grouped issue**, never one per raw event — that is the volume trap this
connector exists to avoid:

```
ValueError: database is locked
Project: backend-api · Level: error · Status: unresolved (ongoing) · Short id: BACKEND-API-42
Seen: 412 events, 37 users affected, first seen 2026-10-18, last seen 2026-10-27
Culprit: app/views.py in handle_request
Assigned to: Sam Lee
Unhandled: the code did not catch this.
Event sample (recommended): ValueError: database is locked
  in handle_request (app/views.py:42)
  in _locked_write (app/db.py:117)
https://acme.sentry.io/issues/4857392011/
```

The stack trace comes from a bounded sample of the issue's events (by default one: the
`recommended` event; then `latest`, then `oldest` — Sentry's three fixed per-issue event
slots). Only frames Sentry marks `in_app` are kept, at most `max_frames` of
them; when nothing is marked in-app (minified or native traces) the three innermost frames
are used and marked as such. Breadcrumbs, request bodies, contexts and tags are **not**
ingested: that is where both the volume and most of the PII live.

Each document carries the node sets `sentry:project:<slug>` and `sentry:level:<level>`, so
a search can be scoped to one project or to errors only. There is deliberately **no node
set per issue**: the count of `NodeSet` nodes is bounded by the organization's projects
times Sentry's six levels, not by the number of issues, so a 50,000-issue organization
still has a handful of them and `recall(node_name="sentry:project:backend-api")` stays a
real filter rather than a per-record lookup. The project node set uses the project *slug*,
which a rename changes — after one, issues move to the new node set and the old one is
left empty. The sync itself survives a rename: the selection is matched on project **id**,
and when a selected slug stops resolving the last id it resolved to is reused, with a
warning asking you to update `project_slugs`. A slug that never resolved is still an
error, since syncing the whole organization instead would ingest projects you did not ask
for.

## How sync works

**Every run lists every issue.** Sentry writes no `updated` field, and resolving,
archiving, assigning or re-prioritising an issue does not move `lastSeen` (its
`update` handler writes `status`, `substatus`, `priority` and `resolved_at` only). So no
time-based cursor can catch a status change, and the full listing is what deletion
detection needs anyway. It is cheap: 100 issues a page. A document is rewritten only when
its text actually changed, and an issue's event sample is re-read only when `lastSeen`
moved, so a resolve costs one listing page and no event request.

Every listing sends an **explicit empty `query=`**. Sentry's issue listing otherwise
defaults to `is:unresolved`, and a connector that relied on that default would see a
resolved issue vanish from the listing and wrongly forget it. The status stays on the
document instead, so "is this still unresolved?" remains answerable; an archived issue
comes back as `status: ignored` with an `archived_*` substatus.

**Deletes.** `DELETE /issues/{id}/` answers `202 Accepted` and queues the deletion; the
issue then 404s (confirmed live) and stops appearing in the listing. A stored issue
missing from a *complete*
listing therefore gets one `GET /issues/{id}/`, and is emitted with the `_deleted`
hard-delete marker only when

* Sentry answers 404 with its missing-resource body, or
* Sentry answers 200 with a **different** `id` than the one asked for: it keeps a
  redirect from a merged-away issue to the surviving one, so that old id is gone too.

Nothing else deletes. A listing cut short by the request budget or a 429 queues no checks;
a 401 (the only thing a revoked token answers) and a 403 abort the run; a 5xx or network
failure is retried and then raised. dlt commits neither rows nor state for a failed run.

**Retention is not deletion — and this is the one behaviour taken on trust.** Sentry keeps
events for a plan-dependent retention window (30 days on the free Developer plan; see
Sentry's data-retention documentation for the paid plans), and the issue listing is a query
over events. So an issue whose events have all aged out is expected to
drop out of the listing while `GET /issues/{id}/` still answers 200 for it. That reasoning
is **inferred from Sentry's retention documentation and the shape of the listing; it was
not reproduced**, because reproducing it means waiting out 30 days on a live organization.
The connector therefore refuses to read "missing from the listing" as "deleted" on its own:
a missing issue is re-read with a `GET`, and only a confirmed 404 (or a merge redirect)
tombstones it. An issue that answers 200 is kept with its last known state, a line in the
document saying so, and a weekly re-check instead of a per-run one. If the inference is
wrong, the failure mode is a stale document, never a wrongly forgotten one.

**Rate limits** are per caller **and per endpoint**. An internal integration's token belongs
to the organization, so anything else syncing with the same token is expected to share the
budget (inferred, not measured). Budgets read off the headers on a live organization on
2026-10-28, with the window advancing about a second per call:

| Endpoint | Budget per window |
| --- | --- |
| organization issue listing | 10 |
| issue detail (`GET`/`PUT`/`DELETE /issues/{id}/`) | 5 |
| issue event detail (`events/{slot}/`) | 15 |
| concurrent requests (as reported on these endpoints) | 25 |

Reading those budgets as per-second (the window advanced about a second per call) gives an
upper bound of roughly 1,000 issues a second for a backfill; sustained throughput was not
measured. Every issue whose `lastSeen` moved costs one event request against the separate
15-per-window budget. The deletion checks are the tight one — they use the 5-per-window
issue-detail budget, so an
organization that drops a few thousand issues at once spends the checks over several runs.
`max_requests` (default 2000) is what actually ends a run first on a large organization.

The client reads `X-Sentry-Rate-Limit-Remaining` and waits for `X-Sentry-Rate-Limit-Reset`
(an epoch second, confirmed live) once the window's budget is used up, and backs off to the
same header on a 429. Those waits are tracked **per endpoint**, matching how Sentry meters:
spending the issue-detail budget of 5 does not stall the listing, which has its own 10. The
three event slots share one key, because they share one budget. A wait longer than 60 seconds cuts the run short instead of blocking
cognee's dlt worker, and so does a run whose waits add up past 300 seconds — one short
wait per request would otherwise still hold that worker for hours. Either way the next
run resumes at the listing page it was reading.

**Pagination** reads Sentry's `Link` header. Sentry always sends a `rel="next"` link, on
every page including the last, and marks the end of a listing with `results="false"` on it.
dlt's `HeaderLinkPaginator` does not check `results`, so driving it with this API would loop
forever — the connector parses the header itself and stops on `results="false"`.

**Why a hand-written HTTP client and not dlt's `RESTClient`.** Two reasons, in order of
weight. First, the paginator had to be written anyway (see above), which is most of what
`RESTClient` would have contributed. Second, `RESTClient` is built on `requests`, and every
test in this package drives the connector through `httpx.MockTransport` so the whole client
path — headers, retries, rate-limit waits, pagination — runs offline exactly as it does
against Sentry; a `requests`-based client cannot be driven that way. So the client here is a
small `httpx` one (about 200 lines), in the style of cognee's other `httpx` connectors.

### Sync stats

`source.cognee_sync_stats` holds the counts of the last run, as plain ints: `scanned` (issues
read from the listing), `skipped` (unchanged, no document rewritten), `deleted` (tombstoned),
`failed` (issues that failed on their own error) and `aged` (issues outside the listing window
that Sentry still serves). `completed` is the one key that is not a count: `True` only when the run read the whole
listing and drained its deletion checks. It is always present and starts `False`, so a run
that died part way through never reads as finished. `abandoned` appears once an issue has been given up
on; `failed_budget` or `failed_rate_limit` appears when the run stopped early for that reason,
and `failed_auth` when a refused token ended it.

**A run stopped by the request budget or by a rate-limit wait longer than 60 seconds returns
normally**, keeping what it read — `cognee.remember(...)` succeeds, and only `completed` and
the `failed_*` keys say otherwise. A caller that needs to know whether a sync finished has to
read them; `examples/example.py` shows the check. Errors that are not about one issue (a
refused token, a malformed listing, an outage) raise instead, and dlt then commits neither
rows nor state.

### Limitations

- One run reads the whole issue listing. With 50,000 issues that is 500 requests before
  any event sample; `max_requests` bounds it and the next run resumes where it stopped.
- A deleted issue is only noticed after a complete listing, so on a very large
  organization a forget can take as many runs as the backfill does.
- Issues are read through the organization issue listing (`event:read`, per Sentry's API
  reference); the `project_slugs` filter costs one extra request per run to resolve slugs to
  ids (`project:read`), and then sends them as repeated `project=<id>` params.
- Sync state keeps a fingerprint, `lastSeen` and the rendered sample lines (at most
  `max_frames` short lines) per issue, rewritten on every load. A backfill that spans
  several runs also keeps the ids listed so far, so that a resumed listing knows what it
  has seen; on a very large organization that list is the biggest thing in the state until
  the listing completes and it is dropped.
- **The event sample is capped at 3, and defaults to 1.** The connector samples only
  Sentry's three fixed per-issue event slots — `events/recommended/`, `events/latest/` and
  `events/oldest/` — because those are stable between runs; a paged walk of an issue's
  events is deliberately out of scope, so three is the ceiling. The default is 1 because the
  point of this connector is one document per *grouped* issue: sampling more events costs a
  request each, re-renders the document whenever `lastSeen` moves, and adds stack frames that
  are usually the same trace again.
- **Two organizations in one dataset need two `resource_name`s.** The default is
  `sentry_<org slug>` (plus a selection suffix), which already separates them; pass `resource_name` only if you have a
  reason to, and give two sources names that differ by more than case or punctuation —
  `Sentry_A` and `sentry-a` normalize to the same dlt table while keeping separate state.
  The same applies to one organization synced from two hosts (regions).
- **Who can read the dataset.** The documents carry issue titles, culprits, source file
  names and line numbers, assignee names, short ids and permalinks — i.e. fragments of your
  source tree and the names of the people on call. Anyone who can read the cognee dataset can
  read all of that, for every project in the organization. Give the dataset the same audience
  as the Sentry organization, and keep it in a dataset of its own. Breadcrumbs, request
  bodies, contexts, tags and user records are never read, which is where the rest of the PII
  would be.
- Issues permanently failing their own error (a 403, an unrecognised 404) are given up on
  after five runs and then re-read weekly; their documents are kept, never deleted, and
  `cognee_sync_stats["abandoned"]` counts them.
- Only ingestion. Sentry's issue webhooks fire on created, resolved, assigned, archived
  and unresolved — there is no delete event — so they could not drive forget-on-delete,
  and sync is pull-based.

## Verified against a real organization vs. taken from the docs

Everything in `tests/fake_sentry.py` is shaped by responses recorded from a live
organization on 2026-10-28, and the sync itself was run against it on 2026-10-29.

**Measured on the live API:** that the default listing hides resolved issues while
`?query=` does not; that resolving an issue leaves `lastSeen` untouched; that `count`
arrives as a *string*; that archiving gives `status: ignored` with an `archived_*`
substatus; that `DELETE` answers 202 and a later `GET` answers 404 with
`{"detail": "The requested resource does not exist"}` — the same body an id that never
existed gets; that a merged-away id answers **200 with the survivor's `id`**, not a 404
or a 302; that a bad token answers 401 `{"detail": "Invalid token"}` on both the listing
and the detail, never an empty list; that `Link` carries `rel="next"` on every page and
marks the end with `results="false"`; that `X-Sentry-Rate-Limit-Reset` is an epoch
second; that the per-endpoint budgets are 10 (listing), 5 (issue detail) and 15 (event
detail) with the window advancing about a second per call; and that the listing accepts
the repeated numeric `project=` filter after slugs are resolved to ids.

One caveat on how that was measured: the 2026-10-28 recordings went through the
*organization-scoped* `/organizations/{org}/issues/{id}/` path, while this connector calls
the unscoped `/api/0/issues/{id}/`. Both were re-probed on 2026-10-29: the 404 body is
byte-identical and the unscoped detail reports the same budget of 5 per window.

**Taken from Sentry's documentation and reasoning, not reproduced:** the scopes listed
under Setup; the retention window of each plan; and, the one that matters, that an issue
whose events have aged out drops from the listing while `GET /issues/{id}/` still answers
200. Reproducing that last one means waiting out 30 days on a live organization. The
connector is built so that if the inference is wrong the failure mode is a stale
document, never a wrongly forgotten one — a missing issue is always re-read by id, and
only a confirmed 404 or a merge redirect tombstones it.

## Testing

```bash
uv run pytest tests/
```

No network or LLM key needed; the fake API in `tests/fake_sentry.py` serves Sentry's
response shapes, `tests/test_sentry_dlt.py` drives a real dlt pipeline,
`tests/test_sentry_forget.py` runs the cognee pipeline end to end with a mocked LLM, and
`tests/test_sentry_example.py` runs `examples/example.py` against the fake.
