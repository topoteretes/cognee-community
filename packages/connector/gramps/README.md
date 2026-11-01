# cognee-community-connector-gramps

A [Gramps Web](https://www.grampsweb.org/) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync a family tree into memory, "ask my
family tree".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`), so you get
**incremental re-sync** (only documents whose content changed are re-processed) and
**forget-on-delete** (people and families deleted in Gramps Web are purged from memory on the
next sync) with no core changes.

## Requirements

- Python 3.10 to 3.14.
- cognee 1.6.3 or later (below 2), for table-scoped document cleanup and per-row node sets.
- A Gramps Web server (API 3.x; tested against 3.24) and an account that can read the
  tree. Any role works; see [Privacy](#privacy) for what the role changes.

## Install

```bash
uv pip install cognee-community-connector-gramps
# or, from this monorepo:
cd packages/connector/gramps && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_gramps import gramps_source

await cognee.remember(
    gramps_source(
        base_url="https://gramps.example.org",  # or GRAMPS_URL
        username="...",  # or GRAMPS_USERNAME
        password="...",  # or GRAMPS_PASSWORD
        resource_name="gramps_family_tree",
    ),
    dataset_name="family_tree",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by record id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole tree
)

answer = await cognee.recall(
    "Who were Mary Smith's grandparents, and where were they born?",
    datasets=["family_tree"],
)
```

Choose what becomes a document with `include=("people", "families", "places")` (people and
families by default) and narrow the sync to records carrying a Gramps tag with
`tags=["Family"]`. Re-running `remember(...)` with the same dataset syncs only what changed and
forgets what was deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required**: the add pipeline defaults to `"replace"`,
> which would wipe the synced tree on the second sync.

`resource_name` names one tree's sync: it keys the sync state and the cleanup of deleted
records. It defaults to `gramps_<12 hex>` derived from the server address and the user, so
pass your own when you want a readable name or sync two trees from the same server. A run
refuses state that was written for another tree. Pass `check_active` to let a host stop a sync
whose access was revoked; it is called before every request and before every row.

## What gets ingested

One document per person and per family (and per place, when included), with every handle
resolved to a name so the text reads like a family-group sheet:

- **People**: name, alternate names, gender, birth and death (date and place), parents and
  siblings, partners and children, other events with their role (occupation, residence,
  baptism, ...), associations, attributes, tags and notes, including the notes on their events.
- **Families**: the couple, relationship type, children (with adopted, step, ... relations),
  family events (marriage, divorce, ...), attributes, tags and notes.
- **Places** (opt-in): name, type, enclosing places, alternate names, coordinates and notes.

Relatives are written as `Name (born YEAR)` to tell namesakes apart. Events and notes are not
documents of their own: they are folded into the person, family or place they belong to,
which keeps every fact next to the people it is about. GEDCOM bookkeeping (`_UID`, `_UPD`) is
left out. Media, sources, citations and repositories are not read.

Each document links back to its page (`https://<server>/person/I0001`) and carries the node
sets `gramps:people` / `gramps:families` / `gramps:places` plus `gramps:tag:<name>` for each of
its tags, so a search can be narrowed with `node_name`.

## How sync + forget-on-delete work

A person's document names their relatives, places and events, and editing any of those does
not bump the person's own `change` time in Gramps. So a sync that changed anything reads the
whole tree (people, families, events, places, notes and tags, 500 objects per request with
only the fields needed) and emits only the documents whose rendered text changed, using a
fingerprint kept in dlt's per-resource state.

Most runs never get that far. Before reading the tree a run makes about 13 tiny requests: the
tree id, then for each object type its total count and the number of objects changed since the
newest change it saw last time. When nothing moved, the run ends there. A full pass still runs
at least every `full_sync_hours` (24 by default) as a safety net.

A listing is checked for consistency: if the total moved while it was paged through, or the
pages did not hold exactly that many objects, it is read again (up to 3 times) and the run
otherwise stops without syncing or forgetting anything.

A document leaves memory when:

- its record was **deleted** in Gramps Web. A record missing from the listing is confirmed in
  the transaction history (`/api/transactions/history/`), or, when the history does not show
  it, by asking for the record itself and getting a 404. A record that still answers, or whose
  check fails, is kept and re-checked on the next run;
- its record became **private** (unless `include_private=True`), lost the last tag of the
  `tags` filter, or its kind was dropped from `include`.

The forgotten document is emitted with the `_deleted` hard-delete marker, dlt drops it on
`merge`, and cognee's `orphan_cleanup` removes it from the graph, vector and relational stores.
Documents that mentioned the deleted person are re-emitted without them. Keep `resource_name`
and the dataset fixed across runs.

Requests that fail with 429 or 5xx (or a network error) are retried up to 5 times, honouring
`Retry-After` or backing off 1, 2, 4, 8 s. A single wait over 60 s, or more than 120 s of waits
in one run, aborts the run (nothing is lost or forgotten; the next run starts over). A refused
token is renewed with the refresh token, then by logging in again; a client reused across runs
keeps its tokens, since Gramps Web rate-limits logins.

## Limitations

- A full pass reads every person, family, event, place and note: about `objects / 500`
  requests (the 25,000-object demo tree takes about 55). Only the quick check runs when
  nothing changed.
- An edit made in the same second as the newest change of the previous sync can go unnoticed
  until the next full pass (`full_sync_hours`). Gramps stores `change` in whole seconds.
- Reading the transaction history needs the permission to view private records (every role
  from member up). A guest account cannot read it, so every deletion then costs one request,
  at most 200 a run; the rest are confirmed on the next runs.
- Media, sources and citations are not ingested, and events and notes have no documents of
  their own.
- Deletions propagate when `remember` runs in the foreground (`run_in_background=False`, the
  default), like every dlt connector.

## Privacy

- Records, names, event references and notes marked **private** in Gramps are left out by
  default, and the documents of other people do not mention them. `include_private=True`
  syncs them; only use it when everyone who can read the dataset may see private records.
  A user without the permission to view private records never receives them from Gramps Web
  in the first place.
- Living people are synced like everyone else unless they are marked private in Gramps. Mark
  them private, or narrow the sync with a tag, before sharing the dataset.
- With cognee's access control (on by default) only the user who ran the sync, and users
  granted permission on the dataset, can read it. Keep a tree in its own dataset.
- The password and the tokens stay in memory: they are never logged, stored in the sync state
  or shown in errors. Pass them through environment variables, not source code.

## Setup

1. Create (or reuse) a Gramps Web account for the sync. A read-only **member** account is
   enough: it reads the tree and the transaction history, and the connector still leaves
   private records out unless `include_private=True`. A **guest** account never receives
   private records at all, but confirms each deletion with its own request.
2. Export `GRAMPS_URL` (the server address, without `/api`), `GRAMPS_USERNAME` and
   `GRAMPS_PASSWORD`, plus your `LLM_API_KEY` like any other cognee run.
3. Run `python examples/example.py`.

## Verified against a real server

- **Public demo** (`demo.grampsweb.org`, Gramps Web API 3.24, Gramps 6.0, read-only): the
  first 50 people and one page of the other types rendered with no unresolved handles, the
  `change > N` filter and the transaction history behaved as the sync expects, and a second
  run ended after the 13-request quick check.
- **Self-hosted Gramps Web 3.24** with an empty tree, through `cognee.remember` (LLM mocked):
  adding people, events and a family; a no-op re-sync; editing an event (only its person
  re-emitted); making a person private (forgotten); deleting a person (confirmed in the
  history, removed from the graph).

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests fake the Gramps Web API (no server, no network) and include offline end-to-end runs
through a real `dlt` merge and through `cognee.remember`, proving a deleted person is removed
from the graph, not only from staging.
