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
import math
import posixpath
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dropbox import Dropbox
from dropbox import files as dropbox_files
from dropbox.exceptions import ApiError, AuthError, HttpError, RateLimitError
from pypdf import PdfReader
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
# Rate limits and 5xx errors are retried here, not by the SDK, which sleeps
# without asking check_active while cognee holds its dlt staging lock.  One
# wait is capped at a minute, and all waits of a run share a budget of two;
# past either the run stops cleanly and the next sync picks up from there.
MAX_RATE_LIMIT_WAIT_SECONDS = 60
RATE_LIMIT_WAIT_BUDGET_SECONDS = 120
# Retries per request, for a 429 or a 5xx.  Three short waits ride out a
# passing burst; anything longer is left to the next run.
RATE_LIMIT_RETRIES = 3
# The SDK's own wait when a 429 comes without Retry-After.
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 5
# check_active is asked between steps of a wait, so a long wait can be
# cancelled within a second.
WAIT_STEP_SECONDS = 1
# A file that fails this many runs in a row is skipped as unavailable, so an
# error that never goes away does not cost requests on every run forever.
# Three runs are a day or more on a daily sync, longer than a Dropbox outage
# usually lasts.
MAX_RETRY_ATTEMPTS = 3
# dlt uses the resource name as a table name and a state key, so it has to be
# short enough to be part of a file name; _valid_resource_name also checks dlt
# would keep it unchanged.
_RESOURCE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Patched in tests so rate-limit waits take no time.
_sleep = time.sleep


class DropboxRateLimitedError(Exception):
    """Dropbox kept rate limiting, or asked for a longer wait than we accept."""


class DropboxFolderNotFoundError(ValueError):
    """A configured folder does not exist in Dropbox on its first sync."""


