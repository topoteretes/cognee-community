"""Evernote data-source connector for cognee.

Extracts notes, notebooks, and tags from Evernote into cognee memory with
incremental synchronization via Evernote sync chunks and forget-on-delete.
"""

import time
from collections.abc import Callable, Generator
from html.parser import HTMLParser
from typing import Any

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from dlt.common.normalizers.naming.snake_case import NamingConvention
from evernote.edam.error.ttypes import (
    EDAMErrorCode,
    EDAMNotFoundException,
    EDAMSystemException,
    EDAMUserException,
)
from evernote.edam.notestore import NoteStore
from evernote.edam.notestore.ttypes import SyncChunkFilter
from evernote.edam.userstore import UserStore
from thrift.protocol.TBinaryProtocol import TBinaryProtocol
from thrift.Thrift import TException
from thrift.transport.THttpClient import THttpClient
from thrift.transport.TTransport import TTransportException


def _check_cognee_version() -> None:
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise ImportError(
            "cognee-community-connector-evernote requires a cognee version with "
            "DOCUMENT_SYNC_VERSION >= 1; please upgrade cognee."
        )


_check_cognee_version()

from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR  # noqa: E402

logger = get_logger(__name__)

STATE_VERSION = 1


class EvernoteError(Exception):
    """Base exception for Evernote connector."""


class EvernoteAuthError(EvernoteError):
    """Raised when authentication fails or token is invalid/expired."""


class EvernotePermissionError(EvernoteError):
    """Raised when access is denied or permissions are insufficient."""


class NoteNotFoundError(EvernoteError):
    """Raised when a requested note is not found on Evernote."""


