# cognee-community-connector-goodreads

A Goodreads data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your library export — shelves, ratings, reviews and your full re-read
timeline — into memory, so you can ask "which books did I read about systems
design, and what did I think of them?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each book becomes
one document row (title, authors, year, cleaned ISBN, shelves, a status line,
the read timeline with per-read rating and review, average rating, publisher,
binding, pages, owned copies, private notes). Rows go through cognee's normal
cognify pipeline via the document-mode marker, the same way the Notion
connector works, and carry node sets (`shelf:<slug>`, `author:<slug>`) so books
link in the graph by shelf and author without LLM extraction.

## Requirements

Python 3.10 to 3.13 and cognee 1.6.3 or later (below 2). Older 1.4 releases skip orphan
cleanup once a table becomes empty, so the last deleted record would stay in memory. 1.6.3
handles that case.

## Install

```bash
uv pip install cognee-community-connector-goodreads
# or, from this monorepo:
cd packages/connector/goodreads && uv sync
```

## Usage

```python
import os

import cognee
from cognee_community_connector_goodreads import goodreads_source

await cognee.remember(
    goodreads_source(export=os.environ["GOODREADS_EXPORT"]),
    dataset_name="goodreads",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
    self_improvement=False,
)

answers = await cognee.recall(
    "Which books did I read about systems design, and what did I think of them?",
    datasets=["goodreads"],
)
```

`write_disposition="merge"` is required, and anything else is refused by the source
itself: every run re-reads the whole export snapshot, and the pipeline default
(`replace`) would wipe every earlier book on the second run.

## Setup: getting your export

Goodreads **retired its public API in 2020**, so the official library export is the
canonical access path — there is no auth, token or network access involved:

1. On goodreads.com go to **Settings → Import/Export → "Export Library"** and wait
   for the CSV (usually `goodreads_library_export.csv`) to download.
2. Pass the CSV path as `export=`. The connector validates that the path exists
   and that the file is a readable CSV with `Book Id` and `Title` header columns,
   and fails with a clear error (saying where to download the export) otherwise.

Parsing is local, read-only and stdlib-only (`csv`, `html.parser`).

## What it syncs

