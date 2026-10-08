"""Dropbox connector for cognee — a ``dlt`` source that turns Dropbox folders into memory.

Sync one or more Dropbox folders (or the whole Dropbox) into cognee,
incrementally and with forget-on-deletion.  The resource produced here is
handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_dropbox import dropbox_source

    await cognee.remember(
        dropbox_source(folder_paths=["/Notes"]),
        dataset_name="my_dropbox",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by Dropbox file id
        max_rows_per_table=0,        # 0 = no row cap (folders often exceed the default 50)
    )

Design
------
* **Auth** — a long-lived OAuth 2 refresh token (``token_access_type=offline``)
  plus the app key, and the app secret unless the token came from a PKCE flow.
  The official ``dropbox`` SDK refreshes the short-lived access token on its
  own.  A plain access token also works for quick local tries.  Only the
  ``files.metadata.read`` and ``files.content.read`` scopes are needed.
* **Primary key** — the Dropbox file ``id``.  It survives renames and moves, so
  a moved file is updated in place rather than forgotten and re-learned.
* **Incremental cursor** — ``list_folder`` (recursive) on the first run, then
  ``list_folder/continue`` with the saved cursor.  There is one cursor per
  configured folder, persisted in dlt's per-resource state.  The cursor is
  saved even when nothing changed, because unused cursors can expire.
* **Deletes carry no id** — Dropbox reports a deletion as a path only.  The
  connector keeps a ``path_lower -> id`` index in dlt state to turn deleted
  paths back into ids.  A deleted folder forgets every file under its path.
* **Moves** — a move arrives as a deleted old path plus the same id at a new
  path.  Deletions are held until every folder has been read, and an id that
  shows up again is treated as an update, not a delete.
* **Errors never delete** — only a confirmed ``not_found`` removes data.  A
  reset cursor (``409 reset``) triggers a fresh listing that is reconciled
  against the index, like a snapshot.  Rate limits (429) are retried by the SDK
  after the ``Retry-After`` delay.
* **Retry on failure** — when a file fails to download, the cursor is not
  advanced, so the same changes are replayed on the next run.  The index is
  still saved, so files stored by that run can be forgotten later.
* **Content** — text, markdown and CSV are downloaded as-is, PDFs are parsed
  with ``pypdf`` (a cognee dependency), and non-downloadable files such as
  Paper docs are exported through ``files/export``.  Unsupported, too large,
  empty or unparseable files are skipped with a warning and counted.
* **Self-describing** — the resource declares its document source, so a plain
  ``remember()`` call routes file content through normal chunking + LLM graph
  extraction.  The file's folder path is written into the content so the graph
  can relate files that live together.

Limitations
-----------
* Team spaces (the ``Dropbox-API-Path-Root`` header) are not supported yet;
  the connector reads the user's own namespace.
* Files are matched to a text, markdown, CSV or PDF parser by extension.
* Removing a folder from ``folder_paths`` forgets the files that only it covered.
"""

import io
import os
import posixpath
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dropbox import files as dropbox_files
from dropbox.exceptions import ApiError

logger = get_logger("dropbox_connector")

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv"}
PDF_EXTENSION = ".pdf"
# Text formats we accept from files/export, in order of preference.  Paper docs
# offer markdown; other non-downloadable types are skipped as unsupported.
TEXT_EXPORT_FORMATS = ("markdown", "md", "txt", "plain_text", "csv")
# The largest page size list_folder accepts.
LIST_FOLDER_LIMIT = 2000
# Bounded so a long outage fails the run instead of sleeping forever.
MAX_RETRIES_ON_RATE_LIMIT = 5


@dataclass(frozen=True)
class _DropboxConfig:
    folder_paths: tuple[str, ...]
    max_file_size_mb: int


