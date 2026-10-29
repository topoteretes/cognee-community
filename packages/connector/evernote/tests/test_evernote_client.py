"""Unit tests for EvernoteApi client wrapper (auth + transport).

All tests run offline using fake Thrift clients — no real network and no
real credentials required.
"""

from unittest.mock import MagicMock, patch

import pytest
from evernote.edam.error.ttypes import (
    EDAMErrorCode,
    EDAMNotFoundException,
    EDAMSystemException,
    EDAMUserException,
)
from thrift.transport.TTransport import TTransportException

from cognee_community_connector_evernote import (
    EvernoteApi,
    EvernoteAuthError,
    EvernoteError,
    EvernoteRateLimitError,
    NoteNotFoundError,
    evernote_source,
)
from cognee_community_connector_evernote.evernote import EvernotePermissionError


class FakeUserStore:
    def __init__(self, note_store_url="https://fake.evernote.com/edam/note/shard"):
        self.note_store_url = note_store_url
        self.calls = []

    def getUserUrls(self, token):
        self.calls.append(("getUserUrls", token))
        return type("UserUrls", (), {"noteStoreUrl": self.note_store_url})()


class FakeNoteStore:
    def __init__(self):
        self.calls = []
        self.sync_state_side_effects = []
        self.filtered_sync_chunk_side_effects = []
        self.note_content_side_effects = []

    def getSyncState(self, token):
        self.calls.append(("getSyncState", token))
        if self.sync_state_side_effects:
            item = self.sync_state_side_effects.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return "fake-sync-state"

    def getFilteredSyncChunk(self, token, after_usn, max_entries, filter):
        self.calls.append(("getFilteredSyncChunk", token, after_usn, max_entries, filter))
        if self.filtered_sync_chunk_side_effects:
            item = self.filtered_sync_chunk_side_effects.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return "fake-sync-chunk"

    def getNoteContent(self, token, guid):
        self.calls.append(("getNoteContent", token, guid))
        if self.note_content_side_effects:
            item = self.note_content_side_effects.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return "<en-note>fake content</en-note>"


def test_construction_makes_no_network_call():
    """Constructing EvernoteApi does not resolve URL or call any remote method."""
    user_store = FakeUserStore()
    note_store = FakeNoteStore()
    api = EvernoteApi(token="test-token-123", user_store=user_store, note_store=note_store)
    assert len(user_store.calls) == 0
    assert len(note_store.calls) == 0
    assert api._note_store_url is None


def test_wrapper_methods_inject_token_as_first_argument():
    """All NoteStore wrapper methods pass the token as the first argument."""
    user_store = FakeUserStore()
    note_store = FakeNoteStore()
    token = "secret-token-123"
    api = EvernoteApi(token=token, user_store=user_store, note_store=note_store)

    # 1. get_sync_state
    state = api.get_sync_state()
    assert state == "fake-sync-state"
    assert note_store.calls[-1] == ("getSyncState", token)

    # 2. get_filtered_sync_chunk
    chunk = api.get_filtered_sync_chunk(after_usn=100, max_entries=50, filter="filter-obj")
    assert chunk == "fake-sync-chunk"
    assert note_store.calls[-1] == (
        "getFilteredSyncChunk",
        token,
        100,
        50,
        "filter-obj",
    )

    # 3. get_note_content
    content = api.get_note_content("guid-456")
    assert content == "<en-note>fake content</en-note>"
    assert note_store.calls[-1] == ("getNoteContent", token, "guid-456")


def test_notestore_url_resolved_once_and_cached():
    """NoteStore URL is resolved on the first call and cached for subsequent calls."""
    user_store = FakeUserStore("https://evernote.com/shard/s1/notestore")
    api = EvernoteApi(token="my-token", user_store=user_store)

    url1 = api.get_note_store_url()
    url2 = api.get_note_store_url()

    assert url1 == "https://evernote.com/shard/s1/notestore"
    assert url2 == url1
    # UserStore must have been queried exactly once
    assert len(user_store.calls) == 1
    assert user_store.calls[0] == ("getUserUrls", "my-token")