class EvernoteRateLimitError(EvernoteError):
    """Raised when Evernote rate limit retries are exhausted or wait exceeds cap."""

    def __init__(self, message: str, retry_after: float | int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _SyncCancelledError(Exception):
    """Internal sentinel raised when check_active returns False during chunk iteration."""


def _error_code_name(code: Any) -> str:
    """Format an EDAM error code as 'NAME (VALUE)' or fallback to its string representation."""
    name = getattr(code, "name", None)
    if not name and hasattr(EDAMErrorCode, "_VALUES_TO_NAMES"):
        name = getattr(EDAMErrorCode, "_VALUES_TO_NAMES", {}).get(code)
    if not name:
        try:
            name = EDAMErrorCode(code).name
        except (ValueError, TypeError, KeyError):
            name = None
    if name:
        try:
            val = int(code)
            return f"{name} ({val})"
        except (ValueError, TypeError):
            return str(name)
    return str(code)


class EvernoteApi:
    """Wrapper around Thrift UserStore and NoteStore clients."""

    def __init__(
        self,
        token: str,
        sandbox: bool = False,
        user_store: Any = None,
        note_store: Any = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        timeout_seconds: float = 30,
        max_rate_limit_wait: float = 30,
    ) -> None:
        self._token = token
        self.sandbox = sandbox
        self.sleep_fn = sleep_fn
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self.max_rate_limit_wait = max_rate_limit_wait
        self._user_store = user_store
        self._note_store = note_store
        self._note_store_url: str | None = None
        self._user_id: int | None = None

    def __repr__(self) -> str:
        return f"EvernoteApi(sandbox={self.sandbox!r})"

    def _create_user_store(self) -> Any:
        host = "sandbox.evernote.com" if self.sandbox else "www.evernote.com"
        url = f"https://{host}/edam/user"
        transport = THttpClient(url)
        if self.timeout_seconds is not None:
            transport.setTimeout(int(self.timeout_seconds * 1000))
        protocol = TBinaryProtocol(transport)
        return UserStore.Client(protocol)

    def _create_note_store(self, url: str) -> Any:
        transport = THttpClient(url)
        if self.timeout_seconds is not None:
            transport.setTimeout(int(self.timeout_seconds * 1000))
        protocol = TBinaryProtocol(transport)
        return NoteStore.Client(protocol)

    def get_note_store_url(self) -> str:
        """Resolve and cache NoteStore URL lazily via UserStore.getUserUrls(token)."""
        if self._note_store_url is not None:
            return self._note_store_url

        user_store = self._user_store
        if user_store is None:
            user_store = self._create_user_store()
            self._user_store = user_store

        urls = self._call_with_retry(user_store.getUserUrls, self._token)
        self._note_store_url = str(urls.noteStoreUrl)
        return self._note_store_url

    def get_user_id(self) -> int:
        """Resolve and cache user ID lazily via UserStore.getUser(token)."""
        if self._user_id is not None:
            return self._user_id

        user_store = self._user_store
        if user_store is None:
            user_store = self._create_user_store()
            self._user_store = user_store

        user = self._call_with_retry(user_store.getUser, self._token)
        self._user_id = int(user.id)
        return self._user_id

    def get_note_store(self) -> Any:
        """Return NoteStore client, creating it lazily on first access."""
        if self._note_store is not None:
            return self._note_store

        url = self.get_note_store_url()
        self._note_store = self._create_note_store(url)
        return self._note_store

    def _handle_exception(self, exc: Exception, guid: str | None = None) -> None:
        if isinstance(exc, EDAMUserException):
            code_str = _error_code_name(exc.errorCode)
            if exc.errorCode in (EDAMErrorCode.INVALID_AUTH, EDAMErrorCode.AUTH_EXPIRED):
                raise EvernoteAuthError(f"Evernote authentication failed ({code_str})") from exc
            if exc.errorCode == EDAMErrorCode.PERMISSION_DENIED:
                raise EvernotePermissionError(f"Evernote permission denied ({code_str})") from exc
            raise EvernoteError(f"Evernote user error: {code_str}") from exc

        if isinstance(exc, EDAMNotFoundException):
            target = guid or getattr(exc, "key", "")
            raise NoteNotFoundError(f"Evernote note not found: {target}") from exc

        if isinstance(exc, EDAMSystemException):
            code_str = _error_code_name(exc.errorCode)
            raise EvernoteError(f"Evernote system error: {code_str}") from exc

        if isinstance(
            exc, (EvernoteError, EvernoteAuthError, EvernotePermissionError, NoteNotFoundError)
        ):
            raise exc

        raise EvernoteError(f"Evernote API call failed: {exc.__class__.__name__}") from exc

    def _call_with_retry(self, fn: Any, *args: Any, guid: str | None = None, **kwargs: Any) -> Any:
        attempt = 0
        total_rate_limit_slept = 0.0
        while True:
            try:
                return fn(*args, **kwargs)
            except EDAMSystemException as exc:
                if exc.errorCode == EDAMErrorCode.RATE_LIMIT_REACHED:
                    duration = float(getattr(exc, "rateLimitDuration", 1) or 1)
                    if duration > self.max_rate_limit_wait:
                        raise EvernoteRateLimitError(
                            f"Evernote rate limit duration ({duration:.0f}s) exceeds "
                            f"maximum allowed wait ({self.max_rate_limit_wait:.0f}s)",
                            retry_after=duration,
                        ) from exc
                    if total_rate_limit_slept + duration > self.max_rate_limit_wait:
                        total_s = total_rate_limit_slept + duration
                        raise EvernoteRateLimitError(
                            f"Evernote total rate limit wait ({total_s:.0f}s) exceeds "
                            f"maximum allowed wait ({self.max_rate_limit_wait:.0f}s)",
                            retry_after=duration,
                        ) from exc
                    attempt += 1
                    if attempt > self.max_retries:
                        raise EvernoteRateLimitError(
                            "Evernote rate limit reached and retry attempts exhausted",
                            retry_after=duration,
                        ) from exc
                    self.sleep_fn(duration)
                    total_rate_limit_slept += duration
                    continue
                self._handle_exception(exc, guid=guid)
            except (EDAMUserException, EDAMNotFoundException) as exc:
                self._handle_exception(exc, guid=guid)
            except (TTransportException, ConnectionError, TimeoutError, OSError, EOFError) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    err_type = exc.__class__.__name__
                    raise EvernoteError(
                        f"Evernote transport failed after {self.max_retries} retries: {err_type}"
                    ) from exc
                backoff = float(2 ** (attempt - 1))
                self.sleep_fn(backoff)
                continue
            except (TException, EOFError) as exc:  # noqa: B025
                raise EvernoteError(f"Evernote API call failed: {exc.__class__.__name__}") from exc

    def get_sync_state(self) -> Any:
        """Fetch current sync state from NoteStore."""
        note_store = self.get_note_store()
        return self._call_with_retry(note_store.getSyncState, self._token)

    def get_filtered_sync_chunk(self, after_usn: int, max_entries: int, filter: Any) -> Any:
        """Fetch an incremental sync chunk from NoteStore."""
        note_store = self.get_note_store()
        return self._call_with_retry(
            note_store.getFilteredSyncChunk, self._token, after_usn, max_entries, filter
        )

    def get_note_content(self, guid: str) -> str:
        """Fetch the ENML note content by GUID from NoteStore."""
        note_store = self.get_note_store()
        return str(self._call_with_retry(note_store.getNoteContent, self._token, guid, guid=guid))


class ENMLToTextParser(HTMLParser):
    """HTMLParser for converting Evernote ENML content to plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pieces: list[str] = []
        self._ignore_depth = 0
        self._crypt_depth = 0

    def _ensure_newline(self) -> None:
        if self.pieces and not self.pieces[-1].endswith("\n"):
            self.pieces.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)

        if tag in ("script", "style"):
            self._ignore_depth += 1
            return

        if tag == "en-crypt":
            self.pieces.append("[encrypted content]")
            self._crypt_depth += 1
            return

        if self._ignore_depth > 0 or self._crypt_depth > 0:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "div", "p", "tr"):
            self._ensure_newline()
            if tag == "h1":
                self.pieces.append("# ")
            elif tag == "h2":
                self.pieces.append("## ")
            elif tag == "h3":
                self.pieces.append("### ")
        elif tag == "li":
            self._ensure_newline()
            self.pieces.append("- ")
        elif tag == "br":
            self.pieces.append("\n")
        elif tag == "en-todo":
            checked = str(attrs_dict.get("checked", "")).lower() == "true"
            self.pieces.append("[x] " if checked else "[ ] ")
        elif tag == "en-media":
            media_type = attrs_dict.get("type", "")
            self.pieces.append(f"[attachment: {media_type}]")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            if self._ignore_depth > 0:
                self._ignore_depth -= 1
            return

        if tag == "en-crypt":
            if self._crypt_depth > 0:
                self._crypt_depth -= 1
            return

        if self._ignore_depth > 0 or self._crypt_depth > 0:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "div", "p", "tr", "li"):
            self._ensure_newline()

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0 or self._crypt_depth > 0:
            return
        self.pieces.append(data)


def enml_to_text(enml: str) -> str:
    """Convert Evernote ENML content to plain text.

    Tolerates XML declarations, DOCTYPEs, and HTML entities without raising.
    Converts headers, lists, todos, attachments, and collapses empty lines.
    """
    if not enml or not isinstance(enml, str):
        return ""

    parser = ENMLToTextParser()
    parser.feed(enml)
    parser.close()

    raw = "".join(parser.pieces)
    lines = [line.rstrip() for line in raw.splitlines()]

    collapsed: list[str] = []
    prev_blank = True
    for line in lines:
        if not line.strip():
            if not prev_blank:
                collapsed.append("")
                prev_blank = True
        else:
            collapsed.append(line)
            prev_blank = False

    while collapsed and not collapsed[-1]:
        collapsed.pop()

    return "\n".join(collapsed)


def note_to_row(
    note: Any,
    content_text: str,
    notebook_names: dict[str, str] | None = None,
    tag_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert an Evernote Note object and extracted content into a tabular row dictionary.

    Args:
        note: Evernote Note object or mock with note attributes.
        content_text: Extracted plain text content for the note.
        notebook_names: Optional mapping from notebook GUID to human notebook name.
        tag_names: Optional mapping from tag GUID to human tag name.

    Returns:
        Dict formatted for dlt ingestion with keys: id, title, content, _deleted.
    """
    notebook_guid = getattr(note, "notebookGuid", None)
    notebook_map = notebook_names or {}
    notebook_name = notebook_map.get(notebook_guid) if notebook_guid else None

    tag_map = tag_names or {}
    tag_guids = getattr(note, "tagGuids", None) or []
    resolved_tags = [tag_map.get(g) for g in tag_guids]
    valid_tags = [t for t in resolved_tags if t]
    sorted_tags = sorted(valid_tags, key=lambda s: s.casefold())

    header_lines: list[str] = []
    if notebook_name:
        header_lines.append(f"Notebook: {notebook_name}")
    if sorted_tags:
        header_lines.append(f"Tags: {', '.join(sorted_tags)}")

    full_content = "\n".join(header_lines) + "\n\n" + content_text if header_lines else content_text

    return {
        "id": getattr(note, "guid", None),
        "title": getattr(note, "title", "") or "",
        "content": full_content,
        "_deleted": False,
    }


def iter_sync_chunks(
    api: Any,
    after_usn: int = 0,
    max_entries: int = 250,
    check_active: Callable[[], bool] | None = None,
) -> Generator[Any, None, None]:
    """Yield incremental SyncChunk objects from Evernote until caught up.

    Builds a SyncChunkFilter including notes, notebooks, tags, and expunged records.
    Stops when chunkHighUSN >= updateCount or when chunkHighUSN is None (empty chunk).
    Raises EvernoteError if a chunk fails to advance after_usn.
    """
    current_after_usn = after_usn
    chunk_filter = SyncChunkFilter(
        includeNotes=True,
        includeNotebooks=True,
        includeTags=True,
        includeExpunged=True,
    )

    while True:
        if check_active is not None and not check_active():
            raise _SyncCancelledError()
        chunk = api.get_filtered_sync_chunk(current_after_usn, max_entries, chunk_filter)
        high_usn = getattr(chunk, "chunkHighUSN", None)
        if high_usn is None:
            break

        if high_usn <= current_after_usn:
            raise EvernoteError(
                f"SyncChunk did not advance USN ({high_usn} <= after_usn={current_after_usn})"
            )

        yield chunk

        update_count = getattr(chunk, "updateCount", None)
        if update_count is not None and high_usn >= update_count:
            break

        current_after_usn = high_usn


def _sync_records(
    service: Any,
    state: dict[str, Any],
    notebook_names: list[str] | None = None,
    tag_names: list[str] | None = None,
    sandbox: bool = False,
    full_resync: bool = False,
    max_notes_per_run: int | None = 100,
    check_active: Callable[[], bool] | None = None,
    stats: dict[str, int] | None = None,
) -> Generator[Any, None, None]:
    """Execute the two-pass sync algorithm using the provided state dict.

    Updates state only after all rows have been yielded without exception.
    `max_notes_per_run` limits the number of note bodies fetched in a single run
    (default 100, a conservative batch limit). Pass None for unlimited.
    """
    log = get_logger("evernote_connector")

    if stats is not None:
        stats.clear()
        stats.update(notes_synced=0, tombstones=0, failed_rate_limit=0, failed_auth=0)

    # Check active from the start: zero API calls, clean end
    if check_active is not None and not check_active():
        log.warning("Evernote sync stopped before start by check_active callback.")
        yield dlt.mark.materialize_table_schema()
        return

    # An empty configured list [] behaves identically to None (no filter)
    if notebook_names is not None and not notebook_names:
        notebook_names = None
    if tag_names is not None and not tag_names:
        tag_names = None

    # Step 0: Account binding
    try:
        user_id = service.get_user_id()
    except (EvernoteAuthError, EvernotePermissionError):
        if stats is not None:
            stats["failed_auth"] = stats.get("failed_auth", 0) + 1
        raise
    bind = f"{'sandbox' if sandbox else 'production'}:{user_id}"

    if "account" in state and state["account"] != bind:
        raise EvernoteError(
            "this resource name is bound to a different Evernote account or environment; "
            "use a different resource_name"
        )

    # Detect old-shape or foreign state: if state is non-empty and missing/differing
    # version, or note_ids is a list, treat it as a first run (full resync) and overwrite it.
    is_old_or_foreign = bool(state) and (
        state.get("v") != STATE_VERSION or isinstance(state.get("note_ids"), list)
    )
    if is_old_or_foreign:
        state.pop("partial", None)

    # Step 1: Call api.get_sync_state() first and read after_usn
    if check_active is not None and not check_active():
        log.warning("Evernote sync stopped before get_sync_state by check_active callback.")
        yield dlt.mark.materialize_table_schema()
        return

    try:
        sync_state = service.get_sync_state() if hasattr(service, "get_sync_state") else None
    except EvernoteRateLimitError as e:
        if stats is not None:
            stats["failed_rate_limit"] = stats.get("failed_rate_limit", 0) + 1
        log.warning(
            "Evernote rate limit reached getting sync state: %s (retry_after=%s)",
            e,
            getattr(e, "retry_after", None),
        )
        yield dlt.mark.materialize_table_schema()
        return
    except (EvernoteAuthError, EvernotePermissionError):
        if stats is not None:
            stats["failed_auth"] = stats.get("failed_auth", 0) + 1
        raise

    current_sync_time = getattr(sync_state, "currentTime", None)
    server_update_count = getattr(sync_state, "updateCount", None)
    full_sync_before = getattr(sync_state, "fullSyncBefore", None)

    has_last_usn = False if is_old_or_foreign else ("last_usn" in state)
    last_usn = 0 if is_old_or_foreign else state.get("last_usn", 0)
    last_sync_time = None if is_old_or_foreign else state.get("last_sync_time")

    if is_old_or_foreign:
        prior_note_ids: set[str] = set()
        updated_note_ids: dict[str, str] = {}
    else:
        raw_note_ids = state.get("note_ids")
        if isinstance(raw_note_ids, dict):
            prior_note_ids = set(raw_note_ids.keys())
            updated_note_ids = dict(raw_note_ids)
        elif isinstance(raw_note_ids, list):
            prior_note_ids = set(raw_note_ids)
            updated_note_ids = {}
        else:
            prior_note_ids = set()
            updated_note_ids = {}

    # Compute configured scope names
    current_nb_names = sorted(set(notebook_names)) if notebook_names is not None else None
    current_tag_names = sorted(set(tag_names)) if tag_names is not None else None

    current_scope_signature = {
        "notebooks": current_nb_names,
        "tags": current_tag_names,
    }

    # Partial sync recovery
    partial = state.get("partial")
    if not isinstance(partial, dict):
        partial = None

    partial_scope_matches = False
    resume_usn = 0
    if partial is not None:
        partial_scope = partial.get("scope")
        if partial_scope == current_scope_signature:
            partial_scope_matches = True
            resume_usn = int(partial.get("resume_usn", 0) or 0)
        else:
            # Scope signature differs: discard partial and start fresh
            state.pop("partial", None)
            partial = None

    is_resuming_full = partial_scope_matches and bool(partial.get("full", False))

    # Step 2: Determine if this run is a FULL resync upfront.
    # Scope name changes are evaluated first so they cannot be short-circuited by the
    # "server updateCount == last_usn" shortcut that follows.
    persisted_scope = state.get("scope") or {}
    persisted_nb_names = persisted_scope.get("notebooks")
    persisted_tag_names = persisted_scope.get("tags")
    persisted_nb_guids = persisted_scope.get("notebook_guids")
    persisted_tag_guids = persisted_scope.get("tag_guids")

    name_scope_changed = has_last_usn and (
        persisted_nb_names != current_nb_names or persisted_tag_names != current_tag_names
    )
    if name_scope_changed:
        log.info("Evernote scope names changed, triggering full resync.")

    scope_missing_guids = has_last_usn and (
        (
            notebook_names is not None
            and (not isinstance(persisted_nb_guids, list) or not persisted_nb_guids)
        )
        or (
            tag_names is not None
            and (not isinstance(persisted_tag_guids, list) or not persisted_tag_guids)
        )
    )
    if scope_missing_guids:
        log.info("Evernote configured scope lacks persisted GUID list, triggering full resync.")

    is_full_resync = (
        is_resuming_full
        or full_resync
        or is_old_or_foreign
        or name_scope_changed
        or scope_missing_guids
        or (
            has_last_usn
            and full_sync_before is not None
            and last_sync_time is not None
            and full_sync_before > last_sync_time
        )
        or (has_last_usn and server_update_count is not None and server_update_count < last_usn)
    )

    if is_full_resync and not is_resuming_full:
        # A new full resync with no matching partial starts fresh with resume_usn=0
        resume_usn = 0

    # Step 3: Server updateCount == last_usn means nothing changed on server (incremental only)
    has_partial = bool(state.get("partial"))
    if (
        not is_full_resync
        and not has_partial
        and has_last_usn
        and server_update_count is not None
        and server_update_count == last_usn
    ):
        if state.get("v") != STATE_VERSION:
            state["v"] = STATE_VERSION
        if state.get("account") != bind:
            state["account"] = bind
        if "last_sync_time" not in state and current_sync_time is not None:
            state["last_sync_time"] = int(current_sync_time)
        yield dlt.mark.materialize_table_schema()
        return

    # Step 4: Pass 1 chunk iteration and GUID resolution loop
    while True:
        if is_full_resync or not has_last_usn:
            after_usn = 0
            notebook_map: dict[str, str] = {}
            tag_map: dict[str, str] = {}
        else:
            after_usn = last_usn
            notebook_map = dict(state.get("notebooks") or {})
            tag_map = dict(state.get("tags") or {})

        note_meta: dict[str, Any] = {}
        expunged_guids: set[str] = set()
        expunged_nb_guids: set[str] = set()
        highest_chunk_usn: int | None = None

        try:
            for chunk in iter_sync_chunks(
                service, after_usn=after_usn, max_entries=250, check_active=check_active
            ):
                chunk_usn = getattr(chunk, "chunkHighUSN", None)
                if chunk_usn is not None and (
                    highest_chunk_usn is None or chunk_usn > highest_chunk_usn
                ):
                    highest_chunk_usn = chunk_usn
                for nb in getattr(chunk, "notebooks", None) or []:
                    notebook_map[nb.guid] = nb.name
                for tag in getattr(chunk, "tags", None) or []:
                    tag_map[tag.guid] = tag.name
                for note in getattr(chunk, "notes", None) or []:
                    note_meta[note.guid] = note
                for guid in getattr(chunk, "expungedNotes", None) or []:
                    expunged_guids.add(guid)
                    note_meta.pop(guid, None)
                for nb_guid in getattr(chunk, "expungedNotebooks", None) or []:
                    expunged_nb_guids.add(nb_guid)
                    notebook_map.pop(nb_guid, None)
                for tag_guid in getattr(chunk, "expungedTags", None) or []:
                    tag_map.pop(tag_guid, None)
        except EvernoteRateLimitError as e:
            if stats is not None:
                stats["failed_rate_limit"] = stats.get("failed_rate_limit", 0) + 1
            log.warning(
                "Evernote rate limit reached during pass 1: %s (retry_after=%s)",
                e,
                getattr(e, "retry_after", None),
            )
            yield dlt.mark.materialize_table_schema()
            return
        except _SyncCancelledError:
            log.warning("Evernote sync stopped during pass 1 by check_active callback.")
            yield dlt.mark.materialize_table_schema()
            return
        except (EvernoteAuthError, EvernotePermissionError):
            if stats is not None:
                stats["failed_auth"] = stats.get("failed_auth", 0) + 1
            raise

        # Validate requested notebook_names and tag_names against MERGED maps
        if notebook_names is not None:
            requested_nbs = set(notebook_names)
            found_nbs = set(notebook_map.values())
            unknown_nbs = requested_nbs - found_nbs
            if unknown_nbs:
                raise EvernoteError(
                    f"Notebook name(s) not found: {sorted(unknown_nbs)}. "
                    "If a notebook was renamed, update notebook_names."
                )

        if tag_names is not None:
            requested_tags = set(tag_names)
            found_tags = set(tag_map.values())
            unknown_tags = requested_tags - found_tags
            if unknown_tags:
                raise EvernoteError(
                    f"Tag name(s) not found: {sorted(unknown_tags)}. "
                    "If a tag was renamed, update tag_names."
                )

        # Resolve configured notebook and tag names to GUIDs
        allowed_nb_guids = (
            {g for g, n in notebook_map.items() if n in set(notebook_names)}
            if notebook_names is not None
            else None
        )
        allowed_tag_guids = (
            {g for g, n in tag_map.items() if n in set(tag_names)}
            if tag_names is not None
            else None
        )

        resolved_nb_guids = sorted(allowed_nb_guids) if allowed_nb_guids is not None else None
        resolved_tag_guids = sorted(allowed_tag_guids) if allowed_tag_guids is not None else None

        # Check if resolved GUIDs changed from state
        persisted_scope = state.get("scope") or {}
        persisted_nb_guids = persisted_scope.get("notebook_guids")
        persisted_tag_guids = persisted_scope.get("tag_guids")

        guid_scope_changed = False
        if has_last_usn:
            if notebook_names is not None and (
                persisted_nb_guids is None or resolved_nb_guids != persisted_nb_guids
            ):
                guid_scope_changed = True
            if tag_names is not None and (
                persisted_tag_guids is None or resolved_tag_guids != persisted_tag_guids
            ):
                guid_scope_changed = True

        if guid_scope_changed and not is_full_resync:
            log.info("Evernote scope GUIDs changed, restarting full resync from USN 0.")
            is_full_resync = True
            resume_usn = 0
            continue

        break

    # Handle notes of expunged notebooks:
    # Expunged notebook GUIDs were already removed from notebook_map during chunk
    # processing (alongside expunged tags). Here we collect notes previously tracked
    # under those notebooks, yield tombstones (deduplicated with expungedNotes), and
    # remove them from note_ids. If a note appears in this run's metadata with a
    # DIFFERENT notebook guid (moved out first), the newer guid wins and the note is
    # NOT tombstoned.
    for nb_guid in expunged_nb_guids:
        for note_id, current_nb_guid in list(updated_note_ids.items()):
            if current_nb_guid == nb_guid:
                if (
                    note_id in note_meta
                    and getattr(note_meta[note_id], "notebookGuid", None) != nb_guid
                ):
                    continue
                expunged_guids.add(note_id)
                note_meta.pop(note_id, None)

    def _is_live_and_in_scope(note: Any) -> bool:
        if getattr(note, "active", True) is False or getattr(note, "deleted", None) is not None:
            return False
        if (
            allowed_nb_guids is not None
            and getattr(note, "notebookGuid", None) not in allowed_nb_guids
        ):
            return False
        if allowed_tag_guids is not None:
            note_tag_guids = set(getattr(note, "tagGuids", None) or [])
            if not bool(note_tag_guids & allowed_tag_guids):
                return False
        return True

    # Pass 2: Emit tombstones and active in-scope notes
    yielded_count = 0
    tombstoned_count = 0

    if is_full_resync:
        # Deletion reconciliation: tombstone ids that were ingested before but
        # are no longer present on server or live in-scope in this full snapshot.
        live_in_scope_ids = {
            guid for guid, note in note_meta.items() if _is_live_and_in_scope(note)
        }
        stale = prior_note_ids - live_in_scope_ids - set(note_meta.keys()) - expunged_guids
        for stale_id in sorted(stale):
            yield {"id": stale_id, "title": "", "content": "", "_deleted": True}
            tombstoned_count += 1
            if stats is not None:
                stats["tombstones"] = stats.get("tombstones", 0) + 1
            updated_note_ids.pop(stale_id, None)

    # 1. Expunged notes always yield tombstones
    for guid in sorted(expunged_guids):
        yield {"id": guid, "title": "", "content": "", "_deleted": True}
        tombstoned_count += 1
        if stats is not None:
            stats["tombstones"] = stats.get("tombstones", 0) + 1
        updated_note_ids.pop(guid, None)

    # 2. Changed note metadata: inactive / out-of-scope notes yield tombstones
    for note in note_meta.values():
        if not _is_live_and_in_scope(note):
            guid = getattr(note, "guid", "")
            yield {"id": guid, "title": "", "content": "", "_deleted": True}
            tombstoned_count += 1
            if stats is not None:
                stats["tombstones"] = stats.get("tombstones", 0) + 1
            updated_note_ids.pop(guid, None)

    # 3. Live in-scope notes sorted by updateSequenceNum ascending
    candidate_notes = [note for note in note_meta.values() if _is_live_and_in_scope(note)]
    candidate_notes.sort(key=lambda n: getattr(n, "updateSequenceNum", 0) or 0)

    watermark_usn = resume_usn
    body_fetches = 0
    is_unfinished = False

    for idx, note in enumerate(candidate_notes):
        note_usn = getattr(note, "updateSequenceNum", 0) or 0
        if resume_usn > 0 and note_usn > 0 and note_usn <= resume_usn:
            watermark_usn = max(watermark_usn, note_usn)
            continue

        if check_active is not None and not check_active():
            notes_remaining = len(candidate_notes) - idx
            log.warning(
                "Evernote sync stopped by check_active; %d note(s) remaining for next run.",
                notes_remaining,
            )
            is_unfinished = True
            break

        if max_notes_per_run is not None and body_fetches >= max_notes_per_run:
            notes_remaining = len(candidate_notes) - idx
            log.warning(
                "Reached max_notes_per_run limit (%d); %d note(s) remaining for next run.",
                max_notes_per_run,
                notes_remaining,
            )
            is_unfinished = True
            break

        guid = getattr(note, "guid", "")
        try:
            enml = service.get_note_content(guid)
            body_fetches += 1
        except EvernoteRateLimitError as e:
            if stats is not None:
                stats["failed_rate_limit"] = stats.get("failed_rate_limit", 0) + 1
            notes_remaining = len(candidate_notes) - idx
            retry_after = getattr(e, "retry_after", None)
            log.warning(
                "Evernote rate limit reached while fetching note %s: %s "
                "(%d note(s) remaining, retry_after=%s)",
                guid,
                e,
                notes_remaining,
                retry_after,
            )
            is_unfinished = True
            break
        except (EvernoteAuthError, EvernotePermissionError):
            if stats is not None:
                stats["failed_auth"] = stats.get("failed_auth", 0) + 1
            raise
        except NoteNotFoundError:
            body_fetches += 1
            yield {"id": guid, "title": "", "content": "", "_deleted": True}
            tombstoned_count += 1
            if stats is not None:
                stats["tombstones"] = stats.get("tombstones", 0) + 1
            updated_note_ids.pop(guid, None)
            watermark_usn = max(watermark_usn, note_usn)
            continue

        content_text = enml_to_text(enml)
        if not content_text:
            content_text = getattr(note, "title", "") or ""
        if not content_text:
            yield {"id": guid, "title": "", "content": "", "_deleted": True}
            tombstoned_count += 1
            if stats is not None:
                stats["tombstones"] = stats.get("tombstones", 0) + 1
            updated_note_ids.pop(guid, None)
            watermark_usn = max(watermark_usn, note_usn)
            continue

        row = note_to_row(
            note,
            content_text,
            notebook_names=notebook_map,
            tag_names=tag_map,
        )
        yield row
        yielded_count += 1
        if stats is not None:
            stats["notes_synced"] = stats.get("notes_synced", 0) + 1
        updated_note_ids[guid] = getattr(note, "notebookGuid", "")
        watermark_usn = max(watermark_usn, note_usn)

    log.info(
        "Evernote sync complete: %d row(s) yielded, %d tombstone(s) emitted.",
        yielded_count,
        tombstoned_count,
    )

    if yielded_count == 0 and tombstoned_count == 0:
        yield dlt.mark.materialize_table_schema()

    # Step 6: State persistence
    if is_old_or_foreign:
        state.clear()

    new_v = STATE_VERSION
    new_account = bind
    fallback_usn = state.get("last_usn", after_usn)
    new_last_usn = highest_chunk_usn if highest_chunk_usn is not None else fallback_usn
    new_notebooks = notebook_map
    new_tags = tag_map
    new_note_ids = dict(sorted(updated_note_ids.items()))

    is_full = is_full_resync or not has_last_usn

    if is_unfinished:
        # While a pass is unfinished:
        # - do NOT advance last_usn or last_sync_time
        # - do NOT write the new state["scope"]
        # - DO persist note_ids for notes loaded so far, notebook/tag maps, and state["partial"]
        new_partial = {
            "resume_usn": watermark_usn,
            "full": is_full,
            "scope": current_scope_signature,
        }
        if state.get("v") != new_v:
            state["v"] = new_v
        if state.get("account") != new_account:
            state["account"] = new_account
        if state.get("notebooks") != new_notebooks:
            state["notebooks"] = new_notebooks
        if state.get("tags") != new_tags:
            state["tags"] = new_tags
        if state.get("note_ids") != new_note_ids:
            state["note_ids"] = new_note_ids
        if state.get("partial") != new_partial:
            state["partial"] = new_partial
    else:
        # Pass 2 completed: delete state["partial"], write last_usn / last_sync_time / scope
        state.pop("partial", None)

        new_scope = {
            "notebooks": current_nb_names,
            "tags": current_tag_names,
            "notebook_guids": resolved_nb_guids,
            "tag_guids": resolved_tag_guids,
        }

        something_changed = (
            is_full_resync
            or "last_sync_time" not in state
            or state.get("last_usn") != new_last_usn
            or yielded_count > 0
            or tombstoned_count > 0
            or state.get("notebooks") != new_notebooks
            or state.get("tags") != new_tags
            or state.get("scope") != new_scope
            or state.get("note_ids") != new_note_ids
        )

        if state.get("v") != new_v:
            state["v"] = new_v
        if state.get("account") != new_account:
            state["account"] = new_account
        if state.get("last_usn") != new_last_usn:
            state["last_usn"] = new_last_usn
        if something_changed and current_sync_time is not None:
            new_sync_time = int(current_sync_time)
            if state.get("last_sync_time") != new_sync_time:
                state["last_sync_time"] = new_sync_time
        if state.get("notebooks") != new_notebooks:
            state["notebooks"] = new_notebooks
        if state.get("tags") != new_tags:
            state["tags"] = new_tags
        if state.get("note_ids") != new_note_ids:
            state["note_ids"] = new_note_ids
        if state.get("scope") != new_scope:
            state["scope"] = new_scope


def evernote_source(
    token: str | None = None,
    sandbox: bool = False,
    notebook_names: list[str] | None = None,
    tag_names: list[str] | None = None,
    resource_name: str = "evernote_notes",
    service: Any = None,
    full_resync: bool = False,
    max_notes_per_run: int | None = 100,
    check_active: Callable[[], bool] | None = None,
):
    """Create an Evernote dlt resource for cognee memory ingestion.

    Extracts active notes filtered by optional notebook and tag scopes,
    emitting merge records and tombstones for trashed or expunged notes.

    `max_notes_per_run` limits the number of note bodies fetched in a single run
    (default 100, a conservative batch limit not derived from Evernote's published
    limits; pass None for unlimited). If rate-limited or the limit is reached, the
    run finishes cleanly and persists a progress watermark so the next run continues.
    """
    _check_cognee_version()

    if not token or not isinstance(token, str) or not token.strip():
        raise EvernoteAuthError(
            "Evernote authentication token required: pass a non-empty token string."
        )

    if notebook_names is not None and not notebook_names:
        notebook_names = None
    if tag_names is not None and not tag_names:
        tag_names = None

    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("resource_name must be a nonempty string")

    name = NamingConvention().normalize_table_identifier(resource_name)
    api_service = service if service is not None else EvernoteApi(token=token, sandbox=sandbox)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={
            **{
                field: {"data_type": "text", "nullable": False}
                for field in ("id", "title", "content")
            },
            "_deleted": {"data_type": "bool", "hard_delete": True},
        },
    )
    def records():
        state = dlt.current.resource_state()
        rows = _sync_records(
            service=api_service,
            state=state,
            notebook_names=notebook_names,
            tag_names=tag_names,
            sandbox=sandbox,
            full_resync=full_resync,
            max_notes_per_run=max_notes_per_run,
            check_active=check_active,
            stats=stats,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = records()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "evernote")
    setattr(resource, PIPELINE_SCOPE_ATTR, name)
    resource.service = api_service
    resource.cognee_sync_stats = stats
    return resource
