"""Evernote data-source connector for cognee.

Extracts notes, notebooks, and tags from Evernote into cognee memory with
incremental synchronization via Evernote sync chunks and forget-on-delete.
"""

import os
import time
from collections.abc import Callable
from typing import Any

from evernote.edam.error.ttypes import (
    EDAMErrorCode,
    EDAMNotFoundException,
    EDAMSystemException,
    EDAMUserException,
)
from evernote.edam.notestore import NoteStore
from evernote.edam.userstore import UserStore
from thrift.protocol.TBinaryProtocol import TBinaryProtocol
from thrift.transport.THttpClient import THttpClient
from thrift.transport.TTransport import TTransportException


class EvernoteError(Exception):
    """Base exception for Evernote connector."""


class EvernoteAuthError(EvernoteError):
    """Raised when authentication fails or token is invalid/expired."""


class NoteNotFoundError(EvernoteError):
    """Raised when a requested note is not found on Evernote."""


class EvernoteRateLimitError(EvernoteError):
    """Raised when Evernote rate limit retries are exhausted."""


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
    ) -> None:
        self.token = token
        self.sandbox = sandbox
        self.sleep_fn = sleep_fn
        self.max_retries = max_retries
        self._user_store = user_store
        self._note_store = note_store
        self._note_store_url: str | None = None

    def __repr__(self) -> str:
        return f"EvernoteApi(sandbox={self.sandbox!r})"

    def _create_user_store(self) -> Any:
        host = "sandbox.evernote.com" if self.sandbox else "www.evernote.com"
        url = f"https://{host}/edam/user"
        transport = THttpClient(url)
        protocol = TBinaryProtocol(transport)
        return UserStore.Client(protocol)

    def _create_note_store(self, url: str) -> Any:
        transport = THttpClient(url)
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

        urls = self._call_with_retry(user_store.getUserUrls, self.token)
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
            if exc.errorCode in (
                EDAMErrorCode.INVALID_AUTH,
                EDAMErrorCode.AUTH_EXPIRED,
                EDAMErrorCode.PERMISSION_DENIED,
            ):
                code_name = getattr(exc.errorCode, "name", str(exc.errorCode))
                raise EvernoteAuthError(f"Evernote authentication failed ({code_name})") from None
            raise EvernoteError(f"Evernote user error: {exc.errorCode}") from None

        if isinstance(exc, EDAMNotFoundException):
            target = guid or getattr(exc, "key", "")
            raise NoteNotFoundError(f"Evernote note not found: {target}") from None

        if isinstance(exc, (EvernoteError, EvernoteAuthError, NoteNotFoundError)):
            raise exc

        raise EvernoteError(f"Evernote API call failed: {exc.__class__.__name__}") from None

    def _call_with_retry(self, fn: Any, *args: Any, guid: str | None = None, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except EDAMSystemException as exc:
                if exc.errorCode == EDAMErrorCode.RATE_LIMIT_REACHED:
                    attempt += 1
                    if attempt > self.max_retries:
                        raise EvernoteRateLimitError(
                            "Evernote rate limit reached and retry attempts exhausted"
                        ) from None
                    duration = float(getattr(exc, "rateLimitDuration", 1) or 1)
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
        return self._call_with_retry(note_store.getSyncState, self.token)

    def get_filtered_sync_chunk(self, after_usn: int, max_entries: int, filter: Any) -> Any:
        """Fetch an incremental sync chunk from NoteStore."""
        note_store = self.get_note_store()
        return self._call_with_retry(
            note_store.getFilteredSyncChunk, self.token, after_usn, max_entries, filter
        )

    def get_note_content(self, guid: str) -> str:
        """Fetch the ENML note content by GUID from NoteStore."""
        note_store = self.get_note_store()
        return str(self._call_with_retry(note_store.getNoteContent, self.token, guid, guid=guid))


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