def test_rate_limit_retry_and_success():
    """Rate-limiting error sleeps for rateLimitDuration and retries up to success."""
    sleep_calls = []
    user_store = FakeUserStore()
    note_store = FakeNoteStore()
    rate_limit_error = EDAMSystemException(
        errorCode=EDAMErrorCode.RATE_LIMIT_REACHED,
        message="Too many requests",
        rateLimitDuration=30,
    )
    note_store.sync_state_side_effects = [rate_limit_error, "recovered-state"]

    api = EvernoteApi(
        token="my-token",
        user_store=user_store,
        note_store=note_store,
        sleep_fn=sleep_calls.append,
        max_retries=3,
    )

    state = api.get_sync_state()
    assert state == "recovered-state"
    assert sleep_calls == [30.0]
    assert len(note_store.calls) == 2


def test_rate_limit_gives_up_after_max_retries():
    """Rate-limiting error raises EvernoteRateLimitError when retries are exhausted."""
    sleep_calls = []
    user_store = FakeUserStore()
    note_store = FakeNoteStore()
    rate_limit_error = EDAMSystemException(
        errorCode=EDAMErrorCode.RATE_LIMIT_REACHED,
        rateLimitDuration=15,
    )
    note_store.sync_state_side_effects = [rate_limit_error] * 5

    api = EvernoteApi(
        token="my-token",
        user_store=user_store,
        note_store=note_store,
        sleep_fn=sleep_calls.append,
        max_retries=2,
    )

    with pytest.raises(EvernoteRateLimitError, match="Evernote rate limit reached"):
        api.get_sync_state()

    assert len(sleep_calls) == 2
    assert sleep_calls == [15.0, 15.0]


def test_rate_limit_exceeding_max_wait_raises_without_sleeping():
    """Rate-limiting error exceeding max_rate_limit_wait raises immediately without sleeping."""
    sleep_calls = []
    user_store = FakeUserStore()
    note_store = FakeNoteStore()
    rate_limit_error = EDAMSystemException(
        errorCode=EDAMErrorCode.RATE_LIMIT_REACHED,
        rateLimitDuration=500,
    )
    note_store.sync_state_side_effects = [rate_limit_error]

    api = EvernoteApi(
        token="my-token",
        user_store=user_store,
        note_store=note_store,
        sleep_fn=sleep_calls.append,
        max_rate_limit_wait=300,
    )

    with pytest.raises(EvernoteRateLimitError, match=r"500s.*exceeds maximum allowed wait.*300s"):
        api.get_sync_state()

    assert len(sleep_calls) == 0


def test_transport_timeout_is_configured():
    """THttpClient transport is configured with timeout_seconds * 1000 ms."""
    api = EvernoteApi(token="my-token", timeout_seconds=45)
    with patch("cognee_community_connector_evernote.evernote.THttpClient") as mock_thttp:
        mock_instance = MagicMock()
        mock_thttp.return_value = mock_instance

        api._create_user_store()
        mock_instance.setTimeout.assert_called_once_with(45000)

        mock_instance.reset_mock()
        api._create_note_store("https://evernote.com/shard/s1/notestore")
        mock_instance.setTimeout.assert_called_once_with(45000)


def test_transient_transport_error_retries_with_backoff():
    """Transient transport errors retry with exponential backoff before raising."""
    sleep_calls = []
    user_store = FakeUserStore()
    note_store = FakeNoteStore()
    transport_err = TTransportException(message="timed out")
    note_store.sync_state_side_effects = [transport_err, "ok-state"]

    api = EvernoteApi(
        token="my-token",
        user_store=user_store,
        note_store=note_store,
        sleep_fn=sleep_calls.append,
        max_retries=3,
    )

    state = api.get_sync_state()
    assert state == "ok-state"
    assert sleep_calls == [1.0]  # 2 ** (1 - 1)


def test_transient_transport_error_exhaustion():
    """Transient transport errors raise EvernoteError when retries are exhausted."""
    sleep_calls = []
    user_store = FakeUserStore()
    note_store = FakeNoteStore()
    transport_err = TTransportException(message="socket closed")
    note_store.sync_state_side_effects = [transport_err] * 5

    api = EvernoteApi(
        token="my-token",
        user_store=user_store,
        note_store=note_store,
        sleep_fn=sleep_calls.append,
        max_retries=2,
    )

    with pytest.raises(EvernoteError, match="Evernote transport failed after 2 retries"):
        api.get_sync_state()

    assert len(sleep_calls) == 2
    assert sleep_calls == [1.0, 2.0]


