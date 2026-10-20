"""Dropbox connector for cognee — a ``dlt`` source that turns Dropbox folders into memory.

Sync one or more Dropbox folders (or the whole Dropbox) into cognee,
incrementally and with forget-on-deletion.  The resource produced here is
handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_dropbox import dropbox_source

    await cognee.remember(
        dropbox_source(folder_paths=["/Notes"], refresh_token=..., app_key=...),
        dataset_name="my_dropbox",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by Dropbox file id
        max_rows_per_table=0,        # 0 = no row cap (folders often exceed the default 50)
    )

Files are keyed by their Dropbox ``id``, which survives renames and moves.  One
``list_folder`` cursor per folder makes later runs incremental, and a
``path_lower -> id`` index in dlt state turns Dropbox's path-only deletes back
into ids.  Only a confirmed ``not_found`` removes data.  The README explains the
sync model, the retry rules and the limitations.
"""

import codecs
import io
import posixpath
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dropbox import Dropbox
from dropbox import files as dropbox_files
from dropbox.exceptions import ApiError, AuthError, HttpError, RateLimitError
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from requests import RequestException

logger = get_logger("dropbox_connector")

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv"}
PDF_EXTENSION = ".pdf"
# Paper docs export as markdown (or html, which we skip).  Other
# non-downloadable files only offer binary formats and are skipped.
TEXT_EXPORT_FORMATS = ("markdown",)
# The largest page size list_folder accepts.
LIST_FOLDER_LIMIT = 2000
# Our own choice: big enough for long PDFs, small enough that one file can't
# take over a cognify run.  Dropbox has no limit of its own to follow here.
DEFAULT_MAX_FILE_SIZE_MB = 25
# Rate limits are handled here, not by the SDK, which would sleep the full
# Retry-After while cognee holds its dlt staging lock.  One wait is capped at a
# minute, and all waits of a run share a budget of two; past either the run
# stops cleanly and the next sync picks up from there.
MAX_RATE_LIMIT_WAIT_SECONDS = 60
RATE_LIMIT_WAIT_BUDGET_SECONDS = 120
RATE_LIMIT_RETRIES = 3
# The SDK's own wait when a 429 comes without Retry-After.
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 5
# A file that fails this many runs in a row is skipped as unavailable, so an
# error that never goes away does not cost requests on every run forever.
MAX_RETRY_ATTEMPTS = 3
# dlt uses the resource name as a table name and a state key, so it has to be
# already normalized and short enough to be part of a file name.
_RESOURCE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Patched in tests so rate-limit waits take no time.
_sleep = time.sleep


class DropboxRateLimitedError(Exception):
    """Dropbox kept rate limiting, or asked for a longer wait than we accept."""


class DropboxFolderNotFoundError(ValueError):
    """A configured folder does not exist in Dropbox on its first sync."""


@dataclass(frozen=True)
class _DropboxConfig:
    folder_paths: tuple[str, ...]
    max_file_size_mb: int


# ---------------------------------------------------------------------------
# Auth / client construction
# ---------------------------------------------------------------------------
def _check_credentials(
    access_token: str | None, refresh_token: str | None, app_key: str | None
) -> None:
    """Fail when the source is built, not halfway through a pipeline run."""
    if refresh_token and not app_key:
        raise ValueError(
            "A Dropbox refresh token needs the app key. Pass app_key "
            "(and app_secret unless the token came from a PKCE flow)."
        )
    if not (refresh_token or access_token):
        raise ValueError(
            "Dropbox credentials are missing. Pass refresh_token and app_key, "
            "or access_token for a quick try."
        )


def build_dropbox_client(
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
    app_key: str | None = None,
    app_secret: str | None = None,
) -> Dropbox:
    """Build an authenticated Dropbox API client.

    A refresh token is preferred: the SDK uses it to fetch new short-lived
    access tokens whenever the current one expires.
    """
    _check_credentials(access_token, refresh_token, app_key)
    if refresh_token:
        return Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key,
            app_secret=app_secret,
            # 0 hands every 429 to _Session.call, which caps the wait.
            max_retries_on_rate_limit=0,
        )
    return Dropbox(oauth2_access_token=access_token, max_retries_on_rate_limit=0)


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
# Dropbox errors and calls
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


def _is_unsupported(exc: ApiError) -> bool:
    """A file Dropbox will never download or export: retrying cannot help."""
    error = exc.error
    return any(
        getattr(error, check, lambda: False)()
        for check in ("is_unsupported_file", "is_non_exportable", "is_invalid_export_format")
    )


