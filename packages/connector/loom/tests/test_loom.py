from __future__ import annotations

from pathlib import Path

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee_community_connector_loom.loom import (
    LOOM_TABLE_NAME,
    LoomClient,
    fetch_loom_videos,
    format_seconds,
    get_retry_delay,
    loom_source,
    video_to_document,
)


def test_client_auth_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOM_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Loom API token is required"):
        LoomClient(api_token=None)


def test_client_headers_custom_base_url() -> None:
    client = LoomClient(api_token="loom_secret", base_url="https://loom.custom.com/v1/")
    assert client.base_url == "https://loom.custom.com/v1"
    assert client.client.headers["Authorization"] == "Bearer loom_secret"
    client.close()


def test_get_retry_delay() -> None:
    resp_with_header = httpx.Response(429, headers={"Retry-After": "3.5"})
    assert get_retry_delay(resp_with_header, 0) == 3.5

    assert get_retry_delay(httpx.Response(429, headers={"Retry-After": "0"}), 0) == 0.0
    assert get_retry_delay(httpx.Response(429, headers={"Retry-After": "0.5"}), 1) == 0.5

    resp_no_header = httpx.Response(429)
    assert get_retry_delay(resp_no_header, 2, base_delay=1.0) == 4.0


@pytest.mark.parametrize(
    "invalid_header",
    [
        "-1",
        "-10.5",
        "nan",
        "NaN",
        "inf",
        "-inf",
        "Infinity",
        "-Infinity",
        "invalid",
        "",
    ],
)
def test_get_retry_delay_invalid_fallback(invalid_header: str) -> None:
    """Fallback to exponential backoff when Retry-After is negative, NaN, infinity, or invalid."""
    resp = httpx.Response(429, headers={"Retry-After": invalid_header})
    assert get_retry_delay(resp, 0, base_delay=1.0) == 1.0
    assert get_retry_delay(resp, 2, base_delay=1.0) == 4.0


def test_format_seconds() -> None:
    assert format_seconds(None) == "00:00"
    assert format_seconds(45) == "00:45"
    assert format_seconds(125) == "02:05"
    assert format_seconds(3665) == "01:01:05"


def test_client_retry_on_429() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"videos": [{"id": "v101", "title": "Sprint Demo"}]})

    transport = httpx.MockTransport(handler)
    with LoomClient(api_token="test_tok", transport=transport) as client:
        res = client.list_videos()
        assert len(res["videos"]) == 1
        assert call_count == 2


def test_client_network_error_retry() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.NetworkError("Reset connection")
        return httpx.Response(200, json={"videos": [{"id": "v202", "title": "Recovered"}]})

    transport = httpx.MockTransport(handler)
    with LoomClient(api_token="test_tok", transport=transport) as client:
        res = client.list_videos()
        assert len(res["videos"]) == 1
        assert call_count == 2


def test_client_list_videos_params() -> None:
    captured_url: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json={"videos": []})

    transport = httpx.MockTransport(handler)
    with LoomClient(api_token="test_tok", transport=transport) as client:
        client.list_videos(cursor="cur_xyz", limit=25, after="2026-10-01T00:00:00Z")

    assert captured_url is not None
    assert "limit=25" in captured_url
    assert "cursor=cur_xyz" in captured_url
    assert "created_after" in captured_url


def test_client_get_transcript_404_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(handler)
    with LoomClient(api_token="test_tok", transport=transport) as client:
        segments = client.get_transcript("v_missing")
        assert segments == []


def test_video_to_document_formatting() -> None:
    raw_video = {
        "id": "loom_demo_1",
        "title": "Architecture Walkthrough",
        "description": "Walkthrough of graph schema and vector indexing.",
        "url": "https://www.loom.com/share/loom_demo_1",
        "created_at": "2026-10-05T10:00:00Z",
        "duration": 185,
        "creator": {"name": "Alice Engineer", "email": "alice@loom.com"},
        "chapters": [
            {"title": "Overview", "timestamp": 0, "summary": "High-level goals"},
            {"title": "Schema", "timestamp": 60, "summary": "Entity model details"},
        ],
    }
    segments = [
        {"speaker": "Alice", "start_time": 5, "text": "Hello team, today we review the graph."},
        {"speaker": "Bob", "start_time": 65, "text": "How are relationships created?"},
    ]

    doc = video_to_document(raw_video, transcript_segments=segments)
    assert doc["id"] == "loom_demo_1"
    assert doc["title"] == "Architecture Walkthrough"
    assert doc["metadata"]["source"] == "loom"
    assert doc["metadata"]["creator"] == "Alice Engineer"

    text = doc["text"]
    assert "# Video: Architecture Walkthrough" in text
    assert "- **Creator**: Alice Engineer (alice@loom.com)" in text
    assert "- **Duration**: 03:05" in text
    assert "## Chapters & AI Summary" in text
    assert "- **[00:00] Overview**: High-level goals" in text
    assert "## Spoken Transcript" in text
    assert "**[00:05] Alice**: Hello team, today we review the graph." in text
    assert "**[01:05] Bob**: How are relationships created?" in text


