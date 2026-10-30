"""In-memory stand-in for the LangSmith API, shared by test modules."""

import pytest
from cognee_community_connector_langsmith.langsmith import (
    LangSmithAPIError,
    LangSmithAuthError,
    LangSmithQuotaError,
    LangSmithRateLimitedError,
)

class FakeLangSmith:
    """Implements ``get`` for LangSmith endpoints: /prompts, /datasets, /runs."""

    def __init__(self):
        self.prompts: dict[str, dict] = {}
        self.datasets: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.calls: list[str] = []
        self.bad_token = False
        self.rate_limited = False
        self.quota_exhausted = False
        self.server_error = False

    def add_prompt(self, handle: str, template: str, description: str = "", input_variables=None):
        prompt = {
            "handle": handle,
            "template": template,
            "description": description,
            "input_variables": input_variables or ["context", "query"],
            "updated_at": "2026-10-25T10:00:00.000Z",
        }
        self.prompts[handle] = prompt
        return prompt

    def add_dataset(self, ds_id: str, name: str, description: str = "", example_count: int = 25):
        ds = {
            "id": ds_id,
            "name": name,
            "description": description,
            "example_count": example_count,
            "updated_at": "2026-10-25T10:00:00.000Z",
        }
        self.datasets[ds_id] = ds
        return ds

    def add_run(self, run_id: str, name: str, status: str = "success", latency: float = 0.45):
        run = {
            "id": run_id,
            "name": name,
            "status": status,
            "latency": latency,
            "inputs": {"query": "sample query"},
            "outputs": {"answer": "sample answer"},
            "updated_at": "2026-10-25T10:00:00.000Z",
        }
        self.runs[run_id] = run
        return run

    def get(self, path: str, params=None):
        self.calls.append(path)
        if self.bad_token:
            raise LangSmithAuthError("LangSmith rejected the API key (HTTP 401)")
        if self.rate_limited:
            raise LangSmithRateLimitedError("LangSmith rate limit exceeded (HTTP 429)")
        if self.quota_exhausted:
            raise LangSmithQuotaError("LangSmith quota exhausted")
        if self.server_error:
            raise LangSmithAPIError("LangSmith server error: HTTP 500", status=500)

        clean = path.strip("/")
        parts = clean.split("/")

        if len(parts) == 1:
            collection = parts[0]
            if collection == "prompts":
                return list(self.prompts.values())
            if collection == "datasets":
                return list(self.datasets.values())
            if collection == "runs":
                return list(self.runs.values())
            raise LangSmithAPIError(f"Endpoint not found: {path}", status=404)

        if len(parts) == 2:
            collection, item_id = parts
            if collection == "prompts" and item_id in self.prompts:
                return dict(self.prompts[item_id])
            if collection == "datasets" and item_id in self.datasets:
                return dict(self.datasets[item_id])
            if collection == "runs" and item_id in self.runs:
                return dict(self.runs[item_id])
            raise LangSmithAPIError(f"Item not found: {path}", status=404)

        raise LangSmithAPIError(f"Unknown path: {path}", status=404)


@pytest.fixture
def langsmith():
    return FakeLangSmith()
