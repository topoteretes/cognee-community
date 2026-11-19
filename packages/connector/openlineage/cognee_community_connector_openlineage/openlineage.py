"""DLT source for OpenLineage / Marquez metadata (full-snapshot sync + forget-on-delete).

Fetches data pipeline lineage, job topologies, execution run histories, and dataset schema
facets from an OpenLineage HTTP backend (e.g., Marquez API or OpenLineage HTTP proxy),
then yields them as a dlt resource for Cognee's ingestion pipeline.

Unlike the relational dlt path (SQL/CSV tables), OpenLineage pipeline topologies are ingested
as *documents*: the source declares ``cognee_document_source = "openlineage"``, so
``resolve_dlt_sources`` routes each entity through the standard cognify entity-extraction
and graph completion pipeline.

The source implements a full-snapshot replacement model: ``write_disposition="replace"``
ensures each sync run overwrites staging with the exact set of pipeline jobs and datasets
currently visible in the lineage catalog. If a pipeline job is deprecated or decommissioned,
it drops out of subsequent syncs and is cleanly purged from Cognee's knowledge graph and vector
stores via ``orphan_cleanup``. Unchanged pipelines preserve a stable content hash (``data_id``),
preventing redundant LLM calls.
"""

import os
import re
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("openlineage_connector")

OPENLINEAGE_TABLE_NAME = "openlineage_jobs"
OPENLINEAGE_SOURCE_NAME = "openlineage"

_MAX_RETRIES = 5
_RETRY_BACKOFF_FACTOR = 0.5
_SECRET_PATTERN = re.compile(r"(token|secret|password|key|credential)", re.IGNORECASE)


def redact_sensitive_value(key: str, val: Any) -> Any:
    """Redact sensitive values based on key naming."""
    if isinstance(key, str) and _SECRET_PATTERN.search(key):
        return "[REDACTED]"
    return val


class OpenLineageClient:
    """HTTP client for querying OpenLineage / Marquez REST API endpoints."""

    def __init__(
        self,
        endpoint_url: str,
        api_key: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session = None

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "cognee-openlineage-connector/0.1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform GET request with exponential backoff retry on transient errors."""
        import httpx

        url = f"{self.endpoint_url}/{path.lstrip('/')}"
        headers = self._get_headers()

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, headers=headers, params=params)
                    if resp.status_code == 404:
                        logger.warning("OpenLineage endpoint %s not found (404)", url)
                        return {}
                    resp.raise_for_status()
                    return resp.json()
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                resp_code = getattr(getattr(exc, "response", None), "status_code", 0)
                is_transient = isinstance(
                    exc, httpx.ConnectError | httpx.TimeoutException
                ) or resp_code in (429, 500, 502, 503, 504)
                if is_transient and attempt < _MAX_RETRIES:
                    backoff = _RETRY_BACKOFF_FACTOR * (2 ** (attempt - 1))
                    logger.warning(
                        "Transient error contacting %s (%s). Retrying in %.2fs (attempt %d/%d)...",
                        url,
                        exc,
                        backoff,
                        attempt,
                        _MAX_RETRIES,
                    )
                    time.sleep(backoff)
                else:
                    logger.error("Failed to query OpenLineage %s: %s", url, exc)
                    raise

        return {}

    def list_namespaces(self) -> list[str]:
        """Fetch all registered lineage namespaces."""
        data = self.get("api/v1/namespaces")
        namespaces = data.get("namespaces", [])
        return [ns.get("name") for ns in namespaces if ns.get("name")]

    def list_jobs(self, namespace: str) -> list[dict[str, Any]]:
        """Fetch all pipeline jobs in a namespace."""
        data = self.get(f"api/v1/namespaces/{namespace}/jobs")
        return data.get("jobs", [])

    def list_runs(self, namespace: str, job_name: str, limit: int = 5) -> list[dict[str, Any]]:
        """Fetch execution runs for a specific job."""
        data = self.get(
            f"api/v1/namespaces/{namespace}/jobs/{job_name}/runs",
            params={"limit": limit},
        )
        return data.get("runs", [])

    def get_dataset(self, namespace: str, dataset_name: str) -> dict[str, Any]:
        """Fetch detailed dataset metadata including schema facets."""
        return self.get(f"api/v1/namespaces/{namespace}/datasets/{dataset_name}")


