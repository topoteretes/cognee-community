"""Letterboxd connector for cognee: a ``dlt`` source that turns a Letterboxd
data export into memory.

Hand the source to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_letterboxd import letterboxd_source

    await cognee.remember(
        letterboxd_source(export="~/Downloads/letterboxd_export.zip"),
        dataset_name="letterboxd",
        primary_key="id",
        write_disposition="merge",   # REQUIRED, see below
        max_rows_per_table=0,        # compare forget-on-delete against every row
    )

Design
------
* **No API, no auth**: Letterboxd has no public API, so the connector reads the
  export ZIP you download from letterboxd.com (Settings -> Import & Export ->
  Request Export). Pass either the ``.zip`` or the folder you extracted it into.
  Every run re-reads the whole snapshot; ``merge`` upserts each film by its
  Letterboxd URI, and an unchanged film renders byte-identical content, so its
  content hash does not change and it is not re-cognified.
* **Tolerant parsing**: columns are matched by name (case-insensitive), unknown
  and extra columns are ignored, and missing optional files (``ratings.csv``,
  ``reviews.csv``, ``diary.csv``, ``lists/``, ``watchlist.csv``) or columns
  (``Tags``, ``Date``, ``Rewatch`` ...) are fine. The only hard requirement is
  ``watched.csv``; without it the connector raises a clear error.
* **One row per film**, keyed by the Letterboxd URI (the stable public id of a
  film). ``content`` merges watched, rating (rendered as stars, 10-scale
  ``r`` -> ``r/2`` stars with half-steps), the diary timeline (sorted by date,
  rewatch markers and tags kept), the full review text and the film's lists.
  One row per list holds the ordered roster. Rows are deterministic: no
  ingestion timestamps, so unchanged films keep their content hash.
* **Forget-on-delete**: the export is a snapshot, so a film or list that was in
  the previous run's state but is absent from the current export is emitted as
  a ``_deleted`` hard-delete row and cognee's ``orphan_cleanup`` forgets it.
  Removals are only detected on the next re-export.
* **Switching exports**: nothing in the export identifies the account, so the
  state is keyed by URI only. Pointing the connector at a different account's
  export merges it into the same table by URI key; films of the first account
  that the second does not list are forgotten. Use a separate
  ``resource_name`` (and dataset) per account to keep them apart.
* **Node sets**: each film row carries ``cognee_node_set`` with
  ``list:<slug>`` per list it belongs to and ``decade:<YYYY>s`` derived from
  the year, so films group in the graph without LLM extraction.

.. important::
   ``write_disposition="merge"`` is mandatory. The add pipeline defaults to
   ``"replace"``, which would wipe every earlier film on the second run; the
   resource itself refuses a disposition other than ``merge``.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

logger = get_logger("letterboxd_connector")

LETTERBOXD_SOURCE_NAME = "letterboxd"

# dlt snake-cases table names, so "Film-Diary" and "film_diary" would share a table
# while keeping separate sync states. Only names dlt leaves unchanged are accepted.
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
    "Download your export from letterboxd.com (Settings -> Import & Export -> "
    "Request Export) and pass the .zip or the extracted folder as export=."
)


class LetterboxdError(RuntimeError):
    """A Letterboxd export could not be turned into rows."""


class LetterboxdExportError(LetterboxdError):
    """The export is missing, not a zip/folder, or has no watched.csv."""


# ---------------------------------------------------------------------------
# Export reading (tolerant: columns by name, unknown extras ignored)
# ---------------------------------------------------------------------------
@dataclass
class LetterboxdList:
    """One ``lists/*.csv`` file: the list's name (the file name) and its rows."""

    name: str
    rows: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ExportData:
    """The parsed CSVs of one export. Every part but ``watched`` is optional."""

    watched: list[dict[str, str]] = field(default_factory=list)
    ratings: list[dict[str, str]] = field(default_factory=list)
    reviews: list[dict[str, str]] = field(default_factory=list)
    diary: list[dict[str, str]] = field(default_factory=list)
    watchlist: list[dict[str, str]] = field(default_factory=list)
    lists: list[LetterboxdList] = field(default_factory=list)


def _read_csv(text: str) -> list[dict[str, str]]:
    """Parse CSV text into rows keyed by lowercased column names.

    Unknown columns are kept but never read; rows with more fields than the
    header (DictReader collects those under a ``None`` key) are dropped from
    the mapping, not crashed on; a missing column is just an absent key.
    """
    return [
        {
            (key or "").strip().lower(): value.strip() if isinstance(value, str) else ""
            for key, value in row.items()
            if (key or "").strip()
        }
        for row in csv.DictReader(io.StringIO(text))
    ]


def _read_export_files(path: Path) -> dict[str, str]:
    """Every ``.csv`` of the export as ``<archive path> -> text``."""
    if path.is_dir():
        return {
            csv_path.relative_to(path).as_posix(): csv_path.read_text(
                encoding="utf-8-sig", errors="replace"
            )
            for csv_path in sorted(path.rglob("*.csv"))
        }
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name).decode("utf-8-sig", errors="replace")
                for name in archive.namelist()
                if name.lower().endswith(".csv")
            }
    raise LetterboxdExportError(
        f"{path} is neither a .zip nor a folder. {_EXPORT_HELP}"
    )


def _classify_files(files: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split the export's CSVs into top-level files and ``lists/`` files.

    The top-level five are matched by file name wherever they sit (some
    archives wrap everything in one folder), so ``export/watched.csv`` counts.
    First match in sorted order wins if a name appears twice.
    """
    top: dict[str, str] = {}
    lists: dict[str, str] = {}
    for name, text in sorted(files.items()):
        parts = [part for part in name.lower().split("/") if part]
        if not parts or not parts[-1].endswith(".csv"):
            continue
        if len(parts) >= 2 and parts[-2] == "lists":
            lists[name] = text
        else:
            top.setdefault(parts[-1], text)
    return top, lists


def _list_name(archive_path: str) -> str:
    """A list's name is its file name (the export carries no list-name column)."""
    return re.sub(r"\.csv$", "", archive_path.rsplit("/", 1)[-1]).strip()


def parse_export(files: dict[str, str]) -> ExportData:
    """Turn ``<archive path> -> csv text`` into :class:`ExportData`."""
    top, list_files = _classify_files(files)
    if "watched.csv" not in top:
        raise LetterboxdExportError(
            "watched.csv not found in the export, and it is the one required file. "
            f"{_EXPORT_HELP}"
        )
    return ExportData(
        watched=_read_csv(top["watched.csv"]),
        ratings=_read_csv(top.get("ratings.csv", "")),
        reviews=_read_csv(top.get("reviews.csv", "")),
        diary=_read_csv(top.get("diary.csv", "")),
        watchlist=_read_csv(top.get("watchlist.csv", "")),
        lists=[
            LetterboxdList(name=_list_name(path), rows=_read_csv(text))
            for path, text in list_files.items()
        ],
    )


class LetterboxdExportService:
    """Reads a Letterboxd export (ZIP or extracted directory) into rows.

    ``read_export()`` is the whole interface, so tests can substitute a fake
    service without touching the filesystem.
    """

    def __init__(self, export: str | Path):
        self._export = Path(export)

    def __repr__(self) -> str:
        return f"LetterboxdExportService(export={str(self._export)!r})"

    def read_export(self) -> ExportData:
        return parse_export(_read_export_files(self._export))


def _validate_export(path: Path) -> None:
    """Fail fast in the factory: the path exists and watched.csv is there."""
    files = _read_export_files(path)
    top, _ = _classify_files(files)
    if "watched.csv" not in top:
        raise LetterboxdExportError(f"watched.csv not found in {path}. {_EXPORT_HELP}")


# ---------------------------------------------------------------------------
# Film index: merge the CSVs into one record per film
# ---------------------------------------------------------------------------
@dataclass
class DiaryEntry:
    """One ``diary.csv`` row (one watch of a film)."""

    date: str = ""
    rewatch: bool = False
    tags: list[str] = field(default_factory=list)
    rating: float | None = None
    review: str = ""


@dataclass
class Film:
    """Everything the export says about one film, keyed by its Letterboxd URI."""

    uri: str
    name: str = ""
    year: str | None = None
    rating: float | None = None
    review: str | None = None
    entries: list[DiaryEntry] = field(default_factory=list)
    list_names: set[str] = field(default_factory=set)
    watchlist: bool = False
    _reviews_rating: float | None = None


def _col(row: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value:
            return value
    return None


def _parse_rating(value: str | None) -> float | None:
    try:
        rating = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return rating if 0 < rating <= 10 else None


def _film_row(row: dict[str, str]) -> tuple[str, str, str | None] | None:
    """(uri, name, year) of a CSV row, or ``None`` when it names no film."""
    uri = _col(row, "letterboxd uri", "letterboxd_uri", "uri")
    name = _col(row, "name", "title")
    if not uri or not name:
        return None
    return uri, name, _col(row, "year")


def build_films(data: ExportData, stats: dict[str, int] | None = None) -> dict[str, Film]:
    """Merge every CSV of the export into one :class:`Film` per Letterboxd URI.

    Films appear in more files than ``watched.csv`` (a list can hold an unwatched
    film), so the index is the union; a row without a URI or a name cannot be
    keyed and is counted as ``skipped``.
    """
    if stats is None:
        stats = {}
    films: dict[str, Film] = {}

    def film_of(row: dict[str, str]) -> Film | None:
        parsed = _film_row(row)
        if parsed is None:
            if any(row.values()):
                stats["skipped"] = stats.get("skipped", 0) + 1
            return None
        uri, name, year = parsed
        film = films.get(uri)
        if film is None:
            film = Film(uri=uri)
            films[uri] = film
        film.name = film.name or name
        film.year = film.year or year
        return film

    for row in data.diary:
        film = film_of(row)
        if film is None:
            continue
        film.entries.append(
            DiaryEntry(
                date=(_col(row, "date", "watched date") or ""),
                rewatch=(_col(row, "rewatch") or "").strip().lower() == "yes",
                tags=[tag.strip() for tag in (_col(row, "tags") or "").split(",") if tag.strip()],
                rating=_parse_rating(_col(row, "rating")),
                review=_col(row, "review") or "",
            )
        )
    for row in data.watched:
        film = film_of(row)
        if film is None:
            continue
        # Newer exports carry the last watch date here; it only becomes a diary
        # entry when diary.csv said nothing about the film.
        if not film.entries and _col(row, "date"):
            film.entries.append(DiaryEntry(date=_col(row, "date") or ""))
    for row in data.ratings:
        film = film_of(row)
        if film is not None:
            film.rating = _parse_rating(_col(row, "rating"))
    for row in data.reviews:
        film = film_of(row)
        if film is not None:
            film.review = _col(row, "review")
            film._reviews_rating = _parse_rating(_col(row, "rating"))
    for row in data.watchlist:
        film = film_of(row)
        if film is not None:
            film.watchlist = True
    for letterboxd_list in data.lists:
        for row in letterboxd_list.rows:
            film = film_of(row)
            if film is not None:
                film.list_names.add(letterboxd_list.name)

    for film in films.values():
        film.entries.sort(key=lambda entry: entry.date or "9999-99-99")
        if film.rating is None:
            film.rating = film._reviews_rating
        if film.rating is None:  # the newest diary entry that carries one
            film.rating = next(
                (entry.rating for entry in reversed(film.entries) if entry.rating is not None),
                None,
            )
        if not film.review:  # a diary comment stands in for a missing review
            film.review = next(
                (entry.review for entry in reversed(film.entries) if entry.review),
                None,
            )
    return films


def _decade(year: str | None) -> str | None:
    try:
        number = int(str(year).strip())
    except (TypeError, ValueError):
        return None
    return f"decade:{number - number % 10}s"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def rating_to_stars(rating: float) -> str:
    """A 10-scale rating as stars: ``r/2`` stars, half-steps allowed.

    A half step on the 10 scale is half a star, so a rating of 7.5 renders as
    the 3.5 stars it rounds down to (Letterboxd itself only emits integers).
    """
    halves = max(0, min(10, int(rating)))  # one point on the 10 scale = half a star
    return "★" * (halves // 2) + "½" * (halves % 2)


def film_to_row(film: Film) -> dict[str, Any]:
    """Render one film as a document row.

    Deterministic: the same film in the same export always renders the same
    content, so an unchanged film keeps its content hash and is not
    re-cognified.
    """
    lines = [f"Film: {film.name}"]
    if film.year:
        lines.append(f"Year: {film.year}")
    lines.append(f"URI: {film.uri}")
    if film.watchlist:
        lines.append("On my watchlist")
    if film.rating is not None:
        lines.append(f"My rating: {rating_to_stars(film.rating)} ({film.rating:g}/10)")
    if film.list_names:
        lines.append("My lists: " + ", ".join(sorted(film.list_names)))
    if film.entries:
        lines.append("Diary:")
        for entry in film.entries:
            when = entry.date or "unknown date"
            rewatch = " (rewatch)" if entry.rewatch else ""
            tags = f", tags: {', '.join(entry.tags)}" if entry.tags else ""
            lines.append(f"- {when}{rewatch}{tags}")
    if film.review:
        lines.append("My review:")
        lines.append(film.review)

    node_sets = {f"list:{_slug(name)}" for name in film.list_names}
    decade = _decade(film.year)
    if decade:
        node_sets.add(decade)

    return {
        "id": film.uri,
        "title": f"{film.name} ({film.year})" if film.year else film.name,
        "content": "\n".join(lines),
        "url": film.uri,
        NODE_SET_COLUMN: sorted(node_sets),
        "_deleted": False,
    }


def list_to_row(name: str, roster: list[tuple[str, str | None]]) -> dict[str, Any]:
    """Render one list as a document row: the ordered roster of its films."""
    lines = [f"List: {name}", "Films:"]
    for position, (film_name, year) in enumerate(roster, start=1):
        lines.append(f"{position}. {film_name} ({year})" if year else f"{position}. {film_name}")
    return {
        "id": f"list:{_slug(name)}",
        "title": name,
        "content": "\n".join(lines),
        "url": None,
        NODE_SET_COLUMN: [],
        "_deleted": False,
    }


def deleted_row(key: str) -> dict[str, Any]:
    """Hard-delete marker for a film or list that left the export."""
    return {"id": key, "_deleted": True}


def build_list_rows(data: ExportData) -> list[dict[str, Any]]:
    """One row per ``lists/*.csv``, the roster ordered by the Position column."""
    rows: dict[str, dict[str, Any]] = {}
    for letterboxd_list in data.lists:
        entries: list[tuple[int, int, str, str | None]] = []
        for index, row in enumerate(letterboxd_list.rows):
            parsed = _film_row(row)
            if parsed is None:
                continue
            _uri, film_name, year = parsed
            try:
                position = int(str(_col(row, "position") or "").strip())
            except ValueError:
                position = 0
            entries.append((position or 10**9, index, film_name, year))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        row = list_to_row(
            letterboxd_list.name, [(film_name, year) for _pos, _idx, film_name, year in entries]
        )
        existing = rows.get(row["id"])
        if existing is not None:  # two file names slug to the same id: first wins
            logger.warning(
                "Letterboxd: two lists share the id %s; keeping %s.", row["id"], existing["title"]
            )
            continue
        rows[row["id"]] = row
    return list(rows.values())


# ---------------------------------------------------------------------------
# Sync (pure given the parsed export + state dict, unit-testable)
# ---------------------------------------------------------------------------
def sync_export(
    data: ExportData, state: dict, stats: dict[str, int] | None = None
) -> Iterator[dict[str, Any]]:
    """Yield a row per film and per list, then hard-deletes for what left.

    ``state`` (dlt resource state) holds the ids of the previous run:
    ``known_films`` and ``known_lists``. The run works on copies and writes
    them only after its last row, and dlt only persists the state after the
    load succeeds, so a failed run does not corrupt the known-id sets.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(films=0, lists=0, deletions=0, skipped=0, failed=0)

    films = build_films(data, stats)
    list_rows = build_list_rows(data)
    current_films = set(films)
    current_lists = {row["id"] for row in list_rows}
    previous_films = set(state.get("known_films") or [])
    previous_lists = set(state.get("known_lists") or [])

    for uri in sorted(films):
        stats["films"] += 1
        yield film_to_row(films[uri])
    for row in sorted(list_rows, key=lambda item: item["id"]):
        stats["lists"] += 1
        yield row
    for uri in sorted(previous_films - current_films):
        stats["deletions"] += 1
        yield deleted_row(uri)
    for key in sorted(previous_lists - current_lists):
        stats["deletions"] += 1
        yield deleted_row(key)

    logger.info(
        "Letterboxd: %d film row(s), %d list row(s), %d deletion(s), %d skipped row(s).",
        stats["films"],
        stats["lists"],
        stats["deletions"],
        stats["skipped"],
    )
    # Only reached once every row is out: a run stopped mid-way saves nothing.
    state["known_films"] = sorted(current_films)
    state["known_lists"] = sorted(current_lists)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def letterboxd_source(
    export: str | Path | None = None,
    resource_name: str | None = None,
    check_active: Any = None,
    service: Any = None,
):
    """Create a dlt source that yields a Letterboxd export as documents.

    Args:
        export: Path to the export ``.zip`` from letterboxd.com (Settings ->
            Import & Export -> Request Export) or to the folder it was
            extracted into. Ignored when ``service=`` is given.
        resource_name: Table name and part of the sync state key (lowercase
            letters, digits and underscores). Defaults to
            ``letterboxd_default``; keep it fixed across runs. Use one per
            account when you sync more than one export into a dataset.
        check_active: Optional host authorization checkpoint, called before
            the export is read and around every row
            (``dlt_utils.guarded_rows``); raising from it stops the sync.
        service: Object with ``read_export() -> ExportData``; a test injection
            point so a fake export needs no filesystem. Built from ``export``
            when omitted.

    Returns:
        A dlt source with one ``<resource_name>`` resource
        (``primary_key="id"``, ``write_disposition="merge"``, ``_deleted``
        hard delete). Pass ``write_disposition="merge"`` to
        ``cognee.remember`` too. ``source.cognee_sync_stats`` holds counts
        only: ``films``, ``lists``, ``deletions``, ``skipped`` and ``failed``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Letterboxd connector requires dlt: pip install "dlt[sqlalchemy]".'
        ) from exc

    if resource_name and not _RESOURCE_NAME_RE.match(resource_name):
        raise ValueError(
            "resource_name must be lowercase letters, digits and underscores, starting "
            "with a letter, so dlt uses it unchanged as the table name."
        )
    if service is None:
        if export is None or not str(export).strip():
            raise LetterboxdExportError(
                "A Letterboxd export is required: pass export= (the .zip or extracted "
                "folder). " + _EXPORT_HELP
            )
        path = Path(export)
        if not path.exists():
            raise LetterboxdExportError(f"Letterboxd export not found: {path}. {_EXPORT_HELP}")
        _validate_export(path)
        service = LetterboxdExportService(path)

    table_name = resource_name or f"{LETTERBOXD_SOURCE_NAME}_default"
    stats: dict[str, int] = {}

    @dlt.resource(
        name=table_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the staging table on merge,
        # which is what makes cognee's orphan cleanup forget them.
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            # Typed up front so a column that is empty in a run (``url`` for
            # list rows, the text columns in a deletion-only run) still exists.
            "title": {"data_type": "text", "nullable": True},
            "content": {"data_type": "text", "nullable": True},
            "url": {"data_type": "text", "nullable": True},
            **NODE_SET_COLUMN_HINT,
        },
    )
    def letterboxd_films():
        yield from guarded_rows(_rows(dlt.current.resource_state()), check_active)

    def _rows(state: dict):
        stats.clear()
        stats.update(films=0, lists=0, deletions=0, skipped=0, failed=0)
        # The caller's write_disposition overrides the one declared above; a
        # replace run of this snapshot's delta would wipe every film but the
        # changed rows, so refuse it before reading anything.
        disposition = source.resources[table_name].write_disposition
        if disposition != "merge":
            raise ValueError(
                f'letterboxd_source needs write_disposition="merge", got {disposition!r}. '
                "Its rows upsert by id and its deletions only work under merge."
            )
        if check_active is not None:
            check_active()
        try:
            data = service.read_export()
        except LetterboxdError:
            stats.update(failed=1)
            raise
        # The known-id state is written after the last row, so a run stopped
        # between rows (by check_active, say) saves nothing and forgets nothing.
        yield from sync_export(data, state, stats)

    @dlt.source(name=LETTERBOXD_SOURCE_NAME)
    def _letterboxd():
        return letterboxd_films

    source = _letterboxd()
    # Route rows through the document path (text -> cognify), like Notion.
    setattr(source, DOCUMENT_SOURCE_ATTR, LETTERBOXD_SOURCE_NAME)
    # One dlt pipeline (and so one known-id state) per resource name.
    setattr(source, PIPELINE_SCOPE_ATTR, f"letterboxd:{resource_name or 'default'}")
    # Host-readable diagnostics contain counts only, never film titles or reviews.
    source.cognee_sync_stats = stats
    source.resources[table_name].cognee_sync_stats = stats
    return source
