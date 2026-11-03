"""Hermetic unit tests for the Arize Phoenix data-source connector for Cognee."""

import json
import urllib.error
from unittest.mock import patch, MagicMock
import pytest

from cognee_community_connector_phoenix.phoenix import (
    PhoenixClient,
    phoenix_data,
    format_trace_to_markdown,
    extract_node_sets,
)


def test_client_auth_headers():
    """Verifies that headers include proper Authorization and User-Agent."""
    client_unauthed = PhoenixClient(base_url="http://localhost:6006")
    headers = client_unauthed._get_headers()
    assert headers["Accept"] == "application/json"
    assert "Authorization" not in headers

    client_authed = PhoenixClient(
        base_url="https://app.phoenix.arize.com",
        api_key="phx_secret_test_key",
        project_name="eval_cluster"
    )
    headers = client_authed._get_headers()
    assert headers["Authorization"] == "Bearer phx_secret_test_key"
    assert headers["api_key"] == "phx_secret_test_key"


def test_fetch_spans_and_trace_grouping():
    """Verifies that raw spans are grouped correctly by trace_id."""
    client = PhoenixClient(base_url="http://localhost:6006", project_name="my_proj")

    mock_spans = [
        {
            "id": "span_002",
            "trace_id": "trace_abc",
            "name": "call_retriever",
            "span_kind": "RETRIEVER",
            "attributes": {"retrieval.query": "search query"}
        },
        {
            "id": "span_001",
            "trace_id": "trace_abc",
            "name": "generate_answer",
            "span_kind": "LLM",
            "attributes": {"llm.model_name": "gemini-2.5-pro"}
        }
    ]

    with patch.object(client, "get_spans", return_value=mock_spans):
        with patch.object(client, "get_span_annotations", return_value=[]):
            res = client.fetch_traces_and_spans(project_name="my_proj")
            assert res["project_name"] == "my_proj"
            assert res["newest_span_id"] == "span_002"
            assert len(res["traces"]) == 1
            assert res["traces"][0]["trace_id"] == "trace_abc"
            assert len(res["traces"][0]["spans"]) == 2


def test_trace_markdown_document_formatting():
    """Verifies that format_trace_to_markdown formats LLM, Tool, and Annotation details."""
    mock_trace = {
        "trace_id": "tr_999",
        "project_name": "agent_evals",
        "session_id": "sess_42",
        "status_code": "OK",
        "start_time": "2026-11-03T10:00:00Z",
        "end_time": "2026-11-03T10:00:02Z",
        "spans": [
            {
                "name": "claude_reasoning",
                "span_kind": "LLM",
                "status_code": "OK",
                "attributes": {
                    "llm.model_name": "claude-3-5-sonnet",
                    "llm.token_count.prompt": 120,
                    "llm.token_count.completion": 45,
                    "llm.token_count.total": 165
                }
            },
            {
                "name": "execute_code",
                "span_kind": "TOOL",
                "status_code": "OK",
                "attributes": {
                    "tool.name": "bash_runner",
                    "tool.parameters": '{"cmd": "pytest"}',
                    "tool.output": "8 passed"
                }
            }
        ],
        "annotations": [
            {
                "name": "hallucination_eval",
                "label": "factual",
                "score": 1.0,
                "explanation": "No factual deviations detected.",
                "annotator_kind": "LLM"
            }
        ]
    }

    md = format_trace_to_markdown(mock_trace)
    assert "# Arize Phoenix LLM Trace: `tr_999`" in md
    assert "- **Project**: agent_evals" in md
    assert "- **Model**: `claude-3-5-sonnet`" in md
    assert "- **Tool Name**: `bash_runner`" in md
    assert "## Evaluation Annotations" in md
    assert "hallucination_eval" in md
    assert "`factual`" in md


def test_extract_node_sets_graph_links():
    """Verifies node set extraction for project, session, trace, models, and tools."""
    mock_trace = {
        "trace_id": "tr_100",
        "project_name": "prod_swarm",
        "session_id": "session_alpha",
        "spans": [
            {"attributes": {"llm.model_name": "gpt-4o"}},
            {"attributes": {"tool.name": "search_docs"}},
            {"attributes": {"tool.name": "python_eval"}}
        ]
    }

    nodes = extract_node_sets(mock_trace)
    assert nodes["project"] == ["prod_swarm"]
    assert nodes["session"] == ["session_alpha"]
    assert nodes["trace"] == ["tr_100"]
    assert nodes["model_name"] == ["gpt-4o"]
    assert nodes["tool_name"] == ["python_eval", "search_docs"]


def test_incremental_cursor_calculation():
    """Verifies that since_span_id stops span iteration when cursor is reached."""
    client = PhoenixClient()

    spans = [
        {"id": "span_005", "trace_id": "tr_5"},
        {"id": "span_004", "trace_id": "tr_4"},
        {"id": "span_003", "trace_id": "tr_3"},
        {"id": "span_002", "trace_id": "tr_2"},
        {"id": "span_001", "trace_id": "tr_1"},
    ]

    with patch.object(client, "get_spans", return_value=spans):
        with patch.object(client, "get_span_annotations", return_value=[]):
            res = client.fetch_traces_and_spans(since_span_id="span_003")
            # Should break when encountering span_003
            assert len(res["traces"]) == 2
            tids = [t["trace_id"] for t in res["traces"]]
            assert "tr_5" in tids
            assert "tr_4" in tids


def test_rate_limit_retry_handling():
    """Verifies exponential retry on HTTP 429."""
    client = PhoenixClient(base_url="http://localhost:6006")

    err_429 = urllib.error.HTTPError(
        url="http://localhost:6006/v1/projects/default/spans",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=None
    )

    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"data": []}'
        mock_resp.__enter__.return_value = mock_resp
        mock_open.side_effect = [err_429, mock_resp]

        res = client.get_spans()
        assert res == []
        assert mock_open.call_count == 2


def test_auth_failure_raises_permission_error():
    """Verifies that HTTP 401 raises PermissionError."""
    client = PhoenixClient(base_url="https://app.phoenix.arize.com", api_key="invalid_token")

    err_401 = urllib.error.HTTPError(
        url="https://app.phoenix.arize.com/v1/projects/default/spans",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=None
    )

    with patch("urllib.request.urlopen", side_effect=err_401):
        with pytest.raises(PermissionError, match="Arize Phoenix authentication failed"):
            client.get_spans()


def test_phoenix_data_generator_yields_documents():
    """Verifies that phoenix_data yields structured documents with valid metadata."""
    mock_pack = {
        "project_name": "eval_test",
        "newest_span_id": "sp_1",
        "traces": [
            {
                "trace_id": "tr_1",
                "project_name": "eval_test",
                "session_id": "sess_1",
                "status_code": "OK",
                "spans": [{"name": "llm_call", "span_kind": "LLM"}],
                "annotations": []
            }
        ]
    }

    mock_client = MagicMock()
    mock_client.fetch_traces_and_spans.return_value = mock_pack

    docs = list(phoenix_data(client=mock_client))
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "phoenix_trace_tr_1"
    assert doc["type"] == "phoenix_trace"
    assert "metadata" in doc
    assert doc["metadata"]["project_name"] == "eval_test"
    assert "node_sets" in doc["metadata"]