def _format_fields_table(fields: list[dict[str, Any]]) -> str:
    """Format dataset fields into a clean Markdown table."""
    if not fields:
        return "*No schema fields documented.*"

    lines = [
        "| Field Name | Type | Description |",
        "| :--- | :--- | :--- |",
    ]
    for field in fields:
        name = field.get("name", "unknown")
        ftype = field.get("type", "string")
        desc = field.get("description") or "-"
        lines.append(f"| `{name}` | `{ftype}` | {desc} |")
    return "\n".join(lines)


def _format_datasets_section(datasets: list[dict[str, Any]], client: Any | None, title: str) -> str:
    """Format input or output datasets with column-level schemas."""
    if not datasets:
        return f"### {title}\n*None documented.*"

    parts = [f"### {title}"]
    for ds in datasets:
        name = ds.get("name", "unknown")
        ns = ds.get("namespace", "default")
        fields = []

        # Extract schema facets if available directly in the job dataset object
        facets = ds.get("facets", {}) or {}
        schema_facet = facets.get("schema", {}) or {}
        fields = schema_facet.get("fields", [])

        # If fields not in job payload, try fetching full dataset definition
        if not fields and client and hasattr(client, "get_dataset"):
            try:
                full_ds = client.get_dataset(ns, name)
                full_facets = full_ds.get("facets", {}) or {}
                full_schema = full_facets.get("schema", {}) or {}
                fields = full_schema.get("fields", full_ds.get("fields", []))
            except Exception as exc:
                logger.debug("Could not fetch full dataset schema for %s/%s: %s", ns, name, exc)

        parts.append(f"#### Dataset: `{ns}.{name}`")
        if ds.get("description"):
            parts.append(f"*{ds.get('description')}*")
        parts.append(_format_fields_table(fields))

    return "\n\n".join(parts)


