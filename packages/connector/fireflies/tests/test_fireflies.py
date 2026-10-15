"""Credential-free tests for the Fireflies connector."""

from copy import deepcopy

import pytest

from cognee_community_connector_fireflies.fireflies import (
    DOCUMENT_SOURCE_ATTR,
    FirefliesClient,
    _build_transcript_query,
    _FirefliesConfig,
    _iter_transcript_metadata,
    _transcript_to_row,
    fireflies_source,
    sync_transcripts,
)


def _metadata(transcript_id, date, title=None):
    return {"id": transcript_id, "date": date, "title": title or transcript_id}


def _detail(transcript_id, date=1000):
    return {
        "id": transcript_id,
        "title": "Product weekly",
        "date": date,
        "transcript_url": f"https://app.fireflies.ai/view/{transcript_id}",
        "speakers": [{"id": "speaker-1", "name": "Alice"}],
        "sentences": [
            {
                "index": 0,
                "speaker_id": "speaker-1",
                "speaker_name": "Alice",
                "text": "I will ship the login flow on Friday.",
                "start_time": 5,
                "end_time": 12,
            }
        ],
        "summary": {
            "overview": "The team discussed authentication.",
            "short_summary": "Authentication planning.",
            "action_items": "Alice will ship the login flow on Friday.",
            "topics_discussed": ["Authentication"],
        },
    }


class FakeFirefliesClient:
    def __init__(self, transcripts, details=None):
        self.transcripts = list(transcripts)
        self.details = details or {
            item["id"]: _detail(item["id"], item["date"]) for item in transcripts
        }
        self.list_calls = []
        self.detail_calls = []

    def list_transcripts(self, *, limit, skip):
        self.list_calls.append((limit, skip))
        return self.transcripts[skip : skip + limit]

    def get_transcript(self, transcript_id, config):
        self.detail_calls.append((transcript_id, config))
        return self.details[transcript_id]


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def test_graphql_client_sends_bearer_key_and_variables():
    session = _Session({"data": {"transcripts": []}})
    client = FirefliesClient("secret-key", session=session)

    assert client.list_transcripts(limit=25, skip=50) == []
    url, request = session.calls[0]
    assert url == "https://api.fireflies.ai/graphql"
    assert request["headers"]["Authorization"] == "Bearer secret-key"
    assert request["json"]["variables"] == {"limit": 25, "skip": 50}


def test_graphql_errors_raise_even_on_successful_http_status():
    client = FirefliesClient(
        "secret-key",
        session=_Session({"errors": [{"message": "not authorized"}]}),
    )
    with pytest.raises(RuntimeError, match="GraphQL error"):
        client.list_transcripts(limit=50, skip=0)


def test_detail_query_selects_only_requested_categories():
    query = _build_transcript_query(
        _FirefliesConfig(
            include_transcript=False,
            include_summary=True,
            include_action_items=False,
            include_speakers=False,
        )
    )
    assert "overview" in query
    assert "sentences" not in query
    assert "speakers" not in query
    assert "action_items" not in query


def test_pagination_uses_limit_and_skip():
    client = FakeFirefliesClient([_metadata(str(i), i) for i in range(5)])
    rows = list(_iter_transcript_metadata(client, page_size=2))

    assert len(rows) == 5
    assert client.list_calls == [(2, 0), (2, 2), (2, 4)]


def test_rendered_document_preserves_speaker_entities_and_attribution():
    row = _transcript_to_row(_detail("meeting-1"), _FirefliesConfig())

    assert row["id"] == "meeting-1"
    assert "Speaker: Alice (Fireflies speaker ID: speaker-1)" in row["content"]
    assert "Alice (speaker_id: speaker-1) said:" in row["content"]
    assert "00:00:05-00:00:12" in row["content"]
    assert "Alice will ship the login flow" in row["content"]


def test_rendered_document_respects_content_selection():
    config = _FirefliesConfig(
        include_transcript=False,
        include_summary=False,
        include_action_items=True,
        include_speakers=False,
    )
    content = _transcript_to_row(_detail("meeting-1"), config)["content"]

    assert "## Action items" in content
    assert "## Transcript" not in content
    assert "## Summary" not in content
    assert "## Speakers" not in content


def test_first_sync_fetches_everything_and_records_state():
    client = FakeFirefliesClient([_metadata("a", 1000), _metadata("b", 2000)])
    state = {}

    rows = list(sync_transcripts(client, state))

    assert [row["id"] for row in rows] == ["a", "b"]
    assert [call[0] for call in client.detail_calls] == ["a", "b"]
    assert state["known_ids"] == ["a", "b"]
    assert state["last_date"] == 2000.0
    assert set(state["content_hashes"]) == {"a", "b"}


