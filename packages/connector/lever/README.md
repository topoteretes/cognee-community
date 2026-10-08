# cognee-community-connector-lever

A [Lever](https://www.lever.co/) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync job postings — and, opt-in, interview
feedback and notes — into memory. "Ask my hiring pipeline".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Rows are
ingested as **normal documents** (they flow through cognee's cognify entity-extraction
pipeline, not the deterministic dlt-row path) via cognee's document-mode marker, with
**incremental re-sync** and **forget-on-delete**.

## Install

```bash
uv pip install cognee-community-connector-lever
# or, from this monorepo:
cd packages/connector/lever && uv sync --all-extras
```

Requires a cognee release with document-mode (`cognee>=1.4.0`).

## Usage

```python
import cognee
from cognee_community_connector_lever import lever_source

await cognee.remember(
    lever_source(posting_states=["published", "internal"]),  # LEVER_API_KEY from env
    dataset_name="lever",
    primary_key="id",
    write_disposition="merge",  # REQUIRED: incremental upsert by id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Which open roles need Kubernetes experience?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["lever"],
)
```

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would drop every posting that did not change since the last run.

### Choosing what to ingest

| Argument | Default | What it does |
| --- | --- | --- |
| `include_postings` | `True` | Job postings: title, team/location/commitment, description, requirement lists, closing. |
| `include_feedback` | `False` | Interview feedback forms (restricted — see below). |
| `include_notes` | `False` | Non-secret candidate notes (restricted — see below). |
| `posting_states` | all | Keep only postings in these states, e.g. `["published"]`. |
| `posting_ids` | all | Only sync feedback/notes for opportunities on these postings. **Recommended** when you enable feedback/notes: every in-scope opportunity is re-read each run, so new feedback is never missed (see below). |
| `include_confidential` | `False` | Also ingest confidential postings/opportunities. |
| `full_refresh` | `False` | Ignore the stored cursor and re-read everything in scope. Use it once after narrowing `posting_states` so postings that fell out of scope are forgotten. |

## Candidate data is restricted by default

Feedback and notes describe real people, so they are **off unless you opt in**, and even
then the connector never ingests candidate contact data — no name, email, phone, links,
headline or location. Each opportunity becomes one document holding only the text that
interviewers wrote (feedback answers and non-secret notes), keyed by the opportunity id.

* **Names in free text are pseudonymized.** Interviewers often write the candidate's name in
  their feedback, so the candidate's full name, each part of it ("Jane", "Jane's"), their
  emails and phone numbers are replaced with a stable label such as `Candidate 3f2a9c1e`.
  The label is a one-way hash of the Lever contact id: one person's documents stay linked in
  the graph without revealing who they are, and different candidates never merge into one
  "candidate" entity. Matching is whole-word and errs on the side of privacy (a name that is
  also a common word is still replaced). Names of *other* people mentioned in the text,
  misspellings, nicknames and differently formatted phone numbers are not caught.
* **Anonymized candidates are forgotten.** When Lever anonymizes a candidate (`isAnonymized`,
  for example after a GDPR erasure request), their document is deleted on the next sync.
* Secret notes, deleted forms and confidential records are skipped.

Keep this data in its own dataset and apply your organisation's retention rules
(`cognee.forget` removes a dataset in one call).

## How sync + forget-on-delete work

* **Incremental cursor** — Lever's `updated_at_start` filter on `/postings` and
  `/opportunities`. The cursor is the time the previous run *started* (minus a 5-minute
  overlap), stored in dlt's per-resource state, so re-running `remember(...)` only reads what
  changed. Unchanged documents keep a stable content-hash `data_id` and are not re-cognified.
* **Forget-on-delete** — ids from Lever's delete feeds (`/postings/deleted`, read in
  ≤ 29-day windows as the API requires, and `/opportunities/deleted`) are emitted with the
  `_deleted` hard-delete marker. So are records that left the selected scope (a posting
  whose state is no longer selected, a record that became confidential, an opportunity whose
  feedback/notes were all deleted). dlt removes those rows on `merge` and cognee's existing
  `orphan_cleanup` purges them from the graph + vector + relational stores.
* **Failure safety** — rate limits (429) and 5xx responses are retried with backoff; any
  other error aborts the run *before* the cursor advances, so nothing is skipped or forgotten
  by mistake.

### Keeping feedback and notes fresh

Lever does **not** bump an opportunity's `updatedAt` when feedback or a note is added, edited
or deleted, so an `updated_at_start` cursor alone would miss new interview feedback. The
connector therefore has two modes for opportunities:

* **Scoped rescan (recommended)** — pass `posting_ids=[...]`. Every opportunity on those
  postings is re-read on each run. New, edited and deleted feedback/notes are always picked
  up; unchanged documents render to identical content and keep their `data_id`, so nothing
  is re-cognified. Opportunities that leave the scope are forgotten. (If the listing comes
  back empty while opportunities were known, forgetting is skipped for that run, since that
  almost always means a transient failure or a mistyped posting id.)
* **Account-wide incremental** — no `posting_ids`. Only opportunities whose `updatedAt` moved
  are re-read, which keeps large accounts cheap; feedback on an otherwise untouched
  opportunity is picked up on its next update, or with `full_refresh=True`.

## Setup

1. In Lever, go to **Settings → Integrations and API → API credentials** and generate an API
   key. Read-only access to postings (and to opportunities, feedback and notes if you opt in)
   is enough — the connector only issues `GET` requests.
2. Export it as `LEVER_API_KEY` (or pass `api_key=...`), plus your `LLM_API_KEY` like any
   other cognee run.
3. Run `examples/example.py`.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Lever API (no live key) and cover rendering, pagination, the incremental
cursor, delete feeds and scope changes, retry/abort behaviour, the restricted-data guarantees
(including name pseudonymization and anonymized candidates), the scoped rescan that catches
feedback added without an `updatedAt` change,
a real `dlt` merge proving a tombstone physically removes the row, and a `cognee.add` run
proving deleted postings are forgotten while unchanged ones keep their `data_id`.
