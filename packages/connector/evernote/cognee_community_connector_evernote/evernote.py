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


class EvernoteSource:
    """Stub Evernote source representation for Milestone 1 and 2."""

    def __init__(
        self,
        token: str | None = None,
        sandbox: bool = False,
        notebook_names: list[str] | None = None,
        tag_names: list[str] | None = None,
        resource_name: str = "evernote_notes",
        client: Any = None,
    ) -> None:
        self.sandbox = sandbox
        self.notebook_names = notebook_names
        self.tag_names = tag_names
        self.resource_name = resource_name
        self.client = client
        self._token = token

    def __repr__(self) -> str:
        return (
            f"EvernoteSource(resource_name={self.resource_name!r}, "
            f"sandbox={self.sandbox!r}, "
            f"notebook_names={self.notebook_names!r}, "
            f"tag_names={self.tag_names!r})"
        )


def evernote_source(
    token: str | None = None,
    sandbox: bool = False,
    notebook_names: list[str] | None = None,
    tag_names: list[str] | None = None,
    resource_name: str = "evernote_notes",
    client: Any = None,
) -> EvernoteSource:
    """Create an Evernote data-source for cognee.

    Args:
        token: Evernote authentication token (or developer token). Falls back
            to the ``EVERNOTE_TOKEN`` environment variable.
        sandbox: Whether to target Evernote's sandbox environment.
        notebook_names: Optional list of notebook names to restrict ingestion.
        tag_names: Optional list of tag names to restrict ingestion.
        resource_name: Table / resource name for dlt (default "evernote_notes").
        client: Pre-built Evernote client (primarily an injection point for tests).

    Returns:
        EvernoteSource stub instance.
    """
    resolved_token = token or os.environ.get("EVERNOTE_TOKEN")
    if client is None and not resolved_token:
        raise ValueError(
            "Evernote authentication token required: pass token= or set EVERNOTE_TOKEN."
        )

    if client is None:
        client = EvernoteApi(token=resolved_token, sandbox=sandbox)

    return EvernoteSource(
        token=resolved_token,
        sandbox=sandbox,
        notebook_names=notebook_names,
        tag_names=tag_names,
        resource_name=resource_name,
        client=client,
    )
