"""Goodreads connector for cognee: a ``dlt`` source that turns a Goodreads
library export CSV into memory.

Hand the source to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_goodreads import goodreads_source

    await cognee.remember(
        goodreads_source(export="~/Downloads/goodreads_library_export.csv"),
        dataset_name="goodreads",
        primary_key="id",
        write_disposition="merge",   # REQUIRED, see below
        max_rows_per_table=0,        # compare forget-on-delete against every row
    )

Design
------
* **No API, no auth**: Goodreads retired its public API in 2020, so the
  connector reads the official library export (goodreads.com: Settings ->
  Import/Export -> "Export Library"), a CSV file you download and pass as
  ``export=``. Parsing is local (stdlib ``csv``) and read-only; no network.
* **Tolerant parsing**: columns are matched by name (case-insensitive, ignoring
  spacing/underscores), unknown and extra columns are ignored, and the export
  starts with a UTF-8 BOM (stripped). The only hard requirement is a header
  with ``Book Id`` and ``Title``; rows without both (a metadata footer, a
  trailing empty line) are skipped and counted.
* **Re-reads**: for a re-read, the export repeats the read-tracking columns —
  additional ``Date Read``-style columns plus repeated ``My Review`` /
  ``My Rating`` columns, numbered or not. Every column matching each family is
  collected and the three families are zipped positionally into a per-read
  timeline: a read exists for each non-empty date, and the review/rating at
  the same position belongs to it. When ``Read Count`` is greater than 1 but
  only one dated read exists (the export without re-read columns), the single
  date is rendered together with the count. The timeline is sorted by date.
* **One row per book**, keyed ``book:<Book Id>`` (Goodreads book ids are
  global, so they are stable across exports). ``content`` renders title,
  authors, year (original publication preferred), cleaned ISBN/ISBN13 (the
  export quotes them as ``="..."``), shelves, a status line, the read
  timeline with per-read rating (``★ x/5``, only when rated; 0 means unrated)
  and the full review text (HTML stripped to plain text), average rating,
  publisher, binding, pages, owned copies and private notes — each only when
  non-empty. Rows are deterministic: no ingestion timestamps, so an unchanged
  book keeps its content hash and is not re-cognified.
* **Forget-on-delete**: the export is a snapshot, so a book present in the
  previous run's state but absent from the current export is emitted as a
  ``_deleted`` hard-delete row and cognee's ``orphan_cleanup`` forgets it.
  Removals are only detected on the next re-export.
* **Shelf filter**: ``shelves=`` (a list of shelf names, case-insensitive)
  ingests only books on those shelves, matching the exclusive shelf or any
  ``Bookshelves`` entry. Deletions still diff against every previously
  ingested key, so a book that stops matching the filter is forgotten while a
  book that still matches another selected shelf stays.
* **Switching exports**: Goodreads book ids are global, so pointing the same
  resource at a different account's export merges into the same table by
  ``Book Id``; books the second export does not list are forgotten. Use a
  separate ``resource_name`` (and dataset) per account to keep them apart.
* **Node sets**: each row carries ``cognee_node_set`` with ``shelf:<slug>``
  for every shelf (exclusive and custom) and ``author:<first-last-slug>``
  from ``Author l-f``, so books link in the graph by shelf and author without
  LLM extraction.

.. important::
   ``write_disposition="merge"`` is mandatory. The add pipeline defaults to
   ``"replace"``, which would wipe every earlier book on the second run; the
   resource itself refuses a disposition other than ``merge``.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from itertools import zip_longest
from pathlib import Path
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

logger = get_logger("goodreads_connector")

GOODREADS_SOURCE_NAME = "goodreads"

# dlt snake-cases table names, so "My-Library" and "my_library" would share a
# table while keeping separate sync states. Only names dlt leaves unchanged
# are accepted.
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# The node-set column, as declared by the installed cognee (fallback for older
# releases that predate per-row node sets).
NODE_SET_COLUMN = getattr(dlt_utils, "NODE_SET_COLUMN", "cognee_node_set")
NODE_SET_COLUMN_HINT = getattr(
    dlt_utils,
    "NODE_SET_COLUMN_HINT",
    {NODE_SET_COLUMN: {"data_type": "json", "nullable": True}},
)

_EXPORT_HELP = (
    "Download your library export from goodreads.com (Settings -> Import/Export -> "
    "Export Library) and pass the CSV path as export=."
)

_TITLE_LENGTH = 80

# Header names are normalized (lowercased, non-alphanumerics dropped) before
# matching, so "Book Id", "book_id" and "bookid" are the same column.
_SCALAR_COLUMNS = {
    "bookid": "book_id",
    "title": "title",
    "author": "author",
    "authorlf": "author_lf",
    "additionalauthors": "additional_authors",
    "isbn": "isbn",
    "isbn13": "isbn13",
    "averagerating": "average_rating",
    "publisher": "publisher",
    "binding": "binding",
    "numberofpages": "pages",
    "yearpublished": "year_published",
    "originalpublicationyear": "original_publication_year",
    "bookshelves": "bookshelves",
    "exclusiveshelf": "exclusive_shelf",
    "privatenotes": "private_notes",
    "readcount": "read_count",
    "ownedcopies": "owned_copies",
}

# Read-tracking families, in normalized form: every matching column is one
# read's date / review / rating. "date read", "date_read", "Date Read 2" and
# "dateread2" all match "dateread".
_READ_FAMILIES = ("dateread", "myreview", "myrating")

_READ_TIMELINE_HEADING = "Read timeline:"


class GoodreadsError(RuntimeError):
    """A Goodreads export could not be turned into rows."""


class GoodreadsExportError(GoodreadsError):
    """The export is missing, unreadable, or not a Goodreads library CSV."""


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.strip().lower())


def _read_family(name: str) -> str | None:
    """The read-tracking family of a normalized column name, if any.

    ``dateread``, ``dateread2`` and ``dateread10`` match the ``dateread``
    family; a name like ``datereadagain`` (or anything with a non-numeric
    tail) does not.
    """
    for family in _READ_FAMILIES:
        if name == family or (name.startswith(family) and name[len(family) :].isdigit()):
            return family
    return None


class _HTMLTextParser(HTMLParser):
    """Collects the text of an HTML fragment, turning ``<br>`` into newlines."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("p", "div"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(text: str) -> str:
    """Review HTML (``<br/>``, ``<a href>``) as plain text; entities unescaped."""
    parser = _HTMLTextParser()
    parser.feed(text)
    parser.close()
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def clean_isbn(value: str) -> str:
    """Strip the export's ``="..."`` escaping (and stray quotes) from an ISBN."""
    cleaned = value.strip()
    if cleaned.startswith('="') and cleaned.endswith('"') and len(cleaned) > 2:
        cleaned = cleaned[2:-1]
    elif cleaned.startswith("="):
        cleaned = cleaned[1:].strip('"')
    return cleaned.strip()


