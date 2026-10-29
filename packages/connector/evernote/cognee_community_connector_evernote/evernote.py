"""Evernote data-source connector for cognee.

Extracts notes, notebooks, and tags from Evernote into cognee memory with
incremental synchronization via Evernote sync chunks and forget-on-delete.
"""

import os
import time
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
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
from thrift.transport.THttpClient import THttpClient
from thrift.transport.TTransport import TTransportException

logger = get_logger(__name__)


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
        max_rate_limit_wait: float = 300,
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
            if exc.errorCode in (EDAMErrorCode.INVALID_AUTH, EDAMErrorCode.AUTH_EXPIRED):
                code_name = getattr(exc.errorCode, "name", str(exc.errorCode))
                raise EvernoteAuthError(f"Evernote authentication failed ({code_name})") from None
            if exc.errorCode == EDAMErrorCode.PERMISSION_DENIED:
                code_name = getattr(exc.errorCode, "name", str(exc.errorCode))
                raise EvernotePermissionError(f"Evernote permission denied ({code_name})") from None
            raise EvernoteError(f"Evernote user error: {exc.errorCode}") from None

        if isinstance(exc, EDAMNotFoundException):
            target = guid or getattr(exc, "key", "")
            raise NoteNotFoundError(f"Evernote note not found: {target}") from None

        if isinstance(
            exc, (EvernoteError, EvernoteAuthError, EvernotePermissionError, NoteNotFoundError)
        ):
            raise exc

        raise EvernoteError(f"Evernote API call failed: {exc.__class__.__name__}") from None

    def _call_with_retry(self, fn: Any, *args: Any, guid: str | None = None, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except EDAMSystemException as exc:
                if exc.errorCode == EDAMErrorCode.RATE_LIMIT_REACHED:
                    duration = float(getattr(exc, "rateLimitDuration", 1) or 1)
                    if duration > self.max_rate_limit_wait:
                        raise EvernoteRateLimitError(
                            f"Evernote rate limit duration ({duration:.0f}s) exceeds "
                            f"maximum allowed wait ({self.max_rate_limit_wait:.0f}s)"
                        ) from None
                    attempt += 1
                    if attempt > self.max_retries:
                        raise EvernoteRateLimitError(
                            "Evernote rate limit reached and retry attempts exhausted"
                        ) from None
                    self.sleep_fn(duration)
                    continue
                self._handle_exception(exc, guid=guid)
            except (TTransportException, ConnectionError, TimeoutError, OSError) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    err_type = exc.__class__.__name__
                    raise EvernoteError(
                        f"Evernote transport failed after {self.max_retries} retries: {err_type}"
                    ) from None
                backoff = float(2 ** (attempt - 1))
                self.sleep_fn(backoff)
                continue
            except Exception as exc:
                self._handle_exception(exc, guid=guid)

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
    try:
        parser.feed(enml)
        parser.close()
    except Exception:
        pass

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


def _timestamp_to_iso(ts_ms: int | float | None) -> str | None:
    if ts_ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
        return dt.isoformat()
    except Exception:
        return None


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
        Dict formatted for dlt ingestion with keys: id, title, content, notebook_guid,
        notebook, tags, created, updated, _deleted.
    """
    notebook_guid = getattr(note, "notebookGuid", None)
    notebook_map = notebook_names or {}
    notebook_name = notebook_map.get(notebook_guid, notebook_guid or "")

    tag_map = tag_names or {}
    tag_guids = getattr(note, "tagGuids", None) or []
    resolved_tags = [tag_map.get(g, g or "") for g in tag_guids]
    tags_str = ", ".join(t for t in resolved_tags if t)

    created_iso = _timestamp_to_iso(getattr(note, "created", None))
    updated_iso = _timestamp_to_iso(getattr(note, "updated", None))

    return {
        "id": getattr(note, "guid", None),
        "title": getattr(note, "title", "") or "",
        "content": content_text,
        "notebook_guid": notebook_guid,
        "notebook": notebook_name,
        "tags": tags_str,
        "created": created_iso,
        "updated": updated_iso,
        "_deleted": False,
    }


def iter_sync_chunks(
    api: Any, after_usn: int = 0, max_entries: int = 250
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
    api_client: Any,
    state: dict[str, Any],
    notebook_names: list[str] | None = None,
    tag_names: list[str] | None = None,
    sandbox: bool = False,
    full_resync: bool = False,
) -> Generator[Any, None, None]:
    """Execute the two-pass sync algorithm using the provided state dict.

    Updates state only after all rows have been yielded without exception.
    """
    log = get_logger("evernote_connector")

    # Step 0: Account binding
    user_id = api_client.get_user_id()
    bind = f"{'sandbox' if sandbox else 'production'}:{user_id}"

    if "account" in state and state["account"] != bind:
        raise EvernoteError(
            "this resource name is bound to a different Evernote account or environment; "
            "use a different resource_name"
        )

    # Step 1: Call api.get_sync_state() first and read after_usn
    sync_state = api_client.get_sync_state() if hasattr(api_client, "get_sync_state") else None
    current_sync_time = getattr(sync_state, "currentTime", None)
    server_update_count = getattr(sync_state, "updateCount", None)
    full_sync_before = getattr(sync_state, "fullSyncBefore", None)

    has_last_usn = "last_usn" in state
    last_usn = state.get("last_usn", 0)
    last_sync_time = state.get("last_sync_time")

    # Step 2: Determine if this run is a FULL resync
    is_full_resync = (
        full_resync
        or (
            has_last_usn
            and full_sync_before is not None
            and last_sync_time is not None
            and full_sync_before > last_sync_time
        )
        or (has_last_usn and server_update_count is not None and server_update_count < last_usn)
    )

    # Step 3: Server updateCount == last_usn means nothing changed on server (incremental only)
    if (
        not is_full_resync
        and has_last_usn
        and server_update_count is not None
        and server_update_count == last_usn
    ):
        if current_sync_time is not None:
            state["last_sync_time"] = int(current_sync_time)
        state["account"] = bind
        yield dlt.mark.materialize_table_schema()
        return

    # Step 4: Iterate sync chunks from after_usn, starting notebook/tag maps from state
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
    highest_chunk_usn: int | None = None
    updated_note_ids: set[str] = set(state.get("note_ids") or [])

    yielded_count = 0
    tombstoned_count = 0

    for chunk in iter_sync_chunks(api_client, after_usn=after_usn, max_entries=250):
        chunk_usn = getattr(chunk, "chunkHighUSN", None)
        if chunk_usn is not None and (highest_chunk_usn is None or chunk_usn > highest_chunk_usn):
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

        # UNKNOWN: How Evernote reports notes inside an expunged notebook (whether their GUIDs
        # are also included in chunk.expungedNotes, whether they are orphaned, or whether
        # the notebook deletion cascades on the server). No behavior is invented here.
        for nb_guid in getattr(chunk, "expungedNotebooks", None) or []:
            notebook_map.pop(nb_guid, None)
        for tag_guid in getattr(chunk, "expungedTags", None) or []:
            tag_map.pop(tag_guid, None)

    # Validate requested notebook_names and tag_names against MERGED maps
    if notebook_names is not None:
        requested_nbs = set(notebook_names)
        found_nbs = set(notebook_map.values())
        unknown_nbs = requested_nbs - found_nbs
        if unknown_nbs:
            raise EvernoteError(f"Unknown notebook name(s): {', '.join(sorted(unknown_nbs))}")

    if tag_names is not None:
        requested_tags = set(tag_names)
        found_tags = set(tag_map.values())
        unknown_tags = requested_tags - found_tags
        if unknown_tags:
            raise EvernoteError(f"Unknown tag name(s): {', '.join(sorted(unknown_tags))}")

    # Compute allowed notebook and tag GUID sets
    allowed_nb_guids = (
        {g for g, n in notebook_map.items() if n in set(notebook_names)}
        if notebook_names is not None
        else None
    )
    allowed_tag_guids = (
        {g for g, n in tag_map.items() if n in set(tag_names)} if tag_names is not None else None
    )

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
    # state["note_ids"] is used ONLY for:
    #   (a) the full-resync stale-id reconciliation sweep below, and
    #   (b) final bookkeeping (add yielded ids, discard tombstoned ids).
    # Every tombstone case (trashed, expunged, out-of-scope, NoteNotFound,
    # empty content+title) is emitted unconditionally — regardless of whether
    # the id was previously ingested.
    prior_note_ids: set[str] = set(state.get("note_ids") or [])

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
            updated_note_ids.discard(stale_id)

    # 1. Expunged notes always yield tombstones
    for guid in sorted(expunged_guids):
        yield {"id": guid, "title": "", "content": "", "_deleted": True}
        tombstoned_count += 1
        updated_note_ids.discard(guid)

    # 2. Changed / snapshot note metadata
    for note in note_meta.values():
        guid = getattr(note, "guid", "")
        if not _is_live_and_in_scope(note):
            yield {"id": guid, "title": "", "content": "", "_deleted": True}
            tombstoned_count += 1
            updated_note_ids.discard(guid)
            continue

        try:
            enml = api_client.get_note_content(guid)
        except NoteNotFoundError:
            yield {"id": guid, "title": "", "content": "", "_deleted": True}
            tombstoned_count += 1
            updated_note_ids.discard(guid)
            continue

        content_text = enml_to_text(enml)
        if not content_text:
            content_text = getattr(note, "title", "") or ""
        if not content_text:
            yield {"id": guid, "title": "", "content": "", "_deleted": True}
            tombstoned_count += 1
            updated_note_ids.discard(guid)
            continue

        row = note_to_row(
            note,
            content_text,
            notebook_names=notebook_map,
            tag_names=tag_map,
        )
        yield row
        yielded_count += 1
        updated_note_ids.add(guid)

    log.info(
        "Evernote sync complete: %d row(s) yielded, %d tombstone(s) emitted.",
        yielded_count,
        tombstoned_count,
    )

    if yielded_count == 0 and tombstoned_count == 0:
        yield dlt.mark.materialize_table_schema()

    # Step 6: State is mutated ONLY after all rows and tombstones have been yielded
    # without error. Dlt persists resource state only when the run completes
    # successfully, ensuring that any mid-run failure (e.g. transport, auth,
    # rate-limit, or body fetch error) leaves the cursor and state untouched
    # so the run can be safely retried from the old cursor.
    if highest_chunk_usn is not None:
        state["last_usn"] = highest_chunk_usn
    elif "last_usn" not in state:
        state["last_usn"] = after_usn

    if current_sync_time is not None:
        state["last_sync_time"] = int(current_sync_time)

    state["notebooks"] = notebook_map
    state["tags"] = tag_map
    state["note_ids"] = sorted(updated_note_ids)
    state["account"] = bind


def evernote_source(
    token: str | None = None,
    sandbox: bool = False,
    notebook_names: list[str] | None = None,
    tag_names: list[str] | None = None,
    resource_name: str = "evernote_notes",
    client: Any = None,
    full_resync: bool = False,
):
    """Create an Evernote dlt resource for cognee memory ingestion.

    Extracts active notes filtered by optional notebook and tag scopes,
    emitting merge records and tombstones for trashed or expunged notes.
    """
    resolved_token = token or os.environ.get("EVERNOTE_TOKEN")
    if client is None and not resolved_token:
        raise ValueError(
            "Evernote authentication token required: pass token= or set EVERNOTE_TOKEN."
        )

    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("resource_name must be a nonempty string")

    name = NamingConvention().normalize_table_identifier(resource_name)
    api_client = (
        client if client is not None else EvernoteApi(token=resolved_token, sandbox=sandbox)
    )

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
        yield from _sync_records(
            api_client=api_client,
            state=state,
            notebook_names=notebook_names,
            tag_names=tag_names,
            sandbox=sandbox,
            full_resync=full_resync,
        )

    resource = records()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "evernote")
    setattr(resource, PIPELINE_SCOPE_ATTR, name)
    resource.client = api_client
    return resource
