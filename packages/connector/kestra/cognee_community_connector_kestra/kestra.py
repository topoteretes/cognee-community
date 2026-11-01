"""Kestra orchestration platform data-source connector for Cognee.

Ingests flow definitions, execution states, task run durations, and operational failure logs
from Kestra into Cognee memory for intelligent automated DevOps & pipeline troubleshooting.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Generator, List, Optional
import dlt
import httpx

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = logging.getLogger(__name__)

DEFAULT_KESTRA_BASE_URL = "http://localhost:8080"


class KestraClient:
    """Client for querying the Kestra REST API v1."""

    def __init__(
        self,
        base_url: str = DEFAULT_KESTRA_BASE_URL,
        token: Optional[str] = None,
        http_client: Optional[Any] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"User-Agent": "CogneeCommunityKestraConnector/0.1.0", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self._http_client = http_client or httpx.Client(
            timeout=timeout,
            headers=headers,
        )

    def get_flows(self, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve flows, optionally filtered by namespace."""
        url = f"{self.base_url}/api/v1/flows/search"
        params = {}
        if namespace:
            params["namespace"] = namespace

        try:
            resp = self._http_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            # Kestra flow search responses return either a list or paginated object {"results": [...]}
            return data.get("results", data) if isinstance(data, dict) else data
        except Exception as exc:
            logger.error("Failed to fetch Kestra flows: %s", exc)
            raise

    def get_executions(
        self,
        namespace: Optional[str] = None,
        flow_id: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Retrieve executions matching filters."""
        url = f"{self.base_url}/api/v1/executions/search"
        params: Dict[str, Any] = {"size": limit}
        if namespace:
            params["namespace"] = namespace
        if flow_id:
            params["flowId"] = flow_id
        if state:
            params["state"] = state

        try:
            resp = self._http_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", data) if isinstance(data, dict) else data
        except Exception as exc:
            logger.error("Failed to fetch Kestra executions: %s", exc)
            raise

    def get_execution_logs(
        self,
        execution_id: str,
        min_level: str = "WARN",
    ) -> List[Dict[str, Any]]:
        """Retrieve task execution logs for an execution."""
        url = f"{self.base_url}/api/v1/logs/{execution_id}"
        params = {"minLevel": min_level}
        try:
            resp = self._http_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("results", [])
        except Exception as exc:
            logger.warning("Failed to fetch logs for execution %s: %s", execution_id, exc)
            return []


def _flow_to_document(flow: Dict[str, Any], base_url: str) -> Optional[Dict[str, Any]]:
    """Convert a Kestra flow definition into a structured Markdown document."""
    flow_id = flow.get("id")
    namespace = flow.get("namespace")
    if not flow_id or not namespace:
        return None

    description = flow.get("description") or "No description provided."
    tasks = flow.get("tasks", [])
    triggers = flow.get("triggers", [])
    labels = flow.get("labels", {})

    url = f"{base_url}/ui/flows/edit/{namespace}/{flow_id}"

    task_lines = []
    for t in tasks:
        t_id = t.get("id", "unnamed")
        t_type = t.get("type", "unknown.task")
        task_lines.append(f"- **{t_id}** (`{t_type}`)")

    trigger_lines = []
    for trig in triggers:
        trig_id = trig.get("id", "trigger")
        trig_type = trig.get("type", "schedule")
        trigger_lines.append(f"- **{trig_id}** (`{trig_type}`)")

    content = f"""# Kestra Flow: {namespace}.{flow_id}
**Namespace:** `{namespace}`
**Flow ID:** `{flow_id}`
**Description:** {description}
**Flow UI Link:** [{namespace}.{flow_id}]({url})

## Tasks Defined ({len(tasks)}):
{chr(10).join(task_lines) if task_lines else "- No explicit child tasks defined."}

## Triggers & Schedules:
{chr(10).join(trigger_lines) if trigger_lines else "- Manual execution only (no automated schedules)."}

## Labels:
{", ".join(f"`{k}: {v}`" for k, v in labels.items()) if labels else "None"}

---
*Ingested from Kestra Orchestrator at `{base_url}` via Cognee community connector.*"""

    return {
        "id": f"kestra_flow_{namespace}_{flow_id}",
        "url": url,
        "title": f"Kestra Flow: {namespace}.{flow_id}",
        "content": content,
    }


def _execution_to_document(
    exec_data: Dict[str, Any],
    base_url: str,
    logs: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Convert a Kestra execution record into a structured Markdown document."""
    exec_id = exec_data.get("id")
    namespace = exec_data.get("namespace")
    flow_id = exec_data.get("flowId")
    if not exec_id or not namespace or not flow_id:
        return None

    state = exec_data.get("state", {})
    current_state = state.get("current", "UNKNOWN")
    duration = state.get("duration", "N/A")
    start_date = state.get("startDate", "Unknown")
    end_date = state.get("endDate", "Unknown")

    url = f"{base_url}/ui/executions/{namespace}/{flow_id}/{exec_id}"

    # Task Run summaries
    task_runs = exec_data.get("taskRunList", [])
    task_run_lines = []
    for tr in task_runs:
        tr_id = tr.get("taskId", "task")
        tr_state = tr.get("state", {}).get("current", "UNKNOWN")
        task_run_lines.append(f"- **{tr_id}:** `{tr_state}`")

    # Error & Warning logs
    log_lines = []
    if logs:
        for log_entry in logs:
            level = log_entry.get("level", "INFO")
            msg = log_entry.get("message", "")
            task = log_entry.get("taskId", "flow")
            log_lines.append(f"[{level}] ({task}): {msg}")

    content = f"""# Kestra Execution: {namespace}.{flow_id} ({current_state})
**Execution ID:** `{exec_id}`
**Flow Reference:** [{namespace}.{flow_id}]({base_url}/ui/flows/edit/{namespace}/{flow_id})
**Status:** **{current_state}**
**Started:** {start_date} | **Finished:** {end_date}
**Duration:** {duration}

## Task Run Summary:
{chr(10).join(task_run_lines) if task_run_lines else "- No task runs recorded."}

## Captured Diagnostics & Failure Logs:
{chr(10).join(log_lines) if log_lines else "- No warning or error logs recorded for this execution."}

---
*Ingested from Kestra Execution `{exec_id}` via Cognee community connector.*"""

    return {
        "id": f"kestra_exec_{exec_id}",
        "url": url,
        "title": f"Execution: {namespace}.{flow_id} - {current_state}",
        "content": content,
    }


@dlt.source(name="kestra_source")
def kestra_source(
    base_url: str = DEFAULT_KESTRA_BASE_URL,
    token: Optional[str] = None,
    namespaces: Optional[List[str]] = None,
    include_logs: bool = True,
    limit_executions: int = 50,
    client: Optional[KestraClient] = None,
) -> Any:
    """DLT data-source for ingesting Kestra flows and executions into Cognee."""
    kestra_client = client or KestraClient(base_url=base_url, token=token)

    @dlt.resource(
        name="kestra_operational_memory",
        write_disposition="replace",
        primary_key="id",
    )
    def kestra_operational_memory() -> Generator[Dict[str, Any], None, None]:
        target_namespaces = namespaces or [None]

        for ns in target_namespaces:
            flows = kestra_client.get_flows(namespace=ns)
            for flow in flows:
                flow_doc = _flow_to_document(flow, base_url=base_url)
                if flow_doc:
                    yield flow_doc

            executions = kestra_client.get_executions(namespace=ns, limit=limit_executions)
            for execution in executions:
                exec_id = execution.get("id")
                logs = []
                if include_logs and exec_id:
                    logs = kestra_client.get_execution_logs(execution_id=exec_id, min_level="WARN")
                exec_doc = _execution_to_document(execution, base_url=base_url, logs=logs)
                if exec_doc:
                    yield exec_doc

    resource = kestra_operational_memory()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "kestra")
    return resource