class _Session:
    """One run's access to Dropbox: the host checkpoint and the rate-limit budget."""

    def __init__(self, client: Any, check_active: Callable[[], None] | None):
        self.client = client
        self.check_active = check_active
        self.wait_left = RATE_LIMIT_WAIT_BUDGET_SECONDS

    def call(self, method: Callable, *args: Any, **kwargs: Any) -> Any:
        """Run one API call after the host's authorization checkpoint.

        ``check_active`` runs outside the ``try``, so whatever it raises
        reaches the host untouched.  A 429 is retried after a short wait that
        comes out of the run's budget; a long or repeated rate limit raises
        ``DropboxRateLimitedError`` instead of sleeping through it.
        """
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            if self.check_active is not None:
                self.check_active()
            try:
                return method(*args, **kwargs)
            except RateLimitError as exc:
                wait = exc.backoff if exc.backoff is not None else DEFAULT_RATE_LIMIT_WAIT_SECONDS
                if (
                    wait > MAX_RATE_LIMIT_WAIT_SECONDS
                    or wait > self.wait_left
                    or attempt == RATE_LIMIT_RETRIES
                ):
                    raise DropboxRateLimitedError(
                        f"Dropbox is rate limiting; it asked to wait {wait:.0f}s."
                    ) from None
                self.wait_left -= wait
                _sleep(wait)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def _next_page(session: _Session, result, seen: set[str]):
    """Follow ``has_more``, refusing a cursor Dropbox already gave us."""
    if result.cursor in seen:
        raise RuntimeError("Dropbox returned the same cursor twice; stopping instead of looping.")
    seen.add(result.cursor)
    return session.call(session.client.files_list_folder_continue, result.cursor)


def _list_folder(session: _Session, path: str) -> tuple[list, str]:
    """Full recursive listing of one folder. Returns (entries, cursor)."""
    result = session.call(
        session.client.files_list_folder,
        path,
        recursive=True,
        limit=LIST_FOLDER_LIMIT,
        include_non_downloadable_files=True,
    )
    entries = list(result.entries)
    seen: set[str] = set()
    while result.has_more:
        result = _next_page(session, result, seen)
        entries.extend(result.entries)
    return entries, result.cursor


def _list_changes(session: _Session, cursor: str) -> tuple[list, str]:
    """Every change since ``cursor``. Returns (entries, new cursor)."""
    result = session.call(session.client.files_list_folder_continue, cursor)
    entries = list(result.entries)
    seen = {cursor}
    # The cursor is only complete once has_more is false.
    while result.has_more:
        result = _next_page(session, result, seen)
        entries.extend(result.entries)
    return entries, result.cursor


def _get_metadata(session: _Session, file_id: str) -> Any:
    """Current metadata of a file kept for retry, or None once it is gone."""
    try:
        meta = session.call(session.client.files_get_metadata, file_id)
    except ApiError as exc:
        if _is_not_found(exc):
            # Gone since it failed: the listing reports the delete.
            return None
        raise
    return meta if isinstance(meta, dropbox_files.FileMetadata) else None