| Export column | Becomes |
|---|---|
| `Book Id`, `Title`, `Author`, `Author l-f` | The row key (`book:<Book Id>`), title (truncated at 80 chars), author line, `author:<first-last-slug>` node set |
| `Additional Authors` | An "Additional authors:" line when present |
| `Original Publication Year` / `Year Published` | A "Year:" line (original preferred, fallback used) |
| `ISBN` / `ISBN13` | A cleaned "ISBN:" line (the export's `="..."` escaping stripped; ISBN13 preferred) |
| `Exclusive Shelf` + `Bookshelves` | "My shelves:" as a comma list, a status line ("Read" / "Currently reading" / "To read"), and `shelf:<slug>` node sets |
| `Date Read` + repeated `My Review` / `My Rating` groups | The read timeline: one entry per non-empty date, sorted, each with that read's rating (`★ x/5`, only when rated — 0 means unrated) and full review text (HTML stripped) |
| `Read Count` | Rendered with the single date when a book was re-read but only one `Date Read` column exists |
| `Average Rating`, `Publisher`, `Binding`, `Number of Pages`, `Owned Copies`, `Private Notes` | One line each, only when non-empty |

Parsing is deliberately tolerant, because Goodreads adds and renames columns
between export versions: columns are matched **by name** (case-insensitive,
ignoring spacing, underscores and numeric suffixes — `Date Read`, `date_read`
and `Date Read 2` are the same family), unknown or extra columns are ignored,
the UTF-8 BOM is stripped, and rows without a `Book Id` and `Title` (a metadata
footer, a trailing empty line) are skipped and counted in
`source.cognee_sync_stats["skipped"]`. The only fatal case is a header without
`Book Id` and `Title`.

## How sync works

| Concern | Behaviour |
|---|---|
| Identity | `book:<Book Id>` — Goodreads book ids are global and stable across exports. |
| Incremental | Every run re-reads the whole snapshot and upserts by id under `merge`. Rows are deterministic (no ingestion timestamps anywhere), so an unchanged book keeps its content hash and is **not** re-cognified; only genuinely changed books (a new rating, an edited review, a shelf move, a new re-read) get re-cognified. |
| Forget-on-delete | The book ids of the previous run are kept in dlt resource state. A book present then but absent from the current export is emitted as a `_deleted` hard-delete row, and cognee's `orphan_cleanup` forgets it. The state is written only after the last row of a run and only persisted after a successful load, so a failed run never corrupts it. |
| Shelf filter | `shelves=["read", "favorites"]` (case-insensitive) ingests only books on those shelves, matching the exclusive shelf or any `Bookshelves` entry. Deletions still diff against **every** previously ingested book, so a book that stops matching the filter is forgotten, while a book that still matches another selected shelf stays. |
| Node sets | Each row carries `cognee_node_set`: `shelf:<slug>` for the exclusive and every custom shelf, plus `author:<first-last-slug>` (from `Author l-f`, "last, first", flipped to first-last), so books group in the graph by shelf and author without LLM extraction. |
| Resource name | Defaults to `goodreads_default`; pass `resource_name=` (lowercase letters, digits, underscores) to choose the table name and keep it fixed across runs. Use one per account when you sync more than one export into a dataset. |

## Limitations (be honest)

- **The export is a snapshot.** Nothing is fetched live: a rating, review or
  re-read you log today only arrives with your next export download, and
  removals (a book deleted from your library, a review removed) are only
  detected when a later export no longer contains them. Re-export after
  changes you care about.
- **The public API is retired.** There is no incremental feed; the CSV is the
  only supported access path, and no historical data (past edits of a review,
  for example) is available.
- **Switching exports merges by Book Id.** Goodreads book ids are global, so
  pointing the same resource at a different account's export merges into the
  same table by `Book Id`, and books the second export does not list are
  forgotten. Give each account its own `resource_name` (and dataset) to keep
  them apart.
- **Private data.** Reviews and private notes are personal data; anyone who
  can read the cognee dataset can recall them. Keep them in their own dataset.
- **Duplicate `Book Id`s** in one export (not something Goodreads emits) would
  keep the first row and log a warning.

## Resetting a sync

The known-id state lives in dlt's pipeline state (under `~/.dlt/pipelines`, or
`$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning cognee does
not reset it, so the next sync would still diff against the old known ids. To
re-sync from scratch, delete that pipeline folder as well. The state is kept
per `resource_name` (`PIPELINE_SCOPE_ATTR`).

## Privacy and security

- The connector is read-only and offline: it opens the CSV you pass and
  nothing else. No network access, no credentials.
- Hosts can pass `check_active=`, a callable run before the export is read and
  around every row; raising from it stops the sync without corrupting state.
- After a run, `source.cognee_sync_stats` holds counts only: `books`,
  `deletions`, `skipped` and `failed`.

## Testing

```bash
uv run pytest tests/ -q
```

No export or account is needed. `tests/fake_goodreads.py` writes a real export
CSV with the exact shapes of a real one (UTF-8 BOM, an extra unknown column,
escaped ISBNs, HTML reviews, repeated unnumbered and numbered snake_case
re-read columns, a footer junk line, a trailing empty line) covering a read
book with rating and review, a re-read with dates out of order in the file, a
currently-reading book, a to-read book, an unrated book, a book with additional
authors, custom shelves, and a book read 3 times with a single `Date Read`.

- `test_goodreads.py` covers rendering and determinism, the re-read timeline,
  star ratings, HTML stripping, ISBN cleaning, node sets, title truncation,
  first run and edited-review upserts, book deletion, shelf-filter matching
  and its deletion semantics, state timing, tolerant parsing, the factory
  validation and the dlt resource wiring (merge, `id` primary key, hard
  delete, `DOCUMENT_SOURCE_ATTR`, stats, `check_active`, service injection),
  and the row-to-document mapping in cognee.
- `test_goodreads_delivery.py` drives a real dlt pipeline into SQLite: rows
  load, an edited review replaces the row's content, a deleted book physically
  disappears from the table (what cognee's forget-on-delete compares under
  `max_rows_per_table=0`), and a `replace` run is refused.