@dataclass(frozen=True, kw_only=True)
class _DropboxConfig:
    folder_paths: tuple[str, ...]
    max_file_size_mb: float


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
            # 0 hands every 429 and 5xx to _Session.call, which caps the wait.
            max_retries_on_rate_limit=0,
            max_retries_on_error=0,
        )
    return Dropbox(
        oauth2_access_token=access_token, max_retries_on_rate_limit=0, max_retries_on_error=0
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
# Dropbox errors and calls
# ---------------------------------------------------------------------------
def _path_lookup_error(exc: ApiError) -> Any:
    """Return the LookupError inside a list/download/export error, if any."""
    error = exc.error
    if hasattr(error, "is_path") and error.is_path():
        return error.get_path()
    return None


def _is_gone(lookup: Any) -> bool:
    """Dropbox says the path is not there, or is no longer a folder."""
    return lookup.is_not_found() or lookup.is_not_folder()


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
    """One run's access to Dropbox: the host checkpoint and the wait budget."""

    def __init__(self, client: Any, check_active: Callable[[], None] | None) -> None:
        self.client = client
        self.check_active = check_active
        self.wait_left: float = RATE_LIMIT_WAIT_BUDGET_SECONDS

    def call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run one API call after the host's authorization checkpoint.

        ``check_active`` runs outside the ``try``, so whatever it raises
        reaches the host untouched.  A 429 or a 5xx is retried after a short
        wait that comes out of the run's budget.  A long or repeated rate
        limit raises ``DropboxRateLimitedError`` instead of sleeping through
        it; a 5xx that outlasts its retries is raised as it came.
        """
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            if self.check_active is not None:
                self.check_active()
            try:
                return method(*args, **kwargs)
            except RateLimitError as exc:
                wait = exc.backoff if exc.backoff is not None else DEFAULT_RATE_LIMIT_WAIT_SECONDS
                if not self._can_wait(wait, attempt):
                    raise DropboxRateLimitedError(
                        f"Dropbox is rate limiting; it asked to wait {wait:.0f}s."
                    ) from None
            except HttpError as exc:
                # 1, 2, then 4 seconds: what the SDK would do, minus its jitter.
                wait = 2**attempt
                if exc.status_code < 500 or not self._can_wait(wait, attempt):
                    raise
            self._wait(wait)
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _can_wait(self, wait: float, attempt: int) -> bool:
        return (
            math.isfinite(wait)
            and wait <= MAX_RATE_LIMIT_WAIT_SECONDS
            and wait <= self.wait_left
            and attempt < RATE_LIMIT_RETRIES
        )

    def _wait(self, seconds: float) -> None:
        """Sleep out of the run's budget, asking ``check_active`` between steps."""
        seconds = max(seconds, 0)
        self.wait_left -= seconds
        while seconds > 0:
            step = min(seconds, WAIT_STEP_SECONDS)
            _sleep(step)
            seconds -= step
            if seconds > 0 and self.check_active is not None:
                self.check_active()


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def _next_page(
    session: _Session, result: dropbox_files.ListFolderResult, seen: set[str]
) -> dropbox_files.ListFolderResult:
    """Follow ``has_more``, refusing a cursor Dropbox already gave us."""
    if result.cursor in seen:
        raise RuntimeError("Dropbox returned the same cursor twice; stopping instead of looping.")
    seen.add(result.cursor)
    return session.call(session.client.files_list_folder_continue, result.cursor)


def _list_folder(session: _Session, path: str) -> tuple[list[dropbox_files.Metadata], str]:
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


def _list_changes(session: _Session, cursor: str) -> tuple[list[dropbox_files.Metadata], str]:
    """Every change since ``cursor``. Returns (entries, new cursor)."""
    result = session.call(session.client.files_list_folder_continue, cursor)
    entries = list(result.entries)
    seen = {cursor}
    # The cursor is only complete once has_more is false.
    while result.has_more:
        result = _next_page(session, result, seen)
        entries.extend(result.entries)
    return entries, result.cursor


def _get_metadata(session: _Session, file_id: str) -> dropbox_files.FileMetadata | None:
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
    client: Any,
    config: _DropboxConfig,
    state: dict[str, Any],
    stats: dict[str, int] | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one row per new or changed file, plus hard-delete tombstones.

    ``state`` holds ``cursors`` (folder path -> list_folder cursor), ``index``
    (file path_lower -> file id), ``stored`` (ids that are in cognee),
    ``synced_folders`` (folders listed at least once) and ``retry`` (file id ->
    failed attempts).  It is a plain dict standing in for dlt's resource state,
    so this is directly unit-testable with a fake client.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(scanned=0, skipped=0, failed=0, deleted=0, failed_auth=0, failed_rate_limit=0)
    session = _Session(client, check_active)

    old_cursors: dict[str, str] = dict(state.get("cursors", {}))
    index: dict[str, str] = dict(state.get("index", {}))
    saved_retry = state.get("retry", {})
    # Before attempts were counted, retry was a plain list of ids.
    retry: dict[str, int] = (
        dict.fromkeys(saved_retry, 1) if isinstance(saved_retry, list) else dict(saved_retry)
    )
    # Skipped and failed files are indexed too, so the index alone can't say
    # what is in cognee, and a folder listed once and gone later is forgotten
    # like a delete while one never listed is taken for a typo.  State saved
    # before synced_folders existed is migrated by _migrated_synced_folders.
    synced_folders: set[str] = (
        set(state["synced_folders"])
        if "synced_folders" in state
        else _migrated_synced_folders(index, old_cursors, config.folder_paths)
    )
    if "stored" in state:
        stored: set[str] = set(state["stored"])
    else:
        # Saved before stored existed: the index can't tell which files made it
        # into cognee, so list every folder again and let the rows rebuild it.
        stored = set()
        old_cursors = {}
    new_cursors: dict[str, str] = {}
    # Ids whose path went away this round. Held until every folder is read,
    # because a move shows up as a deleted old path plus the same id elsewhere.
    removed_ids: set[str] = set()
    # Latest metadata of every file added or changed this round, by id.
    changed: dict[str, dropbox_files.FileMetadata] = {}

    def forget(folder: str) -> None:
        for path in [p for p in index if _is_under(p, folder)]:
            removed_ids.add(index.pop(path))

    def apply(entry: dropbox_files.Metadata) -> None:
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

    def list_all() -> None:
        # Files no configured folder covers any more: their folder was dropped
        # from the config.  Walking the index rather than the saved cursors
        # also catches a folder whose cursor was never saved.  A folder still
        # covered by a configured parent is re-listed below, and its files
        # reappear instead of being deleted.
        for path in [p for p in index if not any(_is_under(p, f) for f in config.folder_paths)]:
            removed_ids.add(index.pop(path))

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

            # First run, reset cursor, or a path error to confirm: list the
            # whole folder and reconcile it against the index like a snapshot.
            try:
                entries, new_cursors[folder] = _list_folder(session, folder)
            except ApiError as exc:
                lookup = _path_lookup_error(exc)
                if lookup is None:
                    raise
                if folder not in synced_folders and (
                    _is_gone(lookup) or lookup.is_restricted_content()
                ):
                    # Never listed: most likely a typo in folder_paths, which
                    # would otherwise just produce an empty dataset.
                    raise DropboxFolderNotFoundError(
                        "A configured Dropbox folder was not found, or is not a folder "
                        "you can read. Check folder_paths."
                    ) from None
                if not _is_gone(lookup):
                    # Locked, restricted, malformed or a tag Dropbox adds later:
                    # nothing says the files are gone, so raise like any other
                    # listing error and leave the state as it was.
                    raise
                # Synced before and now deleted or replaced by a file: that
                # does not come back by retrying, and raising would stop every
                # other folder on every run, so forget it like a delete.
                logger.warning("Dropbox: a synced folder is gone; forgetting it.")
                forget(folder)
                continue
            synced_folders.add(folder)
            forget(folder)
            for entry in entries:
                apply(entry)

    # Listing comes before anything is emitted, and state is only written at
    # the end, so stopping here leaves the previous state exactly as it was.
    try:
        list_all()
    except DropboxRateLimitedError:
        stats["failed_rate_limit"] += 1
        logger.warning("Dropbox: rate limited while listing; the next run lists again.")
        return
    except AuthError:
        # Nothing was done yet, so this is just a bad token: say so.
        stats["failed_auth"] += 1
        raise

    rows_stored = 0

    def tombstone(file_id: str) -> dict[str, Any]:
        if file_id in stored:
            stored.discard(file_id)
            stats["deleted"] += 1
        return {"id": file_id, "_deleted": True}

    def work_done() -> int:
        # Rows and real deletes only: a tombstone for an id that was never
        # stored changes nothing, so it must not hide a bad token.
        return rows_stored + stats["deleted"]

    present_ids = set(index.values())
    # This also tombstones a file added and deleted within the round. That is
    # a no-op if it was never stored, but after a failed run it may have been
    # stored without the state being saved, and must not be left behind.
    for file_id in sorted(removed_ids - present_ids):
        yield tombstone(file_id)

    # Files changed this round, then files that failed before and did not
    # change since (None: their metadata is fetched again by id).  Added and
    # then deleted within the round, or gone since they failed: nothing to do.
    work: list[tuple[str, dropbox_files.FileMetadata | None]] = list(changed.items())
    work += [(file_id, None) for file_id in retry if file_id not in changed]
    work = [(file_id, entry) for file_id, entry in work if file_id in present_ids]

    def attempts_so_far(file_id: str) -> int:
        # A new revision starts over: its failures are its own.
        return 0 if file_id in changed else retry.get(file_id, 0)

    next_retry: dict[str, int] = {}

    def failed(file_id: str) -> bool:
        """Count a failure; True once the file is given up for good."""
        attempts = attempts_so_far(file_id) + 1
        if attempts >= MAX_RETRY_ATTEMPTS:
            _count_skip(stats, "unavailable")
            return True
        stats["failed"] += 1
        next_retry[file_id] = attempts
        return False

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
            next_retry.update({file_id: attempts_so_far(file_id) for file_id in remaining})
            if isinstance(exc, AuthError):
                stats["failed"] += len(remaining)
                stats["failed_auth"] += len(remaining)
                if not work_done():
                    # Nothing done yet: that's just a bad token, say so.
                    raise
            else:
                # A pause, not a failure: failed_rate_limit says why it stopped.
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
            row = _RETRY
        if row is _RETRY:
            if failed(file_id) and file_id in stored:
                # Given up: its stored text is no longer current, so it goes.
                yield tombstone(file_id)
        elif row is not None:
            stored.add(file_id)
            rows_stored += 1
            yield row
        elif file_id in stored:
            # Stored before but skipped now (emptied, too large, renamed to an
            # unsupported type): its old text must not stay in memory.
            yield tombstone(file_id)

    # The cursors always move forward: files that failed are kept by id in
    # ``retry`` instead, so one bad file never makes later runs replay
    # everything since it failed.
    state["index"] = index
    state["cursors"] = new_cursors
    state["stored"] = sorted(stored)
    # Only configured folders: one dropped from the config and added back later
    # must pass the typo check again instead of counting as listed.
    state["synced_folders"] = sorted(synced_folders & set(config.folder_paths))
    state["retry"] = next_retry
    if next_retry:
        logger.warning("Dropbox: %d file(s) failed and will be retried next run.", len(next_retry))
    logger.info("Dropbox: sync stats %s", stats)


def _migrated_synced_folders(
    index: dict[str, str], old_cursors: dict[str, str], folder_paths: Iterable[str]
) -> set[str]:
    """Folders that state saved before ``synced_folders`` existed had listed.

    The first version saved no cursors after a run with a failed download, so
    a folder counts as listed when it has a cursor or any file in the index.
    """
    return set(old_cursors) | {
        folder for folder in folder_paths if any(_is_under(path, folder) for path in index)
    }


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------
# Returned by _file_to_row when a download failed in a way worth retrying.
_RETRY = object()


def _file_to_row(
    session: _Session,
    entry: dropbox_files.FileMetadata,
    config: _DropboxConfig,
    stats: dict[str, int],
) -> dict[str, Any] | object | None:
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
    except Exception:  # noqa: BLE001
        # One unit on purpose: on malformed files pypdf raises KeyError,
        # TypeError, AttributeError, zlib.error or RecursionError besides its
        # own errors, and our decode raises ValueError.  A corrupt file fails
        # the same way every time, so skip it rather than break every run.
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


def _pick_export_format(export_info: dropbox_files.ExportInfo | None) -> str | None:
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


def _render_content(entry: dropbox_files.FileMetadata, text: str) -> str:
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
def _valid_resource_name(name: Any) -> bool:
    """A name dlt keeps as it is.

    The table follows dlt's normalized name but the pipeline scope follows the
    name itself, so ``work__dbx`` and ``work_dbx`` would share one table with
    separate state.  An empty name would drop the pipeline scope altogether.
    """
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    return (
        isinstance(name, str)
        and _RESOURCE_NAME.fullmatch(name) is not None
        and NamingConvention().normalize_identifier(name) == name
    )


def dropbox_source(
    folder_paths: list[str] | str | None = None,
    *,
    resource_name: str = "dropbox_files",
    check_active: Callable[[], None] | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    app_key: str | None = None,
    app_secret: str | None = None,
    max_file_size_mb: float = DEFAULT_MAX_FILE_SIZE_MB,
    service: Any = None,
    client: Any = None,
) -> Any:
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
        max_file_size_mb: Skip files larger than this.  Must be above 0:
            with 0 every file would be skipped, and every stored file forgotten.
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
    if not _valid_resource_name(resource_name):
        raise ValueError(
            "resource_name must be 1 to 64 lowercase letters, digits and single "
            'underscores, starting with a letter and not ending in one, like "work_dropbox".'
        )

    if (
        isinstance(max_file_size_mb, bool)
        or not isinstance(max_file_size_mb, (int, float))
        or not math.isfinite(max_file_size_mb)
        or max_file_size_mb <= 0
    ):
        # 0 or less would skip every file and forget everything already stored.
        raise ValueError("max_file_size_mb must be a number above 0, like 25.")

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
    def dropbox_files() -> Iterator[dict[str, Any]]:
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