# ---------------------------------------------------------------------------
# Auth / client construction
# ---------------------------------------------------------------------------
def build_dropbox_client(
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
    app_key: str | None = None,
    app_secret: str | None = None,
) -> Any:
    """Build an authenticated Dropbox API client.

    A refresh token is preferred: the SDK uses it to fetch new short-lived
    access tokens whenever the current one expires.
    """
    from dropbox import Dropbox

    if refresh_token:
        if not app_key:
            raise ValueError(
                "A Dropbox refresh token needs the app key. Set DROPBOX_APP_KEY "
                "(and DROPBOX_APP_SECRET unless the token came from a PKCE flow)."
            )
        return Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key,
            app_secret=app_secret,
            max_retries_on_rate_limit=MAX_RETRIES_ON_RATE_LIMIT,
        )
    if access_token:
        return Dropbox(
            oauth2_access_token=access_token,
            max_retries_on_rate_limit=MAX_RETRIES_ON_RATE_LIMIT,
        )
    raise ValueError(
        "Dropbox credentials are missing. Set DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY, "
        "or DROPBOX_ACCESS_TOKEN for a quick try."
    )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def _normalize_path(path: str) -> str:
    """Return the lower-case form Dropbox uses in ``path_lower``.

    The Dropbox root is the empty string, so "/" and "" both become "".
    """
    normalized = path.strip().lower().rstrip("/")
    if normalized and not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def _normalize_folder_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Normalize, de-duplicate and drop folders nested inside another one.

    A nested folder would report the same files twice under two cursors, so
    only the outer folder is kept.
    """
    kept: list[str] = []
    # Sorted, so a parent always comes before its children.
    for path in sorted({_normalize_path(p) for p in paths}):
        if not any(_is_under(path, parent) for parent in kept):
            kept.append(path)
    return tuple(kept)


def _is_under(path: str, folder: str) -> bool:
    """True when ``path`` is ``folder`` itself or anything inside it."""
    return folder == "" or path == folder or path.startswith(folder + "/")


# ---------------------------------------------------------------------------
# Dropbox errors
# ---------------------------------------------------------------------------
def _path_lookup_error(exc: ApiError) -> Any:
    """Return the LookupError inside a list/download/export error, if any."""
    error = exc.error
    if hasattr(error, "is_path") and error.is_path():
        return error.get_path()
    return None


def _is_not_found(exc: ApiError) -> bool:
    lookup = _path_lookup_error(exc)
    return lookup is not None and lookup.is_not_found()


def _is_reset(exc: ApiError) -> bool:
    error = exc.error
    return hasattr(error, "is_reset") and error.is_reset()


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def _list_folder(client, path: str) -> tuple[list, str]:
    """Full recursive listing of one folder. Returns (entries, cursor)."""
    result = client.files_list_folder(
        path,
        recursive=True,
        limit=LIST_FOLDER_LIMIT,
        include_non_downloadable_files=True,
    )
    entries = list(result.entries)
    while result.has_more:
        result = client.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return entries, result.cursor


def _list_changes(client, cursor: str) -> tuple[list, str]:
    """Every change since ``cursor``. Returns (entries, new cursor)."""
    entries: list = []
    while True:
        result = client.files_list_folder_continue(cursor)
        entries.extend(result.entries)
        cursor = result.cursor
        # The cursor is only complete once has_more is false.
        if not result.has_more:
            return entries, cursor


# ---------------------------------------------------------------------------
# Sync state machine (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
def _iter_rows(client, config: _DropboxConfig, state: dict, stats: dict[str, int] | None = None):
    """Yield one row per new or changed file, plus hard-delete tombstones.

    ``state`` holds ``cursors`` (folder path -> list_folder cursor) and
    ``index`` (file path_lower -> file id).  It is a plain dict standing in for
    dlt's resource state, so this is directly unit-testable with a fake client.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(scanned=0, skipped=0, failed=0, deleted=0)

    old_cursors: dict[str, str] = dict(state.get("cursors", {}))
    index: dict[str, str] = dict(state.get("index", {}))
    new_cursors: dict[str, str] = {}
    # Ids whose path went away this round. Held until every folder is read,
    # because a move shows up as a deleted old path plus the same id elsewhere.
    removed_ids: set[str] = set()
    # Latest metadata of every file added or changed this round, by id.
    changed: dict[str, Any] = {}

    def forget(folder: str) -> None:
        for path in [p for p in index if _is_under(p, folder)]:
            removed_ids.add(index.pop(path))

    def apply(entry) -> None:
        if isinstance(entry, dropbox_files.FileMetadata):
            index[entry.path_lower] = entry.id
            changed[entry.id] = entry
        elif isinstance(entry, dropbox_files.DeletedMetadata):
            # A deleted folder is one entry, so forget everything under it.
            forget(entry.path_lower)
        # FolderMetadata needs no row: folder paths live inside file paths.

    # Folders dropped from the config: forget the files only they covered.
    # A folder that is still covered by a configured parent is re-listed below,
    # and its files reappear instead of being deleted.
    for folder in old_cursors:
        if folder not in config.folder_paths:
            forget(folder)

    for folder in config.folder_paths:
        cursor = old_cursors.get(folder)
        if cursor is not None:
            try:
                entries, new_cursors[folder] = _list_changes(client, cursor)
            except ApiError as exc:
                if _is_reset(exc):
                    logger.info("Dropbox: cursor for '%s' was reset; re-listing.", folder or "/")
                elif _path_lookup_error(exc) is not None:
                    # Confirm with a fresh listing before deleting anything.
                    logger.info("Dropbox: '%s' is not reachable; re-listing.", folder or "/")
                else:
                    raise
            else:
                for entry in entries:
                    apply(entry)
                continue

        # First run, reset cursor, or a path error to confirm: list the whole
        # folder and reconcile it against the index like a snapshot.
        try:
            entries, new_cursors[folder] = _list_folder(client, folder)
        except ApiError as exc:
            if not _is_not_found(exc):
                raise
            logger.warning("Dropbox: folder '%s' was not found; forgetting its files.", folder)
            forget(folder)
            continue
        forget(folder)
        for entry in entries:
            apply(entry)

    present_ids = set(index.values())
    # This also tombstones a file added and deleted within the round. That is
    # a no-op if it was never stored, but after a failed run it may have been
    # stored without the index being saved, and must not be left behind.
    for file_id in sorted(removed_ids - present_ids):
        stats["deleted"] += 1
        yield {"id": file_id, "_deleted": True}

    yielded = 0
    for file_id, entry in changed.items():
        # Added and then deleted within the same round: nothing to store.
        if file_id not in present_ids:
            continue
        row = _file_to_row(client, entry, config, stats)
        if row is not None:
            yielded += 1
            yield row

    # Always save the index: rows from this round may already be stored, and a
    # file that is only in memory must stay findable so its deletion can be
    # mapped back to its id.  Replaying the same changes onto this index next
    # run ends in the same index, so holding back only the cursor is safe.
    state["index"] = index
    if stats["failed"]:
        # Keep the old cursors so the failed files are retried next run
        # instead of being lost behind an advanced cursor.
        logger.warning(
            "Dropbox: %d file(s) failed to download; they will be retried next run.",
            stats["failed"],
        )
    else:
        state["cursors"] = new_cursors
    logger.info(
        "Dropbox: sync yielded %d changed file(s), %d deletion(s).", yielded, stats["deleted"]
    )


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------
def _file_to_row(client, entry, config: _DropboxConfig, stats: dict[str, int]) -> dict | None:
    stats["scanned"] += 1
    name = entry.name

    export_format = None
    if entry.is_downloadable:
        extension = posixpath.splitext(name)[1].lower()
        if extension in TEXT_EXTENSIONS:
            parse = _decode
        elif extension == PDF_EXTENSION:
            parse = _extract_pdf_text
        else:
            _count_skip(stats, "unsupported_type")
            logger.warning("Skipping unsupported Dropbox file '%s' (%s).", name, entry.id)
            return None
        if entry.size > config.max_file_size_mb * 1024 * 1024:
            _count_skip(stats, "too_large")
            logger.warning(
                "Skipping Dropbox file '%s' (%s): size exceeds max_file_size_mb=%d.",
                name,
                entry.id,
                config.max_file_size_mb,
            )
            return None
    else:
        export_format = _pick_export_format(entry.export_info)
        if export_format is None:
            _count_skip(stats, "unsupported_type")
            logger.warning(
                "Skipping Dropbox file '%s' (%s): no text export format.", name, entry.id
            )
            return None
        parse = _decode

    try:
        data = _fetch_bytes(client, entry.id, export_format)
    except ApiError as exc:
        lookup = _path_lookup_error(exc)
        if lookup is not None:
            # A path error repeats every run: the file is gone since it was
            # listed (the next sync reports the deletion) or can never be
            # downloaded, like restricted content. Skip it, don't retry it.
            _count_skip(stats, "not_found" if lookup.is_not_found() else "unavailable")
            logger.warning("Skipping Dropbox file '%s' (%s): %s", name, entry.id, exc)
            return None
        stats["failed"] += 1
        logger.warning("Dropbox file '%s' (%s) failed to download: %s", name, entry.id, exc)
        return None
    except Exception as exc:
        stats["failed"] += 1
        logger.warning("Dropbox file '%s' (%s) failed to download: %s", name, entry.id, exc)
        return None

    try:
        text = parse(data)
    except Exception as exc:
        # A corrupt file fails the same way every time, so skip it rather than
        # blocking the cursor forever.
        _count_skip(stats, "unparseable")
        logger.warning("Skipping Dropbox file '%s' (%s): could not parse: %s", name, entry.id, exc)
        return None
    if not text.strip():
        _count_skip(stats, "empty_content")
        return None

    # Document-mode row contract: {id, title, content, url}.  resolve_dlt_sources
    # tags these rows system_metadata["source"]="dropbox" (see the
    # DOCUMENT_SOURCE_ATTR marker below), so each file flows through normal
    # cognify (LLM graph extraction) rather than the relational schema path.
    return {
        "id": entry.id,
        "title": name,
        "content": _render_content(entry, text),
        # Dropbox paths are relative to the app folder for App folder apps, so
        # there is no reliable web link to build.
        "url": None,
        "_deleted": False,
    }