def test_incremental_sync_fetches_only_new_ids_or_dates():
    state = {}
    list(sync_transcripts(FakeFirefliesClient([_metadata("a", 1000), _metadata("b", 2000)]), state))
    client = FakeFirefliesClient([_metadata("a", 1000), _metadata("b", 3000), _metadata("c", 2000)])

    rows = list(sync_transcripts(client, state))

    assert [row["id"] for row in rows] == ["b", "c"]
    assert state["last_date"] == 3000.0
    assert state["known_ids"] == ["a", "b", "c"]


def test_deleted_transcript_emits_tombstone():
    state = {}
    list(
        sync_transcripts(
            FakeFirefliesClient([_metadata("a", 1000), _metadata("gone", 1000)]), state
        )
    )
    client = FakeFirefliesClient([_metadata("a", 1000)])

    assert list(sync_transcripts(client, state)) == [{"id": "gone", "_deleted": True}]
    assert state["known_ids"] == ["a"]


def test_successful_empty_sweep_deletes_everything():
    client = FakeFirefliesClient([])
    state = {"known_ids": ["a", "b"], "last_date": 2000}

    assert list(sync_transcripts(client, state)) == [
        {"id": "a", "_deleted": True},
        {"id": "b", "_deleted": True},
    ]
    assert state == {"known_ids": [], "last_date": 2000, "content_hashes": {}}


def test_failure_does_not_advance_state():
    class BrokenClient(FakeFirefliesClient):
        def get_transcript(self, transcript_id, config):
            raise RuntimeError("temporary failure")

    state = {"known_ids": ["a"], "last_date": 1000}
    client = BrokenClient([_metadata("a", 1000), _metadata("b", 2000)])

    with pytest.raises(RuntimeError, match="temporary failure"):
        list(sync_transcripts(client, state))
    assert state == {"known_ids": ["a"], "last_date": 1000}


def test_source_has_merge_primary_key_hard_delete_and_document_marker():
    pytest.importorskip("dlt")
    resource = fireflies_source(client=FakeFirefliesClient([]))

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "fireflies"


def test_real_dlt_merge_removes_a_deleted_transcript(tmp_path, monkeypatch):
    dlt = pytest.importorskip("dlt")
    # Keep dlt's global working directory inside the test sandbox. On Windows,
    # expanduser() consults USERPROFILE before dlt creates its run context.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    db_path = (tmp_path / "fireflies.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_fireflies_delete",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="meetings",
        pipelines_dir=str(tmp_path / "state"),
    )

    pipeline.run(
        fireflies_source(client=FakeFirefliesClient([_metadata("a", 1000), _metadata("b", 2000)]))
    )
    with pipeline.sql_client() as sql_client:
        rows = sql_client.execute_sql("SELECT id FROM fireflies_transcripts ORDER BY id")
    assert [row[0] for row in rows] == ["a", "b"]

    # The same pipeline restores dlt resource state. Transcript b disappears
    # from the successful sweep, so the connector emits its hard-delete row.
    pipeline.run(fireflies_source(client=FakeFirefliesClient([_metadata("a", 1000)])))
    with pipeline.sql_client() as sql_client:
        rows = sql_client.execute_sql("SELECT id FROM fireflies_transcripts ORDER BY id")
    assert [row[0] for row in rows] == ["a"]

    pipeline.run(fireflies_source(client=FakeFirefliesClient([])))
    with pipeline.sql_client() as sql_client:
        assert sql_client.execute_sql("SELECT id FROM fireflies_transcripts") == []


def test_source_validates_selection_page_size_and_credentials(monkeypatch):
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="at least one"):
        fireflies_source(
            client=object(),
            include_transcript=False,
            include_summary=False,
            include_action_items=False,
            include_speakers=False,
        )
    with pytest.raises(ValueError, match="page_size"):
        fireflies_source(client=object(), page_size=51)

    monkeypatch.delenv("FIREFLIES_API_KEY", raising=False)
    with pytest.raises(ValueError, match="FIREFLIES_API_KEY"):
        fireflies_source()


def test_unchanged_sync_emits_nothing_but_old_edits_and_late_summaries_are_detected():
    client = FakeFirefliesClient([_metadata("a", 1000)])
    client.details["a"]["summary"] = None
    state = {}
    assert len(list(sync_transcripts(client, state))) == 1
    original = deepcopy(state)
    assert list(sync_transcripts(client, state)) == []
    assert state == original

    client.details["a"]["summary"] = {"overview": "The summary is now ready."}
    rows = list(sync_transcripts(client, state))
    assert len(rows) == 1
    assert "The summary is now ready." in rows[0]["content"]
    assert state["last_date"] == 1000
    assert list(sync_transcripts(client, state)) == []