def _parse_int(value: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _parse_float(value: str) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number


def _parse_rating(value: str) -> int:
    """A read's ``My Rating``: an int 0-5, where 0 (or garbage) means unrated."""
    rating = _parse_int(value)
    return rating if 0 < rating <= 5 else 0


def _map_header(header: list[str]) -> list[tuple[str | None, str | None]]:
    """Each header cell -> (canonical scalar key, read family), both optional."""
    mapped: list[tuple[str | None, str | None]] = []
    for cell in header:
        normalized = _normalize_name(cell or "")
        if not normalized:
            mapped.append((None, None))
        elif normalized in _SCALAR_COLUMNS:
            mapped.append((_SCALAR_COLUMNS[normalized], None))
        else:
            mapped.append((None, _read_family(normalized)))
    return mapped


def has_required_columns(header: list[str]) -> bool:
    """A header names a Goodreads export when it has ``Book Id`` and ``Title``."""
    normalized = {_normalize_name(cell or "") for cell in header}
    return "bookid" in normalized and "title" in normalized


def parse_export(path: str | Path) -> list[dict[str, Any]]:
    """Parse a Goodreads library export CSV into one dict per CSV record.

    Scalar columns land under their canonical key; the read-tracking column
    families are zipped positionally into ``reads`` (a read per non-empty
    date, carrying that position's rating and review), with ``read_count``
    kept for rendering. Records without a Book Id and a Title (a metadata
    footer, a trailing empty line) stay in the list with empty
    ``book_id``/``title`` and a ``_has_content`` flag, so the sync can count
    them as skipped instead of crashing on them.
    """
    path = Path(path)
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None or not has_required_columns(header):
                raise GoodreadsExportError(
                    f"{path} does not look like a Goodreads library export "
                    f"(no 'Book Id' and 'Title' columns). {_EXPORT_HELP}"
                )
            mapped = _map_header(header)
            return [_parse_row(values, mapped) for values in reader]
    except OSError as exc:
        raise GoodreadsExportError(
            f"Goodreads export not readable: {path}. {_EXPORT_HELP}"
        ) from exc


def _parse_row(values: list[str], mapped: list[tuple[str | None, str | None]]) -> dict:
    """One CSV record -> a book dict. Records that name no book (no Book Id
    and no Title) keep empty keys and ``_has_content`` says whether the line
    held anything at all."""
    row: dict[str, Any] = {}
    families: dict[str, list[str]] = {family: [] for family in _READ_FAMILIES}
    has_content = False
    padded = zip_longest(mapped, values, fillvalue="")
    for (scalar, family), raw in padded:
        value = raw.strip() if isinstance(raw, str) else ""
        if value:
            has_content = True
        if scalar is not None:
            row[scalar] = value
        elif family is not None:
            families[family].append(value)
    book_id = row.get("book_id", "")
    title = row.get("title", "")
    reads = [
        {
            "date": date,
            "rating": _parse_rating(rating),
            "review": strip_html(review),
        }
        for date, review, rating in zip_longest(
            families["dateread"], families["myreview"], families["myrating"], fillvalue=""
        )
        if date
    ]
    reads.sort(key=lambda read: read["date"])
    row["book_id"] = book_id
    row["title"] = title
    row["_has_content"] = has_content
    row["reads"] = reads
    row["read_count"] = _parse_int(row.get("read_count", ""))
    return row


class GoodreadsExportService:
    """Reads a Goodreads library export CSV into parsed rows.

    ``read_export()`` is the whole interface, so tests can substitute a fake
    service without touching the filesystem.
    """

    def __init__(self, export: str | Path):
        self._export = Path(export)

    def __repr__(self) -> str:
        return f"GoodreadsExportService(export={str(self._export)!r})"

    def read_export(self) -> list[dict[str, Any]]:
        return parse_export(self._export)


def _validate_export(path: Path) -> None:
    """Fail fast in the factory: the path exists, is readable, and is a
    Goodreads export (a header with ``Book Id`` and ``Title``)."""
    if not path.exists():
        raise GoodreadsExportError(f"Goodreads export not found: {path}. {_EXPORT_HELP}")
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            header = next(csv.reader(handle), None)
    except OSError as exc:
        raise GoodreadsExportError(
            f"Goodreads export not readable: {path}. {_EXPORT_HELP}"
        ) from exc
    if header is None or not has_required_columns(header):
        raise GoodreadsExportError(
            f"{path} does not look like a Goodreads library export "
            f"(no 'Book Id' and 'Title' columns). {_EXPORT_HELP}"
        )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_STATUS = {
    "read": "Read",
    "currently-reading": "Currently reading",
    "to-read": "To read",
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def author_slug(row: dict) -> str | None:
    """``author:<first-last-slug>``: ``Author l-f`` is "last, first", so the
    parts are flipped; the display ``Author`` stands in when it is unusable."""
    author = row.get("author", "")
    source = row.get("author_lf", "") or author
    parts = source.split(",", 1)
    if len(parts) == 2 and parts[1].strip() and parts[0].strip():
        source = f"{parts[1].strip()} {parts[0].strip()}"
    slug = _slug(source)
    return f"author:{slug}" if slug else None


def shelf_names(row: dict) -> list[str]:
    """The exclusive shelf first, then every ``Bookshelves`` entry, deduped."""
    shelves = [row.get("exclusive_shelf", "").strip()]
    shelves += [
        shelf.strip()
        for shelf in (row.get("bookshelves", "") or "").split(";")
        if shelf.strip()
    ]
    return list(dict.fromkeys(shelf for shelf in shelves if shelf))


def _timeline_lines(row: dict) -> list[str]:
    reads = row["reads"]
    if not reads:
        # A book read more than once whose dates were never tracked.
        if row["read_count"] > 1:
            return [f"Read count: {row['read_count']}"]
        return []
    # Only one dated read but Read Count > 1 (the export without re-read
    # columns): the single date is rendered together with the count.
    with_count = row["read_count"] > 1 and len(reads) == 1
    lines = [_READ_TIMELINE_HEADING]
    for read in reads:
        entry = f"- Read on {read['date']}"
        if read["rating"]:
            entry += f", ★ {read['rating']}/5"
        if with_count:
            entry += f" (read {row['read_count']} times)"
        lines.append(entry)
        if read["review"]:
            review_lines = read["review"].splitlines()
            lines.append(f"  Review: {review_lines[0]}")
            lines += [f"  {line}" for line in review_lines[1:]]
    return lines


def book_to_row(row: dict) -> dict[str, Any]:
    """Render one parsed book as a document row.

    Deterministic: the same book in the same export always renders the same
    content, so an unchanged book keeps its content hash and is not
    re-cognified.
    """
    title = row["title"]
    if len(title) > _TITLE_LENGTH:
        title = title[: _TITLE_LENGTH - 1].rstrip() + "…"
    lines = [f"Book: {row['title']}"]
    if row.get("author"):
        lines.append(f"Author: {row['author']}")
    if row.get("additional_authors"):
        lines.append(f"Additional authors: {row['additional_authors']}")
    year = row.get("original_publication_year") or row.get("year_published") or ""
    if year:
        lines.append(f"Year: {year}")
    isbn = clean_isbn(row.get("isbn13", "")) or clean_isbn(row.get("isbn", ""))
    if isbn:
        lines.append(f"ISBN: {isbn}")
    shelves = shelf_names(row)
    if shelves:
        lines.append("My shelves: " + ", ".join(shelves))
    status = _STATUS.get(row.get("exclusive_shelf", ""), row.get("exclusive_shelf", ""))
    if status:
        lines.append(f"Status: {status}")
    lines += _timeline_lines(row)
    average = _parse_float(row.get("average_rating", ""))
    if average is not None:
        lines.append(f"Average rating: {average:g}/5")
    for key, label in (
        ("publisher", "Publisher"),
        ("binding", "Binding"),
        ("pages", "Pages"),
        ("owned_copies", "Owned copies"),
    ):
        if row.get(key):
            lines.append(f"{label}: {row[key]}")
    if row.get("private_notes"):
        lines.append(f"Private notes: {row['private_notes']}")

    node_sets = {f"shelf:{_slug(shelf)}" for shelf in shelves if _slug(shelf)}
    author_node = author_slug(row)
    if author_node:
        node_sets.add(author_node)

    return {
        "id": f"book:{row['book_id']}",
        "title": title,
        "content": "\n".join(lines),
        "url": f"https://www.goodreads.com/book/show/{row['book_id']}",
        NODE_SET_COLUMN: sorted(node_sets),
        "_deleted": False,
    }


def deleted_row(key: str) -> dict[str, Any]:
    """Hard-delete marker for a book that left the export."""
    return {"id": key, "_deleted": True}


# ---------------------------------------------------------------------------
# Sync (pure given the parsed export + state dict, unit-testable)
# ---------------------------------------------------------------------------
def matches_shelves(row: dict, shelves: set[str] | None) -> bool:
    """A book matches when its exclusive shelf or any Bookshelves entry is
    selected (case-insensitive); ``None`` selects everything."""
    if shelves is None:
        return True
    return any(shelf.lower() in shelves for shelf in shelf_names(row))


def sync_export(
    rows: list[dict],
    state: dict,
    shelves: Iterable[str] | None = None,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield a row per selected book, then hard-deletes for what left.

    ``state`` (dlt resource state) holds the book ids of the previous run
    (``known_books``). The run works on a copy and writes it only after its
    last row, and dlt only persists the state after the load succeeds, so a
    failed run does not corrupt the known-id set. Deletions diff against every
    previously ingested id, not just the ones matching the current filter.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(books=0, deletions=0, skipped=0, failed=0)
    selected = {str(shelf).strip().lower() for shelf in shelves or () if str(shelf).strip()} or None

    books: dict[str, dict] = {}
    for row in rows:
        if not row.get("book_id") or not row.get("title"):
            # a metadata footer or trailing empty line, not a book
            if row.get("_has_content"):
                stats["skipped"] += 1
            continue
        if not matches_shelves(row, selected):
            continue
        rendered = book_to_row(row)
        existing = books.get(rendered["id"])
        if existing is not None:  # one row per Book Id; the first one wins
            logger.warning(
                "Goodreads: Book Id %s appears twice in the export; keeping %s.",
                row["book_id"],
                existing["title"],
            )
            continue
        books[rendered["id"]] = rendered
    current = set(books)
    previous = set(state.get("known_books") or [])

    for key in sorted(books):
        stats["books"] += 1
        yield books[key]
    for key in sorted(previous - current):
        stats["deletions"] += 1
        yield deleted_row(key)

    logger.info(
        "Goodreads: %d book row(s), %d deletion(s), %d skipped row(s).",
        stats["books"],
        stats["deletions"],
        stats["skipped"],
    )
    # Only reached once every row is out: a run stopped mid-way saves nothing.
    state["known_books"] = sorted(current)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def goodreads_source(
    export: str | Path | None = None,
    shelves: list[str] | None = None,
    resource_name: str | None = None,
    check_active: Any = None,
    service: Any = None,
):
    """Create a dlt source that yields a Goodreads library export as documents.

    Args:
        export: Path to the ``goodreads_library_export.csv`` you downloaded
            from goodreads.com (Settings -> Import/Export -> Export Library).
            Ignored when ``service=`` is given.
        shelves: Optional list of shelf names (case-insensitive); only books
            on one of these shelves are ingested, matching the exclusive
            shelf or any ``Bookshelves`` entry. Books that stop matching a
            later run are forgotten; deletions always consider every
            previously ingested book.
        resource_name: Table name and part of the sync state key (lowercase
            letters, digits and underscores). Defaults to
            ``goodreads_default``; keep it fixed across runs. Use one per
            account when you sync more than one export into a dataset.
        check_active: Optional host authorization checkpoint, called before
            the export is read and around every row
            (``dlt_utils.guarded_rows``); raising from it stops the sync.
        service: Object with ``read_export() -> list[dict]`` (parsed rows); a
            test injection point so a fake export needs no filesystem. Built
            from ``export`` when omitted.

    Returns:
        A dlt source with one ``<resource_name>`` resource
        (``primary_key="id"``, ``write_disposition="merge"``, ``_deleted``
        hard delete). Pass ``write_disposition="merge"`` to
        ``cognee.remember`` too. ``source.cognee_sync_stats`` holds counts
        only: ``books``, ``deletions``, ``skipped`` and ``failed``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Goodreads connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    if resource_name and not _RESOURCE_NAME_RE.match(resource_name):
        raise ValueError(
            "resource_name must be lowercase letters, digits and underscores, starting "
            "with a letter, so dlt uses it unchanged as the table name."
        )
    if shelves is not None and not isinstance(shelves, (list, tuple, set)):
        raise ValueError("shelves must be a list of shelf names (or None for every shelf).")
    if service is None:
        if export is None or not str(export).strip():
            raise GoodreadsExportError(
                "A Goodreads library export is required: pass export= (the CSV path). "
                + _EXPORT_HELP
            )
        path = Path(export)
        _validate_export(path)
        service = GoodreadsExportService(path)

    table_name = resource_name or f"{GOODREADS_SOURCE_NAME}_default"
    stats: dict[str, int] = {}

    @dlt.resource(
        name=table_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the staging table on merge,
        # which is what makes cognee's orphan cleanup forget them.
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            # Typed up front so a column that is empty in a run (the text
            # columns in a deletion-only run) still exists.
            "title": {"data_type": "text", "nullable": True},
            "content": {"data_type": "text", "nullable": True},
            "url": {"data_type": "text", "nullable": True},
            **NODE_SET_COLUMN_HINT,
        },
    )
    def goodreads_books():
        yield from guarded_rows(_rows(dlt.current.resource_state()), check_active)

    def _rows(state: dict):
        stats.clear()
        stats.update(books=0, deletions=0, skipped=0, failed=0)
        # The caller's write_disposition overrides the one declared above. A
        # replace run of this snapshot would wipe every book but the changed
        # rows, so refuse it before reading anything.
        disposition = source.resources[table_name].write_disposition
        if disposition != "merge":
            raise ValueError(
                f'goodreads_source needs write_disposition="merge", got {disposition!r}. '
                "Its rows upsert by id and its deletions only work under merge."
            )
        if check_active is not None:
            check_active()
        try:
            rows = service.read_export()
        except GoodreadsError:
            stats.update(failed=1)
            raise
        # The known-id state is written after the last row, so a run stopped
        # between rows (by check_active, say) saves nothing and forgets nothing.
        yield from sync_export(rows, state, shelves, stats)

    @dlt.source(name=GOODREADS_SOURCE_NAME)
    def _goodreads():
        return goodreads_books

    source = _goodreads()
    # Route rows through the document path (text -> cognify), like Notion.
    setattr(source, DOCUMENT_SOURCE_ATTR, GOODREADS_SOURCE_NAME)
    # One dlt pipeline (and so one known-id state) per resource name.
    setattr(source, PIPELINE_SCOPE_ATTR, f"goodreads:{resource_name or 'default'}")
    # Host-readable diagnostics contain counts only, never titles or reviews.
    source.cognee_sync_stats = stats
    source.resources[table_name].cognee_sync_stats = stats
    return source
