# cognee-community-connector-lattice

A Lattice data-source connector for [cognee](https://github.com/topoteretes/cognee): sync the
**goals**, **feedback** and **review cycles** of a [Lattice](https://lattice.com) workspace into
memory, and forget a record when it is deleted, archived or made private upstream.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each goal (with its progress
updates), each feedback, each review cycle and each person's submitted reviews in a cycle you
select becomes a short **document** that flows through cognify. Documents of one kind share a node
set (`lattice:goals`, `lattice:feedback`, `lattice:review_cycles`, `lattice:reviews`), so you can
scope a search to one of them.

> **Performance feedback is among the most sensitive data a company has, so it is off by
> default.** Out of the box the connector reads goals and review-cycle metadata only. Feedback and
> review responses each need an explicit opt-in, see [Privacy](#privacy).

## Requirements

- cognee **1.6.4** (pinned). The connector relies on document mode with table-scoped cleanup
  and per-row node sets (`DOCUMENT_SYNC_VERSION` 2).
- Python 3.11 to 3.13.
- A Lattice workspace and an API key (see [Setup](#setup)).

## Install

```bash
uv pip install cognee-community-connector-lattice
# or, from this monorepo:
cd packages/connector/lattice && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_lattice import lattice_source

source = lattice_source(
    region="emea",  # "us" (default) or "emea", the data residency of your workspace
)  # the key comes from LATTICE_API_KEY, or pass api_key="..."

await cognee.remember(source, dataset_name="lattice")

answer = await cognee.recall("Which goals are off track?", datasets=["lattice"])
```

Run the same `remember` call again whenever you want to sync. Use the default
`write_disposition="replace"`; the source refuses anything else (see
[How a sync works](#how-a-sync-works)). `examples/example.py` shows the full flow.

| Argument | Meaning |
|---|---|
| `api_key` | A Lattice API key. Falls back to `LATTICE_API_KEY`. |
| `region` | `"us"` (default) or `"emea"`. Falls back to `LATTICE_REGION`. A workspace answers only on its own region's host, so a wrong region looks like a 401. |
| `include_goals` | Sync goals and key results. Default `True`. |
| `goal_states` | Which goals, by state: `Draft`, `Active`, `Ended`, `Archived`. Default `("Active", "Ended")`. |
| `include_goal_updates` | Add each goal's progress updates (status and comment) to its document. Default `True`. One request per goal. |
| `include_private_goals` | Also sync goals Lattice marks private. Default `False`. |
| `include_feedback` | Sync continuous feedback. **Default `False`.** |
| `feedback_only_public` | Only feedback shared with the whole company (`onlyPublic`). Default `True`; `False` also takes private feedback the key's owner can see. |
| `include_review_cycles` | One document per launched review cycle: name, stage and dates, no responses. Default `True`. |
| `review_cycle_ids` | Cycles whose submitted review **responses** to sync, one document per reviewee. **Default none.** |
| `resource_name` | Prefix for this workspace's staging tables. Default `lattice`; give each workspace its own when several sync into one dataset. |
| `client` | A `LatticeClient`, for a custom base URL, transport or retry behaviour. |

## Setup

### 1. Get an API key

Lattice API keys are created and managed in the Lattice admin dashboard
([authentication](https://developers.lattice.com/reference/authentication)). Lattice states that
**a key carries the privileges of the user who created it.** The connector only ever sends `GET`
requests, but it can read whatever that user can, so create the key from an account that can see
only what you want in memory (a dedicated, low-privilege user is the safest choice). The key is
sent as a bearer header and is never logged or included in an error message.

### 2. Pick the region

Lattice serves each workspace from the base URL of its data residency: `api.latticehq.com` (US) or
`api.emea.latticehq.com` (EMEA). Pass `region="emea"` or set `LATTICE_REGION=emea` for an EMEA
workspace. If you are unsure, Lattice's docs point to customercare@lattice.com.

### 3. Choose what to sync

Goals and review cycles are on by default. To add review responses, pass the ids of the cycles
you want. List them with `LatticeClient().list_all("/v1/reviewCycles")`, which the example prints.
An id that does not exist raises an error listing the ones that do, instead of being skipped,
because skipping it would forget what it synced before.

## Privacy

This sends the text of what you select to your configured LLM and embedding providers during
cognify. Defaults are chosen so that nothing sensitive is ingested by accident:

- **Feedback is off.** `include_feedback=True` turns it on, and even then only feedback shared
  with the whole company is read (`onlyPublic=true`, re-checked on every row in case the server
  ignored it). `feedback_only_public=False` is a second, separate opt-in for private feedback.
- **Review responses are off.** They need the cycle's id in `review_cycle_ids`. Only reviews a
  reviewer has **submitted** are read, so a draft answer, a declined request or a response your
  key may not see never enters memory.
- **Reviewers are never named.** A review document says "Peer review" or "Downward review", not
  who wrote it, because peer reviews are often anonymous to the person they are about. The person
  being reviewed is named.
- **Private and draft goals are skipped.** A skipped private goal's name is not leaked through
  its children's "Parent goal" line either. `include_private_goals=True` and
  `goal_states=[..., "Draft"]` opt in.
- **People are only named.** A `/v1/users` listing (read once per kind that needs names) turns
  user ids into names, deactivated users included so an old goal still names its owner. Emails, birthdates, job levels and other
  profile fields are not ingested.
- **Narrowing the scope forgets.** Switching feedback off, dropping a cycle id or making a goal
  private removes those documents from the graph and vector stores on the next sync.

Use a dedicated dataset so you can forget it all in one call.

## How a sync works

- **What is read.** Goals one `state` at a time (default `Active` and `Ended`), each goal's
  `/updates`, a `/v1/users` listing for names, and feedback, review cycles, reviewees, reviews and the
  text of each question as selected. All lists are read in pages of 100 with Lattice's cursor.
- **Incremental sync.** None of Lattice's goal, feedback or review-cycle listings can be filtered
  by `updatedAt`, and feedback has no `updatedAt` at all (only `createdAt`), so a sync cannot ask
  for "changes since last time". Each run lists everything in scope instead, and cognee does the
  incremental part by **content**: a document whose text did not change keeps its content-hash id
  and is not re-ingested or read by the LLM again; only new and changed documents are
  cognified. A goal's `updatedAt` only moves when the goal itself is edited (a check-in moves
  `lastUpdatedAt`), so the document includes the progress updates and a new check-in is picked up.
  A save that changed no text is sent again with an identical document and skipped the same way.
- **Forget-on-delete.** The API has no delete feed, so a record that was synced before and is no
  longer listed (deleted, archived, made private, or no longer selected) simply drops out of the
  snapshot. The source uses `write_disposition="replace"`, which makes cognee forget exactly the
  documents missing from it, from the graph and the vector stores. A goal deleted between its
  listing and its updates request is left out, so it is forgotten too. The source refuses any other
  write disposition: with `merge` nothing would ever be forgotten.
- **Errors never delete.** A failed or partial read (a rate limit that does not clear, a server
  error, a timeout, a cursor that never ends) raises before anything is loaded, so a hiccup is
  never read as "everything was deleted" and the next run starts from the same memory. Rate
  limits (429, `Retry-After`) and 5xx and network errors are retried with backoff first.
- **Rate limits.** Lattice allows 240 requests a minute. As a guide, 500 goals cost about 5 list
  requests plus 500 update requests (one per goal), and `include_goal_updates=False` removes
  the latter. 1,000 feedback items cost about 10 requests. A review cycle costs its reviewees
  and reviews in pages of 100, plus one request per distinct question.

## Limitations

- Everything in scope is listed on every run, because the API offers no change filter. Large
  workspaces should sync on a schedule, not continuously.
- Goal `tags`, `childGoals` (a child links to its parent instead), competencies, weighted scores,
  calibration details and the separate `/v1/updates` check-in feature are not ingested.
- Two people with the same name are indistinguishable in a document (ids are not rendered).
- Lattice's Talent API is documented as subject to change without notice.

## Tests

```bash
cd packages/connector/lattice
uv sync
uv run pytest
```

No Lattice account or API key is needed. The tests run against an in-memory fake of the Lattice
Talent API, modelled on Lattice's published API reference (`tests/fake_lattice.py`): bearer auth,
both regions, cursor pagination in both list shapes, 429 with `Retry-After`, the `state`,
`status` and `onlyPublic` filters and integer timestamps. `tests/test_lattice_cognee.py` runs the
real cognee pipeline (dlt, ingestion, cognify, orphan cleanup) with a mocked LLM and embeddings
and proves that a deleted, archived or de-scoped record leaves the graph, that an unchanged
re-sync makes no LLM call, and that a failed sync forgets nothing.

**Not yet verified against a live workspace:** Lattice has no public sandbox. Response shapes come
from its published OpenAPI schemas, so details the schemas do not pin down (for example, how a
real workspace answers `status=NULL` on `/v1/users`) are confirmed by a live run, not by these
tests.
