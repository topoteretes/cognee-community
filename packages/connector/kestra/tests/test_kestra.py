"""Hermetic unit tests for Kestra connector."""

from typing import Any, Dict, List
import pytest
from cognee_community_connector_kestra.kestra import (
    KestraClient,
    _execution_to_document,
    _flow_to_document,
    kestra_source,
)

SAMPLE_FLOW: Dict[str, Any] = {
    "id": "nightly_ingest",
    "namespace": "prod.etl",
    "description": "Production nightly data ingest from S3 into BigQuery",
    "labels": {"environment": "production", "team": "data-eng"},
    "tasks": [
        {"id": "download_s3", "type": "io.kestra.plugin.aws.s3.Download"},
        {"id": "load_bigquery", "type": "io.kestra.plugin.gcp.bigquery.Load"},
    ],
    "triggers": [
        {"id": "cron_trigger", "type": "io.kestra.core.models.triggers.types.Schedule"}
    ],
}

SAMPLE_EXECUTION: Dict[str, Any] = {
    "id": "exec-abc-12345",
    "namespace": "prod.etl",
    "flowId": "nightly_ingest",
    "state": {
        "current": "FAILED",
        "startDate": "2026-10-24T03:00:00Z",
        "endDate": "2026-10-24T03:02:15Z",
        "duration": "135s",
    },
    "taskRunList": [
        {
            "taskId": "download_s3",
            "state": {"current": "FAILED"},
        },
        {
            "taskId": "load_bigquery",
            "state": {"current": "CREATED"},
        },
    ],
}

SAMPLE_LOGS: List[Dict[str, Any]] = [
    {
        "level": "ERROR",
        "taskId": "download_s3",
        "message": "Connection to s3://company-warehouse timed out after 30000ms",
    }
]


class FakeResponse:
    def __init__(self, json_data: Any, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")

    def json(self) -> Any:
        return self._json_data


class FakeKestraHttpClient:
    def __init__(self, flows: List[Dict[str, Any]], executions: List[Dict[str, Any]], logs: List[Dict[str, Any]]) -> None:
        self.flows = flows
        self.executions = executions
        self.logs = logs

    def get(self, url: str, params: Any = None) -> FakeResponse:
        if "/api/v1/flows" in url:
            return FakeResponse({"results": self.flows})
        if "/api/v1/executions" in url:
            return FakeResponse({"results": self.executions})
        if "/api/v1/logs" in url:
            return FakeResponse(self.logs)
        return FakeResponse({}, status_code=404)


def test_flow_to_document():
    """Test converting a Kestra flow definition into a structured Markdown document."""
    doc = _flow_to_document(SAMPLE_FLOW, base_url="http://kestra.internal:8080")
    assert doc is not None
    assert doc["id"] == "kestra_flow_prod.etl_nightly_ingest"
    assert doc["title"] == "Kestra Flow: prod.etl.nightly_ingest"
    assert "http://kestra.internal:8080/ui/flows/edit/prod.etl/nightly_ingest" in doc["url"]

    content = doc["content"]
    assert "# Kestra Flow: prod.etl.nightly_ingest" in content
    assert "Production nightly data ingest from S3 into BigQuery" in content
    assert "- **download_s3** (`io.kestra.plugin.aws.s3.Download`)" in content
    assert "- **load_bigquery** (`io.kestra.plugin.gcp.bigquery.Load`)" in content
    assert "- **cron_trigger** (`io.kestra.core.models.triggers.types.Schedule`)" in content
    assert "`environment: production`" in content


def test_execution_to_document_with_failure_logs():
    """Test converting a failed execution and captured logs into a Markdown document."""
    doc = _execution_to_document(SAMPLE_EXECUTION, base_url="http://kestra.internal:8080", logs=SAMPLE_LOGS)
    assert doc is not None
    assert doc["id"] == "kestra_exec_exec-abc-12345"
    assert doc["title"] == "Execution: prod.etl.nightly_ingest - FAILED"

    content = doc["content"]
    assert "# Kestra Execution: prod.etl.nightly_ingest (FAILED)" in content
    assert "**Status:** **FAILED**" in content
    assert "**download_s3:** `FAILED`" in content
    assert "[ERROR] (download_s3): Connection to s3://company-warehouse timed out" in content


def test_kestra_source_dlt_execution():
    """Test full DLT source execution with mocked Kestra client."""
    fake_client = FakeKestraHttpClient(flows=[SAMPLE_FLOW], executions=[SAMPLE_EXECUTION], logs=SAMPLE_LOGS)
    client = KestraClient(base_url="http://kestra.internal:8080", http_client=fake_client)

    resource = kestra_source(base_url="http://kestra.internal:8080", client=client)
    items = list(resource)

    # 1 flow document + 1 execution document = 2 items
    assert len(items) == 2
    assert items[0]["id"] == "kestra_flow_prod.etl_nightly_ingest"
    assert items[1]["id"] == "kestra_exec_exec-abc-12345"
