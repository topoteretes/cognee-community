"""Unit and integration tests for the LangSmith data-source connector."""

import httpx
import pytest
from cognee_community_connector_langsmith.langsmith import (
    LangSmithAPIError,
    LangSmithAuthError,
    LangSmithClient,
    LangSmithRateLimitedError,
    _iter_rows,
    _render_dataset_markdown,
    _render_prompt_markdown,
    _render_run_markdown,
    langsmith_source,
)


def test_client_empty_key_raises():
    with pytest.raises(LangSmithAuthError):
        LangSmithClient(api_key="   ")


def test_client_sends_api_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-api-key") == "ls__test_key"
        return httpx.Response(200, json=[{"handle": "rag-prompt", "description": "RAG"}])

    transport = httpx.MockTransport(handler)
    client = LangSmithClient(api_key="ls__test_key")
    client._client = httpx.Client(transport=transport, base_url="https://api.smith.langchain.com")
    client._owns_client = True

    data = client.get("/prompts")
    assert len(data) == 1
    assert data[0]["handle"] == "rag-prompt"
    client.close()


def test_client_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized"})

    transport = httpx.MockTransport(handler)
    client = LangSmithClient(api_key="invalid_key")
    client._client = httpx.Client(transport=transport, base_url="https://api.smith.langchain.com")
    client._owns_client = True

    with pytest.raises(LangSmithAuthError):
        client.get("/prompts")
    client.close()


def test_client_429_long_retry_after_raises_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"}, json={"error": "Rate limit exceeded"})

    transport = httpx.MockTransport(handler)
    client = LangSmithClient(api_key="test_key")
    client._client = httpx.Client(transport=transport, base_url="https://api.smith.langchain.com")
    client._owns_client = True

    with pytest.raises(LangSmithRateLimitedError):
        client.get("/prompts")
    client.close()


def test_client_check_active_cancelled():
    def _cancel():
        raise LangSmithAPIError("Run cancelled by caller")

    client = LangSmithClient(api_key="test_key", check_active=_cancel)
    with pytest.raises(LangSmithAPIError, match="Run cancelled"):
        client.get("/prompts")


def test_client_budget_exceeded():
    client = LangSmithClient(api_key="test_key", max_requests=1)
    client.calls.append("/prompts")
    with pytest.raises(LangSmithAPIError, match="budget exceeded"):
        client.get("/prompts")


def test_render_prompt_markdown():
    prompt = {
        "handle": "qa-summarizer",
        "description": "Question answering prompt",
        "template": "Answer {query} using {context}",
        "input_variables": ["query", "context"],
        "updated_at": "2026-10-25T12:00:00Z",
    }
    doc_id, title, content = _render_prompt_markdown(prompt)
    assert doc_id == "langsmith:prompt:qa-summarizer"
    assert "qa-summarizer" in title
    assert "query, context" in content
    assert "Answer {query} using {context}" in content


def test_render_dataset_markdown():
    ds = {
        "id": "ds_500",
        "name": "Customer Support Benchmarks",
        "description": "Gold standard QA pairs",
        "example_count": 150,
        "updated_at": "2026-10-25T12:00:00Z",
    }
    doc_id, title, content = _render_dataset_markdown(ds)
    assert doc_id == "langsmith:dataset:ds_500"
    assert "Customer Support Benchmarks" in title
    assert "150" in content


def test_render_run_markdown():
    run = {
        "id": "run_900",
        "name": "ChatOpenAI",
        "status": "success",
        "latency": 0.82,
        "inputs": "What is Cognee?",
        "outputs": "Cognee is a memory layer for AI agents.",
    }
    doc_id, title, content = _render_run_markdown(run)
    assert doc_id == "langsmith:run:run_900"
    assert "ChatOpenAI" in title
    assert "0.82s" in content
    assert "What is Cognee?" in content


def test_source_invalid_resource_raises():
    with pytest.raises(ValueError, match="Unknown LangSmith resource"):
        langsmith_source(api_key="test", resources=["invalid_resource"])


def test_source_yields_all_selected_items(langsmith):
    langsmith.add_prompt("eval-prompt", "Evaluate RAG accuracy")
    langsmith.add_dataset("ds_1", "Eval Dataset")
    langsmith.add_run("run_1", "ChainExecution")

    state = {}
    stats = {}
    rows = list(_iter_rows(langsmith, ("prompts", "datasets", "runs"), state=state, stats=stats))

    assert len(rows) == 3
    assert stats["emitted"] == 3
    ids = {r["id"] for r in rows}
    assert "langsmith:prompt:eval-prompt" in ids
    assert "langsmith:dataset:ds_1" in ids
    assert "langsmith:run:run_1" in ids
    assert all(not r["_deleted"] for r in rows)


def test_source_incremental_sync_skips_unchanged(langsmith):
    prompt = langsmith.add_prompt("router-prompt", "Route query to agent")

    state = {}
    stats1 = {}
    rows1 = list(_iter_rows(langsmith, ("prompts",), state=state, stats=stats1))
    assert len(rows1) == 1
    assert stats1["emitted"] == 1

    stats2 = {}
    rows2 = list(_iter_rows(langsmith, ("prompts",), state=state, stats=stats2))
    assert len(rows2) == 0
    assert stats2["unchanged"] == 1

    prompt["template"] = "Route query to agent v2 with fallback"
    prompt["updated_at"] = "2026-10-26T12:00:00Z"

    stats3 = {}
    rows3 = list(_iter_rows(langsmith, ("prompts",), state=state, stats=stats3))
    assert len(rows3) == 1
    assert stats3["emitted"] == 1


def test_source_forgets_deleted_items(langsmith):
    langsmith.add_prompt("prompt_a", "Prompt A")
    langsmith.add_prompt("prompt_b", "Prompt B")

    state = {}
    stats1 = {}
    rows1 = list(_iter_rows(langsmith, ("prompts",), state=state, stats=stats1))
    assert len(rows1) == 2

    del langsmith.prompts["prompt_b"]

    stats2 = {}
    rows2 = list(_iter_rows(langsmith, ("prompts",), state=state, stats=stats2))

    assert len(rows2) == 1
    assert stats2["deleted"] == 1
    assert rows2[0]["id"] == "langsmith:prompt:prompt_b"
    assert rows2[0]["_deleted"] is True