@pytest.mark.parametrize(
    "error_code",
    [
        EDAMErrorCode.INVALID_AUTH,
        EDAMErrorCode.AUTH_EXPIRED,
    ],
)
def test_auth_errors_map_to_evernote_auth_error(error_code):
    """Auth-related EDAMUserExceptions (INVALID_AUTH, AUTH_EXPIRED) map to EvernoteAuthError."""
    user_store = FakeUserStore()
    note_store = FakeNoteStore()
    note_store.sync_state_side_effects = [
        EDAMUserException(errorCode=error_code, parameter="authenticationToken")
    ]

    api = EvernoteApi(token="test-token", user_store=user_store, note_store=note_store)

    with pytest.raises(EvernoteAuthError, match="Evernote authentication failed"):
        api.get_sync_state()


def test_permission_denied_maps_to_evernote_permission_error():
    """PERMISSION_DENIED EDAMUserException maps to EvernotePermissionError."""
    user_store = FakeUserStore()
    note_store = FakeNoteStore()
    note_store.sync_state_side_effects = [
        EDAMUserException(
            errorCode=EDAMErrorCode.PERMISSION_DENIED, parameter="authenticationToken"
        )
    ]

    api = EvernoteApi(token="test-token", user_store=user_store, note_store=note_store)

    with pytest.raises(EvernotePermissionError, match="Evernote permission denied"):
        api.get_sync_state()


def test_not_found_maps_to_note_not_found_error():
    """EDAMNotFoundException on getNoteContent is mapped to NoteNotFoundError."""
    user_store = FakeUserStore()
    note_store = FakeNoteStore()
    note_store.note_content_side_effects = [
        EDAMNotFoundException(identifier="Note.guid", key="missing-guid-123")
    ]

    api = EvernoteApi(token="test-token", user_store=user_store, note_store=note_store)

    with pytest.raises(NoteNotFoundError, match="Evernote note not found: missing-guid-123"):
        api.get_note_content("missing-guid-123")


def test_token_never_appears_in_str_or_repr_of_raised_errors():
    """Sensitive token string must be absent from str() and repr() of all raised errors."""
    sensitive_token = "secret-super-sensitive-token-xyz-987"
    user_store = FakeUserStore()
    note_store = FakeNoteStore()

    api = EvernoteApi(
        token=sensitive_token,
        user_store=user_store,
        note_store=note_store,
        sleep_fn=lambda _: None,
        max_retries=1,
    )

    # 1. EvernoteAuthError
    note_store.sync_state_side_effects = [
        EDAMUserException(errorCode=EDAMErrorCode.INVALID_AUTH, parameter="authenticationToken")
    ]
    with pytest.raises(EvernoteAuthError) as exc_info:
        api.get_sync_state()
    assert sensitive_token not in str(exc_info.value)
    assert sensitive_token not in repr(exc_info.value)

    # 2. EvernotePermissionError
    note_store.sync_state_side_effects = [
        EDAMUserException(
            errorCode=EDAMErrorCode.PERMISSION_DENIED, parameter="authenticationToken"
        )
    ]
    with pytest.raises(EvernotePermissionError) as exc_info:
        api.get_sync_state()
    assert sensitive_token not in str(exc_info.value)
    assert sensitive_token not in repr(exc_info.value)

    # 3. NoteNotFoundError
    note_store.note_content_side_effects = [EDAMNotFoundException(identifier="Note.guid", key="g1")]
    with pytest.raises(NoteNotFoundError) as exc_info:
        api.get_note_content("g1")
    assert sensitive_token not in str(exc_info.value)
    assert sensitive_token not in repr(exc_info.value)

    # 4. EvernoteRateLimitError
    note_store.sync_state_side_effects = [
        EDAMSystemException(errorCode=EDAMErrorCode.RATE_LIMIT_REACHED, rateLimitDuration=5),
        EDAMSystemException(errorCode=EDAMErrorCode.RATE_LIMIT_REACHED, rateLimitDuration=5),
    ]
    with pytest.raises(EvernoteRateLimitError) as exc_info:
        api.get_sync_state()
    assert sensitive_token not in str(exc_info.value)
    assert sensitive_token not in repr(exc_info.value)

    # 5. EvernoteApi repr
    assert sensitive_token not in repr(api)
    assert sensitive_token not in str(api)


def test_evernote_source_builds_evernote_api_when_client_is_none():
    """evernote_source initializes EvernoteApi with token and sandbox flag."""
    source = evernote_source(token="tok-abc", sandbox=True)
    assert isinstance(source.client, EvernoteApi)
    assert source.client._token == "tok-abc"
    assert source.client.sandbox is True
    # Construction made no network calls:
    assert source.client._note_store_url is None
