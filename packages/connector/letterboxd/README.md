# cognee-community-connector-letterboxd

A Letterboxd data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync the films you watched, your ratings, reviews, diary and lists into memory,
so you can ask "which films did I rate highest, and what did I think of them?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each film becomes
one document (year, rating as stars, diary timeline with rewatches and tags,
full review text, list memberships) and each list becomes one document with its
ordered roster. Rows go through cognee's normal cognify pipeline via the
document-mode marker, the same way the Notion connector works.

## Requirements

Python 3.10 to 3.13 and cognee 1.6.3 or later (below 2). Older 1.4 releases skip orphan
cleanup once a table becomes empty, so the last deleted record would stay in memory. 1.6.3
handles that case.

## Install

```bash
uv pip install cognee-community-connector-letterboxd
# or, from this monorepo:
cd packages/connector/letterboxd && uv sync
```

## Usage

```python
import os

import cognee
from cognee_community_connector_letterboxd import letterboxd_source

await cognee.remember(
    letterboxd_source(export=os.environ["LETTERBOXD_EXPORT"]),
    dataset_name="letterboxd",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
    self_improvement=False,
)

answers = await cognee.recall(
    "Which films did I rate highest, and what did I think of them?",
    datasets=["letterboxd"],
)
```

`write_disposition="merge"` is required, and anything else is refused by the source
itself: every run re-reads the whole export snapshot, and the pipeline default
(`replace`) would wipe every earlier film on the second run.

## Setup: getting your export

Letterboxd has no public API, so the connector reads the data export instead:

1. On letterboxd.com go to **Settings → Import & Export → Request Export**.
2. Letterboxd emails you a ZIP (this can take anywhere from a few minutes to a day).
3. Pass either the `.zip` path or the folder you extracted it into as `export=`.
   The connector validates that the path exists and that `watched.csv` — the one
   required file — is readable, and fails with a clear error otherwise.

No credentials, tokens or network access are involved; parsing happens locally
with the stdlib `csv` and `zipfile` modules.

## What it syncs

| Export file | Becomes |
|---|---|
| `watched.csv` | One document row per film (the required file; every film's anchor) |
| `ratings.csv` | The film's rating, rendered as stars (10-scale `r` → `r/2` stars, half-steps allowed: 7.5 → ★★★½) |
| `reviews.csv` | The film's full review text |
| `diary.csv` | The film's watch timeline, sorted by date, with rewatch markers and tags |
| `lists/*.csv` | One document row per list (the ordered roster), plus list membership on each film row |
| `watchlist.csv` | Films on your watchlist get a row marked "On my watchlist" (file absent in older exports) |

Parsing is deliberately tolerant, because Letterboxd adds columns between export
versions: columns are matched **by name** (case-insensitive), unknown or extra
columns are ignored, and missing optional files or columns (`Tags`, `Date`,
`Rewatch`, whole CSVs) never crash a run. The only fatal case is a missing
`watched.csv`.

## How sync works

| Concern | Behaviour |
|---|---|
| Identity | The film's Letterboxd URI (`https://letterboxd.com/film/parasite/`) — the stable public id — is the primary key. Lists are keyed `list:<list name slug>`. |
| Incremental | Every run re-reads the whole snapshot and upserts by id under `merge`. Rows are deterministic (no ingestion timestamps anywhere), so an unchanged film keeps its content hash and is **not** re-cognified; only genuinely changed films (new rating, edited review, new diary entry, list moves) get re-cognified. |
| Forget-on-delete | The ids of the previous run are kept in dlt resource state. A film or list present then but absent from the current export is emitted as a `_deleted` hard-delete row, and cognee's `orphan_cleanup` forgets it. The state is written only after the last row of a run and only persisted after a successful load, so a failed run never corrupts it. |
| Removing a list | The list's row is deleted, and its films lose the `list:<slug>` node-set entry (their row content changes, so they are re-cognified without that link). |
| Node sets | Each film row carries `cognee_node_set`: the `list:<slug>` of every list it belongs to plus `decade:<YYYY>s` from its year, so films group in the graph without LLM extraction. List rows carry an empty node set. |
| Resource name | Defaults to `letterboxd_default`; pass `resource_name=` (lowercase letters, digits, underscores) to choose the table name and keep it fixed across runs. |

## Limitations (be honest)

- **The export is a snapshot.** Nothing is fetched live: ratings, reviews or
  diary entries you add today only arrive with your next export request, and
  removals (a film un-watched, a deleted review or list) are only detected when
  a later export no longer contains them.
- **No watchlist in older exports.** `watchlist.csv` only appears in newer
  exports; when absent, watchlist films are simply not in the data.
- **Switching exports merges by URI.** Nothing in the export identifies the
  account, so the sync state is keyed by film URI only. Pointing the same
  resource at another account's export merges it into the same table by URI
  key, and films the second export does not list are forgotten. Give each
  account its own `resource_name` (and dataset) to keep them apart.
- **List names come from file names.** The export's `lists/*.csv` files carry
  no list-name column, so the file name is the list's name and id slug.
- **Ratings below half a star or malformed rows** (no URI or name) are skipped
  and counted in `source.cognee_sync_stats["skipped"]`.

## Resetting a sync

The known-id state lives in dlt's pipeline state (under `~/.dlt/pipelines`, or
`$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning cognee does
not reset it, so the next sync would still diff against the old known ids. To
re-sync from scratch, delete that pipeline folder as well. The state is kept
per `resource_name` (`PIPELINE_SCOPE_ATTR`).

## Privacy and security

- The connector is read-only and offline: it opens the export you pass and
  nothing else. No network access, no credentials.
- Hosts can pass `check_active=`, a callable run before the export is read and
  around every row; raising from it stops the sync without corrupting state.
- After a run, `source.cognee_sync_stats` holds counts only: `films`, `lists`,
  `deletions`, `skipped` and `failed`.
- Anyone who can read the cognee dataset can recall your film history and
  review texts, which are personal data. Keep them in their own dataset.

## Testing

```bash
uv run pytest tests/ -q
```

No export, account or API key is needed. `tests/fake_letterboxd.py` writes real
export ZIPs with the exact CSV shapes of a real export (a film with a rating but
no review, a film with a full review, a film watched twice with a rewatch and
tags, a film in two lists, a film only in watched.csv, a 3-film list).

- `test_letterboxd.py` covers rendering and determinism, the star conversion,
  the rewatch timeline, list rosters, node sets, first run and edited-review
  upserts, film and list deletion, state timing, tolerant parsing (extra
  columns, missing optional columns, a watched.csv-only export, a missing
  watched.csv, extracted folders), the factory validation and the dlt resource
  wiring (merge, `id` primary key, hard delete, `DOCUMENT_SOURCE_ATTR`, stats,
  `check_active`, service injection), and the row-to-document mapping in cognee.
- `test_letterboxd_delivery.py` drives a real dlt pipeline into SQLite: rows
  load, an edited review replaces the row's content, a deleted film and a
  deleted list physically disappear from the table (what cognee's
  forget-on-delete compares under `max_rows_per_table=0`), and a `replace` run
  is refused.