def _pick_export_format(export_info) -> str | None:
    if export_info is None:
        return None
    offered = [export_info.export_as, *(export_info.export_options or [])]
    for export_format in TEXT_EXPORT_FORMATS:
        if export_format in offered:
            return export_format
    return None


def _fetch_bytes(client, file_id: str, export_format: str | None) -> bytes:
    if export_format is None:
        _, response = client.files_download(file_id)
    else:
        _, response = client.files_export(file_id, export_format=export_format)
    try:
        return response.content
    finally:
        response.close()


def _render_content(entry, text: str) -> str:
    path = entry.path_display or entry.path_lower
    folder = posixpath.dirname(path) or "/"
    return f"Dropbox file: {path}\nFolder: {folder}\n\n{text}"


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _count_skip(stats: dict[str, int], reason: str) -> None:
    stats["skipped"] += 1
    key = f"skipped_{reason}"
    stats[key] = stats.get(key, 0) + 1


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def dropbox_source(
    folder_paths: list[str] | str | None = None,
    *,
    resource_name: str = "dropbox_files",
    check_active: Callable[[], None] | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    app_key: str | None = None,
    app_secret: str | None = None,
    max_file_size_mb: int | None = None,
    client: Any = None,
):
    """Return a ``dlt`` resource yielding one row per new or changed Dropbox file.

    Any argument left as ``None`` falls back to the matching ``DROPBOX_*``
    environment variable.  Hand the result to ``cognee.remember(...)`` with
    ``write_disposition="merge"`` and ``primary_key="id"``.

    Args:
        folder_paths: Folders to sync recursively, like ``["/Notes", "/Work"]``.
            Defaults to the whole Dropbox (the app folder for App folder apps).
            ``DROPBOX_FOLDER_PATHS`` takes a comma-separated list.
        resource_name: Stable dlt resource name. Give each source its own name
            when several sync into one dataset, so their state stays separate.
        check_active: Optional host authorization checkpoint during extraction.
        access_token: Short-lived access token, for quick local tries.
        refresh_token: Long-lived refresh token (preferred).
        app_key: Dropbox app key, needed with a refresh token.
        app_secret: Dropbox app secret; leave unset for a PKCE refresh token.
        max_file_size_mb: Skip files larger than this (default 25).
        client: Pre-built Dropbox client. Mainly an injection point for tests;
            when omitted a client is built from the auth settings above.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Dropbox sync requires a Cognee build with table-scoped DLT document cleanup. "
            "Upgrade Cognee before syncing to avoid deleting another source's data."
        )

    if folder_paths is None:
        env_paths = os.getenv("DROPBOX_FOLDER_PATHS", "")
        folder_paths = [p for p in env_paths.split(",") if p.strip()] or [""]
    elif isinstance(folder_paths, str):
        folder_paths = [folder_paths]
    if not folder_paths:
        # An empty list would sync nothing and forget every file already
        # synced, so refuse it instead of silently wiping memory.
        raise ValueError(
            'folder_paths is empty. Pass folders like ["/Notes"], or [""] for the whole Dropbox.'
        )

    config = _DropboxConfig(
        folder_paths=_normalize_folder_paths(folder_paths),
        max_file_size_mb=(
            max_file_size_mb
            if max_file_size_mb is not None
            else int(os.getenv("DROPBOX_MAX_FILE_SIZE_MB", "25"))
        ),
    )
    # Building the client makes no network call, so bad settings fail here
    # instead of halfway through a pipeline run.
    dropbox_client = client or build_dropbox_client(
        access_token=access_token or os.getenv("DROPBOX_ACCESS_TOKEN"),
        refresh_token=refresh_token or os.getenv("DROPBOX_REFRESH_TOKEN"),
        app_key=app_key or os.getenv("DROPBOX_APP_KEY"),
        app_secret=app_secret or os.getenv("DROPBOX_APP_SECRET"),
    )

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        # `_deleted` is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def dropbox_files():
        yield from dlt_utils.guarded_rows(
            _iter_rows(dropbox_client, config, dlt.current.resource_state(), stats),
            check_active,
        )

    resource = dropbox_files()
    # Opt into the document ingestion path: each file row (id/title/content/url)
    # becomes a text document that flows through normal cognify (LLM graph
    # extraction). resolve_dlt_sources reads this marker; it never imports this
    # connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, "dropbox")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never file names or content.
    resource.cognee_sync_stats = stats
    return resource