def test_video_to_document_empty_transcript_placeholder() -> None:
    raw_video = {"id": "no_trans", "title": "Silent Video"}
    doc = video_to_document(raw_video, transcript_segments=[])
    assert "*(No transcript available for this recording)*" in doc["text"]


def test_source_metadata_document_mode() -> None:
    source = loom_source(api_token="dummy_token")
    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == "loom"


def test_source_basic_ingestion(tmp_path: Path) -> None:
    mock_videos = [
        {
            "id": "v1",
            "title": "Video One",
            "created_at": "2026-10-01T10:00:00Z",
            "transcript": [{"speaker": "A", "start": 0, "text": "Intro text"}],
        },
        {
            "id": "v2",
            "title": "Video Two",
            "created_at": "2026-10-02T10:00:00Z",
            "transcript": [{"speaker": "B", "start": 0, "text": "Outro text"}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"videos": mock_videos})

    transport = httpx.MockTransport(handler)
    pipeline = dlt.pipeline(
        pipeline_name="test_loom_pipeline",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )

    source = loom_source(api_token="dummy_tok", transport=transport)
    info = pipeline.run(source)
    assert info.has_failed_jobs is False

    with pipeline.sql_client() as client:
        with client.execute_query(f"SELECT COUNT(*) FROM {LOOM_TABLE_NAME}") as cursor:
            count = cursor.fetchone()[0]
            assert count == 2


def test_source_incremental_cursor_advancement(tmp_path: Path) -> None:
    batch1 = {
        "videos": [
            {
                "id": "v10",
                "title": "Old Video",
                "created_at": "2026-10-01T12:00:00Z",
            }
        ]
    }
    batch2 = {
        "videos": [
            {
                "id": "v20",
                "title": "New Video",
                "created_at": "2026-10-06T15:00:00Z",
            }
        ]
    }

    last_after_param: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal last_after_param
        if "transcript" in request.url.path:
            return httpx.Response(200, json={"segments": []})
        after = request.url.params.get("created_after")
        if after is not None:
            last_after_param = after
        if after == "2026-10-01T12:00:00Z":
            return httpx.Response(200, json=batch2)
        return httpx.Response(200, json=batch1)

    transport = httpx.MockTransport(handler)
    pipeline = dlt.pipeline(
        pipeline_name="test_loom_incremental",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )

    # First run
    source1 = loom_source(api_token="dummy_tok", incremental=True, transport=transport)
    pipeline.run(source1)

    # Second run
    source2 = loom_source(api_token="dummy_tok", incremental=True, transport=transport)
    pipeline.run(source2)

    assert last_after_param == "2026-10-01T12:00:00Z"

    with pipeline.sql_client() as client:
        with client.execute_query(f"SELECT COUNT(*) FROM {LOOM_TABLE_NAME}") as cursor:
            total = cursor.fetchone()[0]
            assert total == 2


def test_client_context_manager() -> None:
    with LoomClient(api_token="dummy_tok") as client:
        assert client.client.is_closed is False
    assert client.client.is_closed is True


def test_source_skip_transcripts_flag() -> None:
    sub_endpoint_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sub_endpoint_called
        if "transcript" in request.url.path:
            sub_endpoint_called = True
            return httpx.Response(200, json={"segments": []})
        return httpx.Response(200, json={"videos": [{"id": "no_sub", "title": "Test"}]})

    transport = httpx.MockTransport(handler)
    docs = list(
        fetch_loom_videos(
            api_token="dummy_tok",
            fetch_transcripts=False,
            transport=transport,
        )
    )
    assert len(docs) == 1
    assert sub_endpoint_called is False