def _format_runs_table(runs: list[dict[str, Any]]) -> str:
    """Format recent execution runs into a Markdown table."""
    if not runs:
        return "*No execution runs recorded.*"

    lines = [
        "| Run ID | State | Started | Ended | Duration | Error / Summary |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for run in runs:
        run_id = run.get("id", "unknown")[:8]
        state = run.get("state", "UNKNOWN")
        started = run.get("nominalStartTime") or run.get("createdAt") or "-"
        ended = run.get("endedAt") or "-"
        duration = run.get("durationMs")
        duration_str = f"{duration / 1000.0:.2f}s" if duration is not None else "-"

        facets = run.get("facets", {}) or {}
        error_msg = "-"
        if "errorMessage" in facets:
            error_facet = facets.get("errorMessage", {})
            error_msg = error_facet.get("message") or str(error_facet)
        elif state in ("FAIL", "ABORTED"):
            error_msg = "Job run failed"

        row = f"| `{run_id}` | `{state}` | {started} | {ended} | {duration_str} | {error_msg} |"
        lines.append(row)

    return "\n".join(lines)


def _render_job_markdown(
    job: dict[str, Any],
    runs: list[dict[str, Any]],
    client: Any | None,
) -> str:
    """Render a comprehensive Markdown document describing a pipeline job and its lineage."""
    name = job.get("name", "unknown")
    namespace = job.get("namespace", "default")
    job_type = job.get("type", "BATCH")
    description = job.get("description") or "No description provided."

    inputs = job.get("inputs", [])
    outputs = job.get("outputs", [])

    inputs_md = _format_datasets_section(inputs, client, "Upstream Input Datasets")
    outputs_md = _format_datasets_section(outputs, client, "Downstream Output Datasets")
    runs_md = _format_runs_table(runs)

    doc = f"""# OpenLineage Job: {namespace}/{name}

- **Namespace**: `{namespace}`
- **Job Name**: `{name}`
- **Job Type**: `{job_type}`
- **Description**: {description}

## Lineage Topology

{inputs_md}

{outputs_md}

## Recent Execution Runs

{runs_md}
"""
    return doc.strip()


def openlineage_source(
    endpoint_url: str | None = None,
    api_key: str | None = None,
    namespaces: list[str] | None = None,
    job_names: list[str] | None = None,
    include_facets: bool = True,
    max_runs_per_job: int = 5,
    client: Any | None = None,
):
    """Create a dlt source that yields OpenLineage pipeline jobs and lineage as markdown documents.

    Args:
        endpoint_url: Base URL of API. Falls back to OPENLINEAGE_URL or MARQUEZ_URL.
        api_key: API token for the backend. Falls back to OPENLINEAGE_API_KEY/MARQUEZ_API_KEY.
        namespaces: Optional list of namespaces to ingest. If None, all namespaces are discovered.
        job_names: Optional list of specific job names to filter.
        include_facets: Whether to extract schema and error facets.
        max_runs_per_job: Number of recent execution runs to capture per job.
        client: Optional injected OpenLineageClient or mock instance for testing.
    """
    import dlt

    resolved_url = (
        endpoint_url or os.environ.get("OPENLINEAGE_URL") or os.environ.get("MARQUEZ_URL")
    )
    resolved_api_key = (
        api_key or os.environ.get("OPENLINEAGE_API_KEY") or os.environ.get("MARQUEZ_API_KEY")
    )

    if not resolved_url and client is None:
        raise ValueError(
            "An endpoint_url or client must be provided for the OpenLineage connector "
            "(or set OPENLINEAGE_URL / MARQUEZ_URL in the environment)."
        )

    resolved_client = client or OpenLineageClient(
        endpoint_url=resolved_url,
        api_key=resolved_api_key,
    )

    @dlt.source(name=OPENLINEAGE_SOURCE_NAME)
    def _openlineage():
        @dlt.resource(
            name=OPENLINEAGE_TABLE_NAME,
            write_disposition="replace",
        )
        def openlineage_jobs() -> Iterator[dict[str, Any]]:
            # 1. Discover or use provided namespaces
            target_namespaces = namespaces
            if target_namespaces is None:
                try:
                    target_namespaces = resolved_client.list_namespaces()
                except Exception as exc:
                    logger.error("Failed to list OpenLineage namespaces: %s", exc)
                    raise

            if not target_namespaces:
                logger.info("No OpenLineage namespaces found to ingest.")
                return

            # 2. Iterate through target namespaces
            for ns in target_namespaces:
                try:
                    jobs = resolved_client.list_jobs(ns)
                except Exception as exc:
                    logger.warning("Could not list jobs for namespace %s: %s", ns, exc)
                    continue

                for job in jobs:
                    j_name = job.get("name")
                    if not j_name:
                        continue

                    # Filter by job_names if specified
                    if job_names and j_name not in job_names:
                        continue

                    # Fetch recent runs
                    runs = []
                    if max_runs_per_job > 0:
                        try:
                            runs = resolved_client.list_runs(ns, j_name, limit=max_runs_per_job)
                        except Exception as exc:
                            logger.debug("Could not fetch runs for job %s/%s: %s", ns, j_name, exc)

                    rendered_content = _render_job_markdown(job, runs, resolved_client)
                    job_url = f"{resolved_client.endpoint_url}/api/v1/namespaces/{ns}/jobs/{j_name}"

                    yield {
                        "id": f"{ns}/{j_name}",
                        "url": job_url,
                        "title": f"OpenLineage Job: {ns}/{j_name}",
                        "content": rendered_content,
                        "namespace": ns,
                        "job_name": j_name,
                    }

        return openlineage_jobs

    source = _openlineage()
    # Tag with Cognee document-mode marker so documents flow through cognify
    setattr(source, DOCUMENT_SOURCE_ATTR, OPENLINEAGE_SOURCE_NAME)
    return source