# ---------------------------------------------------------------------------
# Sync state machine (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
def _iter_rows(
    client,
    config: _DropboxConfig,
    state: dict,
    stats: dict[str, int] | None = None,
    check_active: Callable[[], None] | None = None,
):
    """Yield one row per new or changed file, plus hard-delete tombstones.

    ``state`` holds ``cursors`` (folder path -> list_folder cursor), ``index``
    (file path_lower -> file id) and ``retry`` (file id -> failed attempts).
    It is a plain dict standing in for dlt's resource state, so this is
    directly unit-testable with a fake client.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(scanned=0, skipped=0, failed=0, deleted=0, failed_auth=0, failed_rate_limit=0)
    session = _Session(client, check_active)

    old_cursors: dict[str, str] = dict(state.get("cursors", {}))
    index: dict[str, str] = dict(state.get("index", {}))
    stored_before = set(index.values())
    saved_retry = state.get("retry", {})
    # Before attempts were counted, retry was a plain list of ids.
    retry: dict[str, int] = (
        dict.fromkeys(saved_retry, 1) if isinstance(saved_retry, list) else dict(saved_retry)
    )
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
        path = entry.path_lower
        if path is None:
            # Unmounted entries come without a path; there is nothing to index.
            logger.warning("Dropbox: skipped a change without a path.")
            return
        if isinstance(entry, dropbox_files.FileMetadata):
            previous = index.get(path)
            if previous is not None and previous != entry.id:
                # A new file written over a path whose old file was deleted,
                # without a delete in this round's changes.
                removed_ids.add(previous)
            index[path] = entry.id
            changed[entry.id] = entry
        elif isinstance(entry, dropbox_files.DeletedMetadata):
            # A deleted folder is one entry, so forget everything under it.
            forget(path)
        elif path in index:
            # A folder created where a file used to be.
            removed_ids.add(index.pop(path))

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
                entries, new_cursors[folder] = _list_changes(session, cursor)
            except ApiError as exc:
                if _is_reset(exc):
                    logger.info("Dropbox: a folder's cursor was reset; listing it again.")
                elif _path_lookup_error(exc) is not None:
                    # Confirm with a fresh listing before deleting anything.
                    logger.info("Dropbox: a synced folder is not reachable; listing it again.")
                else:
                    raise
            else:
                for entry in entries:
                    apply(entry)
                continue

        # First run, reset cursor, or a path error to confirm: list the whole
        # folder and reconcile it against the index like a snapshot.
        try:
            entries, new_cursors[folder] = _list_folder(session, folder)
        except ApiError as exc:
            if not _is_not_found(exc):
                raise
            if folder not in old_cursors:
                # Never synced: most likely a typo in folder_paths, which would
                # otherwise just produce an empty dataset.
                raise DropboxFolderNotFoundError(
                    "A configured Dropbox folder was not found. Check folder_paths."
                ) from None
            logger.warning("Dropbox: a synced folder was not found; forgetting its files.")
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

    # Files changed this round, then files that failed before and did not
    # change since (None: their metadata is fetched again by id).  Added and
    # then deleted within the round, or gone since they failed: nothing to do.
    work: list[tuple[str, Any]] = list(changed.items())
    work += [(file_id, None) for file_id in retry if file_id not in changed]
    work = [(file_id, entry) for file_id, entry in work if file_id in present_ids]

    next_retry: dict[str, int] = {}

    def failed(file_id: str) -> None:
        attempts = retry.get(file_id, 0) + 1
        if attempts >= MAX_RETRY_ATTEMPTS:
            _count_skip(stats, "unavailable")
        else:
            stats["failed"] += 1
            next_retry[file_id] = attempts

    yielded = 0
    for position, (file_id, entry) in enumerate(work):
        try:
            if entry is None:
                entry = _get_metadata(session, file_id)
                if entry is None:
                    continue
            row = _file_to_row(session, entry, config, stats)
        except (AuthError, DropboxRateLimitedError) as exc:
            # Every later request would fail the same way. Stop here, keep
            # what was stored, and fetch the rest next run without counting
            # this as one of their attempts.
            remaining = [file_id for file_id, _ in work[position:]]
            next_retry.update({file_id: retry.get(file_id, 0) for file_id in remaining})
            stats["failed"] += len(remaining)
            if isinstance(exc, AuthError):
                stats["failed_auth"] += len(remaining)
                if not yielded and not stats["deleted"]:
                    # Nothing done yet: that's just a bad token, say so.
                    raise
            else:
                stats["failed_rate_limit"] += len(remaining)
            logger.warning(
                "Dropbox: stopped early (%s); %d file(s) will be fetched next run.",
                type(exc).__name__,
                len(remaining),
            )
            break
        except (ApiError, HttpError, RequestException):
            # Only the metadata lookup of a retried file gets here; downloads
            # handle their own errors.
            failed(file_id)
            continue
        if row is _RETRY:
            failed(file_id)
        elif row is not None:
            yielded += 1
            yield row
        elif file_id in stored_before:
            # Synced before but skipped now (emptied, too large, renamed to an
            # unsupported type): its old text must not stay in memory.
            stats["deleted"] += 1
            yield {"id": file_id, "_deleted": True}

    # The cursors always move forward: files that failed are kept by id in
    # ``retry`` instead, so one bad file never makes later runs replay
    # everything since it failed.
    state["index"] = index
    state["cursors"] = new_cursors
    state["retry"] = next_retry
    if next_retry:
        logger.warning("Dropbox: %d file(s) failed and will be retried next run.", len(next_retry))
    logger.info("Dropbox: sync yielded %d changed file(s); stats %s", yielded, stats)


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------
# Returned by _file_to_row when a download failed in a way worth retrying.
_RETRY = object()


def _file_to_row(session: _Session, entry, config: _DropboxConfig, stats: dict[str, int]):
    """Download and render one file: a row, None when skipped, or _RETRY."""
    stats["scanned"] += 1
    name = entry.name

    # Checked first, so an exported Paper doc is held to the limit as well.
    if entry.size > config.max_file_size_mb * 1024 * 1024:
        _count_skip(stats, "too_large")
        return None
    export_format = None
    if entry.is_downloadable:
        extension = posixpath.splitext(name)[1].lower()
        if extension in TEXT_EXTENSIONS:
            parse = _decode
        elif extension == PDF_EXTENSION:
            parse = _extract_pdf_text
        else:
            _count_skip(stats, "unsupported_type")
            return None
    else:
        export_format = _pick_export_format(entry.export_info)
        if export_format is None:
            _count_skip(stats, "unsupported_type")
            return None
        parse = _decode

    try:
        data = _fetch_bytes(session, entry.id, export_format)
    except ApiError as exc:
        lookup = _path_lookup_error(exc)
        if lookup is not None:
            # A path error repeats every run: the file is gone since it was
            # listed (the next sync reports the deletion) or can never be
            # downloaded, like restricted content. Skip it, don't retry it.
            _count_skip(stats, "not_found" if lookup.is_not_found() else "unavailable")
            return None
        if _is_unsupported(exc):
            _count_skip(stats, "unsupported_type")
            return None
        return _RETRY
    except AuthError:
        # Not this file's fault: the caller stops the run.
        raise
    except (HttpError, RequestException):
        # 5xx after the SDK's own retries, or the network: worth another try.
        # Not OSError: a host's check_active may raise PermissionError or
        # TimeoutError, and that must stop the run, not count as a failure.
        return _RETRY

    try:
        text = parse(data)
    except (PyPdfError, ValueError):
        # A corrupt file fails the same way every time, so skip it rather than
        # retrying it forever.
        _count_skip(stats, "unparseable")
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


def _fetch_bytes(session: _Session, file_id: str, export_format: str | None) -> bytes:
    client = session.client
    if export_format is None:
        _, response = session.call(client.files_download, file_id)
    else:
        _, response = session.call(client.files_export, file_id, export_format=export_format)
    try:
        return response.content
    finally:
        response.close()


def _render_content(entry, text: str) -> str:
    path = entry.path_display or entry.path_lower
    folder = posixpath.dirname(path) or "/"
    return f"Dropbox file: {path}\nFolder: {folder}\n\n{text}"


def _decode(data: bytes) -> str:
    """Text from a .txt, .md or .csv file, or ValueError when it isn't text.

    Notepad saves UTF-16 with a BOM, which UTF-8 would turn into mojibake with
    a NUL after every letter.  NUL bytes also break a load into Postgres.
    """
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = data.decode("utf-16")
    else:
        text = data.decode("utf-8-sig", errors="replace")
    if "\x00" in text:
        raise ValueError("binary content")
    return text


def _extract_pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data), strict=False)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    # Some PDFs carry NUL bytes in their text layer; they break a Postgres load.
    return text.replace("\x00", "")


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
    max_file_size_mb: int = DEFAULT_MAX_FILE_SIZE_MB,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource yielding one row per new or changed Dropbox file.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.  Credentials are taken as arguments only; the
    example reads them from ``DROPBOX_*`` environment variables.

    Args:
        folder_paths: Folders to sync recursively, like ``["/Notes", "/Work"]``.
            Defaults to the whole Dropbox (the app folder for App folder apps).
            A folder that does not exist on its first sync raises
            ``DropboxFolderNotFoundError``.
        resource_name: Stable dlt resource name: lowercase letters, digits and
            underscores, up to 64 characters.  Give each source its own name
            when several sync into one dataset, so their state stays separate.
        check_active: Optional host authorization checkpoint, asked before
            every Dropbox request.
        access_token: Short-lived access token, for quick local tries.
        refresh_token: Long-lived refresh token (preferred).
        app_key: Dropbox app key, needed with a refresh token.
        app_secret: Dropbox app secret; leave unset for a PKCE refresh token.
        max_file_size_mb: Skip files larger than this.
        service: Pre-built Dropbox client, mainly for tests.  When omitted a
            client is built from the auth settings above for each run, and
            closed when the run ends.
        client: Older name for ``service``.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Dropbox sync requires a Cognee build with table-scoped DLT document cleanup. "
            "Upgrade Cognee before syncing to avoid deleting another source's data."
        )
    if not isinstance(resource_name, str) or not _RESOURCE_NAME.fullmatch(resource_name):
        # An empty name would silently drop the pipeline scope, and a name dlt
        # normalizes differently would share a table with another source.
        raise ValueError(
            "resource_name must be 1 to 64 lowercase letters, digits or underscores, "
            'starting with a letter, like "work_dropbox".'
        )

    if folder_paths is None:
        folder_paths = [""]
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
        max_file_size_mb=max_file_size_mb,
    )
    injected = service or client
    if injected is None:
        _check_credentials(access_token, refresh_token, app_key)

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
        dropbox_client = injected or build_dropbox_client(
            access_token=access_token,
            refresh_token=refresh_token,
            app_key=app_key,
            app_secret=app_secret,
        )
        try:
            yield from dlt_utils.guarded_rows(
                _iter_rows(
                    dropbox_client, config, dlt.current.resource_state(), stats, check_active
                ),
                check_active,
            )
        finally:
            if injected is None:
                # Built for this run, so its HTTP session is ours to close.
                dropbox_client.close()

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