def test_older_unknown_ids_and_equal_cursor_dates_are_not_lost():
    state = {}
    list(sync_transcripts(FakeFirefliesClient([_metadata("a", 2000)]), state))
    rows = list(
        sync_transcripts(
            FakeFirefliesClient([_metadata("a", 2000), _metadata("b", 2000), _metadata("c", 1000)]),
            state,
        )
    )
    assert [row["id"] for row in rows] == ["b", "c"]
    assert state["last_date"] == 2000


def test_selected_content_change_triggers_resync():
    client = FakeFirefliesClient([_metadata("a", 1000)])
    state = {}
    list(sync_transcripts(client, state))
    rows = list(sync_transcripts(client, state, _FirefliesConfig(include_summary=False)))
    assert len(rows) == 1
    assert "## Summary" not in rows[0]["content"]


@pytest.mark.parametrize("invalid_page", [None, [{}], [None], "invalid"])
def test_invalid_listing_does_not_delete_or_advance_state(invalid_page):
    class BrokenClient(FakeFirefliesClient):
        def list_transcripts(self, **kwargs):
            return invalid_page

    state = {"known_ids": ["old"], "last_date": 1000, "content_hashes": {"old": "hash"}}
    before = deepcopy(state)
    with pytest.raises(RuntimeError):
        list(sync_transcripts(BrokenClient([]), state))
    assert state == before


def test_repeated_page_fails_instead_of_looping_or_deleting():
    class RepeatingClient(FakeFirefliesClient):
        def list_transcripts(self, **kwargs):
            return [_metadata("a", 1000)]

    state = {"known_ids": ["old"], "last_date": 500}
    before = deepcopy(state)
    with pytest.raises(RuntimeError, match="repeated"):
        list(
            sync_transcripts(
                RepeatingClient([_metadata("a", 1000)]), state, _FirefliesConfig(page_size=1)
            )
        )
    assert state == before


def test_failure_on_later_page_does_not_reconcile_deletions():
    class BrokenClient(FakeFirefliesClient):
        def list_transcripts(self, *, limit, skip):
            if skip:
                raise RuntimeError("page failed")
            return super().list_transcripts(limit=limit, skip=skip)

    state = {"known_ids": ["old"], "last_date": 500}
    before = deepcopy(state)
    rows = sync_transcripts(
        BrokenClient([_metadata("a", 1000)]), state, _FirefliesConfig(page_size=1)
    )
    assert next(rows)["id"] == "a"
    with pytest.raises(RuntimeError, match="page failed"):
        list(rows)
    assert state == before


@pytest.mark.parametrize("date", [float("nan"), float("inf"), -1, "bad"])
def test_invalid_dates_fail_without_updating_cursor(date):
    state = {}
    with pytest.raises(ValueError, match="timestamp"):
        list(sync_transcripts(FakeFirefliesClient([_metadata("a", date)]), state))
    assert state == {}


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_http_error_retries_then_succeeds(status, monkeypatch):
    import requests

    from cognee_community_connector_fireflies import fireflies as module

    responses = [requests.Response(), _Response({"data": {"transcripts": []}})]
    responses[0].status_code = status
    responses[0].headers["Retry-After"] = "0.5"
    responses[0]._content = b""
    responses[0]._content_consumed = True
    session = _Session(None)
    session.post = lambda *args, **kwargs: responses.pop(0)
    delays = []
    monkeypatch.setattr(module.time, "sleep", delays.append)
    assert FirefliesClient("key", session=session).list_transcripts(limit=50, skip=0) == []
    assert delays == [0.5]


def test_graphql_partial_data_is_an_error_and_does_not_echo_secrets():
    session = _Session(
        {"data": {"transcripts": []}, "errors": [{"message": "secret-key not authorized"}]}
    )
    with pytest.raises(RuntimeError, match="GraphQL error") as exc:
        FirefliesClient("secret-key", session=session).list_transcripts(limit=50, skip=0)
    assert "secret-key" not in str(exc.value)


def test_sentence_without_name_uses_top_level_speaker_and_zero_id():
    transcript = _detail("meeting-a")
    transcript["speakers"] = [{"id": 0, "name": "Alice"}]
    transcript["sentences"][0].update(speaker_id=0, speaker_name=None)
    row = _transcript_to_row(transcript, _FirefliesConfig())
    assert "Alice (speaker_id: 0) said:" in row["content"]


def test_anonymous_speakers_are_scoped_to_their_meeting():
    documents = []
    for meeting in ("a", "b"):
        transcript = _detail(meeting)
        transcript["speakers"] = [{"id": 1, "name": "Speaker 1"}]
        transcript["sentences"][0].update(speaker_id=1, speaker_name="Speaker 1")
        documents.append(_transcript_to_row(transcript, _FirefliesConfig())["content"])
    assert "Speaker 1 in meeting a" in documents[0]
    assert "Speaker 1 in meeting b" in documents[1]
