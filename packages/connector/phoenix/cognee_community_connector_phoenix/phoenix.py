"""Arize Phoenix LLM Traces and Evaluations Data-Source Connector for Cognee.

Ingests LLM execution traces, OpenInference spans (LLM calls, tool executions, retriever spans),
and evaluation annotations from self-hosted or cloud Arize Phoenix into Cognee knowledge graph and vector memory.
Resolves issue: cognee#6450.
"""

import os
import time
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Any, Optional, Iterator, Union, Set
from datetime import datetime, timezone

# Optional dlt import for Cognee ecosystem compatibility
try:
    import dlt
    _has_dlt = True
except ImportError:
    dlt = None
    _has_dlt = False

def _optional_source(func):
    if _has_dlt and hasattr(dlt, "source"):
        return dlt.source(func)
    return func

def _optional_resource(*args, **kwargs):
    def decorator(func):
        if _has_dlt and hasattr(dlt, "resource"):
            return dlt.resource(*args, **kwargs)(func)
        return func
    return decorator


class PhoenixClient:
    """Client for Arize Phoenix REST API v1."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        project_name: Optional[str] = None,
        timeout: int = 30,
        user_agent: Optional[str] = None,
    ):
        self.base_url = (base_url or os.getenv("PHOENIX_COLLECTOR_ENDPOINT") or os.getenv("PHOENIX_BASE_URL") or "http://localhost:6006").rstrip("/")
        self.api_key = api_key or os.getenv("PHOENIX_API_KEY")
        self.project_name = project_name or os.getenv("PHOENIX_PROJECT_NAME") or "default"
        self.timeout = timeout
        self.user_agent = user_agent or "PhoenixCogneeConnector/1.0 (https://github.com/topoteretes/cognee-community)"

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["api_key"] = self.api_key
        return headers

    def _http_get(self, path: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 3) -> Dict[str, Any]:
        """Performs a GET request with automatic retry for rate limits (429)."""
        clean_path = path.lstrip("/")
        url = f"{self.base_url}/{clean_path}"
        if params:
            query_string = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query_string:
                url = f"{url}?{query_string}"

        req = urllib.request.Request(url, headers=self._get_headers(), method="GET")

        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_bytes = resp.read()
                    if not resp_bytes:
                        return {}
                    return json.loads(resp_bytes.decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    if attempt < max_retries - 1:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                    raise RuntimeError("Arize Phoenix rate limit exceeded (HTTP 429)") from e
                elif e.code == 401:
                    raise PermissionError(f"Arize Phoenix authentication failed (HTTP 401): {e.reason}") from e
                elif e.code == 404:
                    raise ValueError(f"Arize Phoenix resource not found (HTTP 404) at {url}") from e
                elif e.code >= 500 and attempt < max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                else:
                    raise RuntimeError(f"HTTP error {e.code}: {e.reason}") from e
            except urllib.error.URLError as e:
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise ConnectionError(f"Failed to connect to Arize Phoenix at {url}: {e.reason}") from e

        return {}

    def get_spans(
        self,
        project_name: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        sort_by: str = "id",
        order: str = "desc"
    ) -> List[Dict[str, Any]]:
        """Fetch spans from the Phoenix project in newest-first insertion order."""
        proj = project_name or self.project_name
        params: Dict[str, Any] = {
            "limit": limit,
            "sort": sort_by,
            "order": order,
        }
        if cursor:
            params["cursor"] = cursor

        res = self._http_get(f"v1/projects/{proj}/spans", params=params)
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data", res.get("spans", []))
        return []

    def get_traces(
        self,
        project_name: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetch traces summary from the Phoenix project."""
        proj = project_name or self.project_name
        params: Dict[str, Any] = {
            "limit": limit,
            "sort": "id",
            "order": "desc"
        }
        if cursor:
            params["cursor"] = cursor

        res = self._http_get(f"v1/projects/{proj}/traces", params=params)
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data", res.get("traces", []))
        return []

    def get_span_annotations(
        self,
        project_name: Optional[str] = None,
        span_ids: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Fetch evaluation annotations recorded on spans."""
        proj = project_name or self.project_name
        params: Dict[str, Any] = {}
        if span_ids:
            params["span_ids"] = ",".join(span_ids)

        res = self._http_get(f"v1/projects/{proj}/span_annotations", params=params)
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data", res.get("annotations", []))
        return []

    def fetch_traces_and_spans(
        self,
        project_name: Optional[str] = None,
        limit: int = 100,
        since_span_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetches spans and groups them by trace_id, resolving trace metadata and annotations."""
        proj = project_name or self.project_name
        raw_spans = self.get_spans(project_name=proj, limit=limit)

        traces_map: Dict[str, Dict[str, Any]] = {}
        span_ids: List[str] = []
        newest_span_id: Optional[str] = None

        for s in raw_spans:
            sid = str(s.get("id") or s.get("span_id", ""))
            tid = str(s.get("trace_id", ""))
            if not tid:
                continue

            if newest_span_id is None and sid:
                newest_span_id = sid

            if since_span_id and sid and sid <= since_span_id:
                break

            span_ids.append(sid)
            if tid not in traces_map:
                traces_map[tid] = {
                    "trace_id": tid,
                    "project_name": proj,
                    "session_id": s.get("attributes", {}).get("session.id", s.get("session_id", "unspecified")),
                    "start_time": s.get("start_time"),
                    "end_time": s.get("end_time"),
                    "status_code": s.get("status_code", "OK"),
                    "spans": [],
                    "annotations": []
                }
            traces_map[tid]["spans"].append(s)

        # Retrieve annotations for the collected spans if any
        if span_ids:
            try:
                annots = self.get_span_annotations(project_name=proj, span_ids=span_ids[:50])
                for a in annots:
                    s_id = str(a.get("span_id", ""))
                    for t in traces_map.values():
                        if any(str(sp.get("id") or sp.get("span_id")) == s_id for sp in t["spans"]):
                            t["annotations"].append(a)
            except Exception:
                pass

        return {
            "project_name": proj,
            "newest_span_id": newest_span_id,
            "traces": list(traces_map.values())
        }


def extract_node_sets(trace: Dict[str, Any]) -> Dict[str, List[str]]:
    """Extracts node sets for cognee graph links: project, session, trace, model_name, tool_name."""
    project_nodes = [trace.get("project_name", "default")]
    session_nodes = [trace.get("session_id", "unspecified")]
    trace_nodes = [trace.get("trace_id", "unknown")]
    model_nodes: Set[str] = set()
    tool_nodes: Set[str] = set()

    for s in trace.get("spans", []):
        attrs = s.get("attributes", {})
        model = attrs.get("llm.model_name") or attrs.get("model_name") or s.get("model_name")
        if model:
            model_nodes.add(str(model))

        tool = attrs.get("tool.name") or attrs.get("tool_name") or s.get("tool_name")
        if tool:
            tool_nodes.add(str(tool))

    return {
        "project": project_nodes,
        "session": session_nodes,
        "trace": trace_nodes,
        "model_name": sorted(list(model_nodes)),
        "tool_name": sorted(list(tool_nodes)),
    }


def format_trace_to_markdown(trace: Dict[str, Any]) -> str:
    """Formats an Arize Phoenix trace, its hierarchical spans, and eval annotations into structured Markdown."""
    tid = trace.get("trace_id", "unknown")
    proj = trace.get("project_name", "default")
    sess = trace.get("session_id", "unspecified")
    status = trace.get("status_code", "OK")
    start = trace.get("start_time", "N/A")
    end = trace.get("end_time", "N/A")

    lines = [
        f"# Arize Phoenix LLM Trace: `{tid}`",
        f"- **Project**: {proj}",
        f"- **Session ID**: `{sess}`",
        f"- **Status**: `{status}`",
        f"- **Start Time**: {start}",
        f"- **End Time**: {end}",
        "",
        "## Execution Span Tree",
    ]

    spans = trace.get("spans", [])
    if not spans:
        lines.append("_No span records attached to this trace._")
    else:
        for idx, span in enumerate(spans, 1):
            s_name = span.get("name", "unnamed_span")
            s_kind = span.get("span_kind", "UNKNOWN")
            s_status = span.get("status_code", "OK")
            attrs = span.get("attributes", {})

            lines.append(f"### {idx}. {s_name} (`{s_kind}` - `{s_status}`)")

            # LLM details
            if "llm.model_name" in attrs or s_kind == "LLM":
                model = attrs.get("llm.model_name", "unknown_model")
                tokens_prompt = attrs.get("llm.token_count.prompt", 0)
                tokens_comp = attrs.get("llm.token_count.completion", 0)
                tokens_tot = attrs.get("llm.token_count.total", tokens_prompt + tokens_comp)
                lines.append(f"- **Model**: `{model}` | **Tokens**: {tokens_prompt} prompt + {tokens_comp} completion = {tokens_tot} total")

                if "llm.input_messages" in attrs:
                    lines.append(f"- **Input Prompt Summary**: {str(attrs['llm.input_messages'])[:300]}")
                if "llm.output_messages" in attrs:
                    lines.append(f"- **Output Completion**: {str(attrs['llm.output_messages'])[:300]}")

            # Tool details
            elif "tool.name" in attrs or s_kind == "TOOL":
                tool = attrs.get("tool.name", s_name)
                params = attrs.get("tool.parameters", "{}")
                output = attrs.get("tool.output", "")
                lines.append(f"- **Tool Name**: `{tool}`")
                lines.append(f"- **Parameters**: `{str(params)[:200]}`")
                if output:
                    lines.append(f"- **Output Result**: `{str(output)[:300]}`")

            # Retriever details
            elif "retrieval.documents" in attrs or s_kind == "RETRIEVER":
                query = attrs.get("retrieval.query", "N/A")
                docs = attrs.get("retrieval.documents", [])
                lines.append(f"- **Retrieval Query**: {query}")
                lines.append(f"- **Retrieved Documents Count**: {len(docs) if isinstance(docs, list) else 1}")

            # Exceptions / Errors
            if s_status == "ERROR" or "exception.message" in attrs:
                err_msg = attrs.get("exception.message", span.get("status_message", "Unknown error"))
                err_type = attrs.get("exception.type", "Error")
                lines.append(f"- **⚠️ Error [{err_type}]**: {err_msg}")

            lines.append("")

    # Evaluation Annotations
    annots = trace.get("annotations", [])
    if annots:
        lines.append("## Evaluation Annotations")
        lines.append("| Evaluator / Name | Label | Score | Explanation | Annotator Kind |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for a in annots:
            name = a.get("name", "eval")
            label = a.get("label", "N/A")
            score = str(a.get("score", "N/A"))
            expl = a.get("explanation", "").replace("\n", " ")[:150]
            kind = a.get("annotator_kind", a.get("user_id", "LLM"))
            lines.append(f"| {name} | `{label}` | {score} | {expl} | {kind} |")
        lines.append("")

    return "\n".join(lines)


@_optional_source
def phoenix_data(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    project_name: Optional[str] = None,
    limit: int = 100,
    since_span_id: Optional[str] = None,
    client: Optional[PhoenixClient] = None,
) -> Iterator[Dict[str, Any]]:
    """Yields structured Cognee memory documents from Arize Phoenix LLM traces.

    Compatible with Cognee vector memory and spatial knowledge graph embeddings.
    """
    phoenix_client = client or PhoenixClient(
        base_url=base_url,
        api_key=api_key,
        project_name=project_name,
    )

    pack = phoenix_client.fetch_traces_and_spans(
        project_name=project_name,
        limit=limit,
        since_span_id=since_span_id,
    )

    traces = pack.get("traces", [])
    for trace in traces:
        tid = trace.get("trace_id", "unknown")
        markdown_text = format_trace_to_markdown(trace)
        node_sets = extract_node_sets(trace)

        yield {
            "id": f"phoenix_trace_{tid}",
            "text": markdown_text,
            "type": "phoenix_trace",
            "metadata": {
                "trace_id": tid,
                "project_name": trace.get("project_name", "default"),
                "session_id": trace.get("session_id", "unspecified"),
                "status_code": trace.get("status_code", "OK"),
                "start_time": trace.get("start_time"),
                "end_time": trace.get("end_time"),
                "spans_count": len(trace.get("spans", [])),
                "annotations_count": len(trace.get("annotations", [])),
                "node_sets": node_sets,
            }
        }
