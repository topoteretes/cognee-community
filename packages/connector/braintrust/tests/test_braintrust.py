"""Hermetic unit tests for Braintrust connector."""

from typing import Any, Dict, List
import pytest
from cognee_community_connector_braintrust.braintrust import (
    BraintrustClient,
    _dataset_to_document,
    _experiment_to_document,
    _project_to_document,
    _prompt_to_document,
    braintrust_source,
)

SAMPLE_PROJECT: Dict[str, Any] = {
    "id": "proj-101",
    "name": "rag-eval-prod",
    "description": "Production RAG evaluation and hallucination benchmark project",
    "created": "2026-10-20T10:00:00Z",
}

SAMPLE_EXPERIMENT: Dict[str, Any] = {
    "id": "exp-202",
    "name": "gemini-3.8-flash-baseline",
    "description": "Evaluating Gemini 3.8 Flash against baseline test suite",
    "dataset_id": "ds-303",
    "created": "2026-10-24T12:00:00Z",
    "scores": {
        "factuality": 0.98,
        "relevance": 0.96,
        "latency_p95": 145.2,
    },
    "metadata": {
        "model": "gemini-3.8-flash",
        "temperature": 0.2,
    },
}

SAMPLE_DATASET: Dict[str, Any] = {
    "id": "ds-303",
    "name": "golden-rag-qa-dataset",
    "description": "500 verified question-context-answer triples for ground truth testing",
    "created": "2026-10-21T08:00:00Z",
}

SAMPLE_PROMPT: Dict[str, Any] = {
    "id": "prompt-404",
    "name": "concise-rag-system-prompt",
    "description": "Production concise system instruction for grounding answers in context",
    "prompt_data": {
        "options": {"model": "gemini-3.8-flash"},
        "prompt": {
            "messages": [
                {"role": "system", "content": "You are a factual assistant. Answer strictly from context."}
            ]
        },
    },
}


class FakeResponse:
    def __init__(self, json_data: Any, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")

    def json(self) -> Any:
        return self._json_data


class FakeBraintrustHttpClient:
    def __init__(
        self,
        projects: List[Dict[str, Any]],
        experiments: List[Dict[str, Any]],
        datasets: List[Dict[str, Any]],
        prompts: List[Dict[str, Any]],
    ) -> None:
        self.projects = projects
        self.experiments = experiments
        self.datasets = datasets
        self.prompts = prompts

    def get(self, url: str, params: Any = None) -> FakeResponse:
        if "/projects" in url:
            return FakeResponse({"objects": self.projects})
        if "/experiments" in url:
            return FakeResponse({"objects": self.experiments})
        if "/datasets" in url:
            return FakeResponse({"objects": self.datasets})
        if "/prompts" in url:
            return FakeResponse({"objects": self.prompts})
        return FakeResponse({}, status_code=404)


def test_project_to_document():
    """Test converting a Braintrust project into a Markdown document."""
    doc = _project_to_document(SAMPLE_PROJECT)
    assert doc is not None
    assert doc["id"] == "bt_proj_proj-101"
    assert doc["title"] == "Braintrust Project: rag-eval-prod"
    assert "https://www.braintrust.dev/app/rag-eval-prod" in doc["url"]
    assert "Production RAG evaluation" in doc["content"]


def test_experiment_to_document():
    """Test converting an evaluation experiment into a document with scores and metadata."""
    doc = _experiment_to_document(SAMPLE_EXPERIMENT, project_name="rag-eval-prod")
    assert doc is not None
    assert doc["id"] == "bt_exp_exp-202"
    assert doc["title"] == "Braintrust Experiment: gemini-3.8-flash-baseline (rag-eval-prod)"
    assert "**factuality:** 0.98" in doc["content"]
    assert "**relevance:** 0.96" in doc["content"]
    assert "**model:** gemini-3.8-flash" in doc["content"]


def test_dataset_and_prompt_documents():
    """Test converting dataset and prompt templates into structured documents."""
    ds_doc = _dataset_to_document(SAMPLE_DATASET, project_name="rag-eval-prod")
    assert ds_doc is not None
    assert ds_doc["id"] == "bt_ds_ds-303"
    assert "golden-rag-qa-dataset" in ds_doc["title"]

    pr_doc = _prompt_to_document(SAMPLE_PROMPT, project_name="rag-eval-prod")
    assert pr_doc is not None
    assert pr_doc["id"] == "bt_prompt_prompt-404"
    assert "**Target Model:** `gemini-3.8-flash`" in pr_doc["content"]
    assert "You are a factual assistant." in pr_doc["content"]


def test_braintrust_source_dlt_execution():
    """Test full DLT source execution with mocked Braintrust API client."""
    fake_client = FakeBraintrustHttpClient(
        projects=[SAMPLE_PROJECT],
        experiments=[SAMPLE_EXPERIMENT],
        datasets=[SAMPLE_DATASET],
        prompts=[SAMPLE_PROMPT],
    )
    client = BraintrustClient(api_key="mock_key", http_client=fake_client)

    resource = braintrust_source(project_names=["rag-eval-prod"], client=client)
    items = list(resource)

    # 1 project + 1 experiment + 1 dataset + 1 prompt = 4 items
    assert len(items) == 4
    assert items[0]["id"] == "bt_proj_proj-101"
    assert items[1]["id"] == "bt_exp_exp-202"
    assert items[2]["id"] == "bt_ds_ds-303"
    assert items[3]["id"] == "bt_prompt_prompt-404"
