"""Atlassian Jira Issues Data-Source Connector for Cognee.

Ingests Jira project tickets, agile sprint statuses, priority tags, and issue comments
from the Atlassian Jira Cloud REST API v3 into the Cognee graph and vector memory.
Resolves issue: cognee#6466.
"""

import os
import time
import base64
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Any, Optional, Iterator, Union
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


def _parse_adf_text(content: Any) -> str:
    """Recursively extracts plain text from Atlassian Document Format (ADF) nodes."""
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""

    node_type = content.get("type", "")
    if node_type == "text":
        return content.get("text", "")

    result = []
    for child in content.get("content", []):
        text = _parse_adf_text(child)
        if text:
            result.append(text)

    if node_type in ("paragraph", "heading"):
        return " ".join(result) + "\n"
    if node_type == "bulletList":
        return "\n".join(f"- {line}" for line in result)
    return " ".join(result)


class JiraClient:
    """Client for the Atlassian Jira Cloud REST API v3."""

    def __init__(
        self,
        domain: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        user_agent: Optional[str] = None
    ):
        self.domain = domain or os.getenv("JIRA_DOMAIN")
        self.email = email or os.getenv("JIRA_EMAIL")
        self.api_token = api_token or os.getenv("JIRA_API_TOKEN")
        self.token = token or os.getenv("JIRA_TOKEN") or os.getenv("JIRA_PAT")
        self.user_agent = user_agent or "JiraCogneeConnector/1.0 (https://github.com/topoteretes/cognee-community)"

        if base_url:
            self.base_url = base_url.rstrip("/")
        elif self.domain:
            self.base_url = f"https://{self.domain.strip()}.atlassian.net/rest/api/3"
        else:
            self.base_url = "https://demo.atlassian.net/rest/api/3"

    def _build_auth_header(self) -> Dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        elif self.email and self.api_token:
            auth_str = f"{self.email}:{self.api_token}"
            encoded = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
            return {"Authorization": f"Basic {encoded}"}
        return {}

    def _http_get(self, endpoint: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 3) -> Any:
        params = params or {}
        query_str = urllib.parse.urlencode(params)
        if endpoint.startswith("http"):
            full_url = f"{endpoint}?{query_str}" if query_str else endpoint
        else:
            full_url = f"{self.base_url}/{endpoint.lstrip('/')}?{query_str}" if query_str else f"{self.base_url}/{endpoint.lstrip('/')}"

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            **self._build_auth_header()
        }
        req = urllib.request.Request(full_url, headers=headers)

        last_exc = None
        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                last_exc = e
                if e.code == 401:
                    raise PermissionError(f"Jira authentication failed: invalid credentials for {self.base_url}")
                if e.code == 404:
                    raise ValueError(f"Jira resource not found: {endpoint}")
                if e.code == 429:
                    retry_after = int(e.headers.get("Retry-After", 2))
                    time.sleep(retry_after)
                    continue
                if e.code in (500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                last_exc = e
                time.sleep(1.0 * (attempt + 1))
                continue

        raise last_exc or RuntimeError(f"Failed to fetch {full_url}")

    def search_issues(
        self,
        jql: str = "ORDER BY updated DESC",
        fields: Optional[List[str]] = None,
        max_results: int = 50,
        start_at: int = 0
    ) -> Dict[str, Any]:
        """Queries Jira Cloud using JQL."""
        fields = fields or [
            "summary", "description", "status", "priority", "issuetype",
            "project", "assignee", "reporter", "labels", "created", "updated"
        ]
        params = {
            "jql": jql,
            "fields": ",".join(fields),
            "maxResults": max_results,
            "startAt": start_at
        }
        return self._http_get("search", params)

    def get_issue_comments(self, issue_key: str) -> List[Dict[str, Any]]:
        """Fetches comment discussion for an issue."""
        try:
            res = self._http_get(f"issue/{issue_key}/comment")
            return res.get("comments", [])
        except Exception:
            return []

    def fetch_issues_data(
        self,
        projects: Optional[List[str]] = None,
        since_timestamp: Optional[str] = None,
        max_issues: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Ingests Jira issues, extracts descriptions & comments, and constructs
        clean Markdown documents designed for Cognee semantic search and QA.
        """
        jql_clauses = []
        if projects:
            p_list = ", ".join(f"'{p}'" for p in projects)
            jql_clauses.append(f"project in ({p_list})")
        if since_timestamp:
            jql_clauses.append(f"updated >= '{since_timestamp}'")

        jql_clauses.append("ORDER BY updated DESC")
        jql = " AND ".join(jql_clauses[:-1]) + " " + jql_clauses[-1] if len(jql_clauses) > 1 else jql_clauses[0]

        issues_data = self.search_issues(jql=jql, max_results=max_issues or 50)
        issues = issues_data.get("issues", [])

        documents = []
        max_updated = since_timestamp or ""

        for iss in issues:
            key = iss.get("key", "UNKNOWN-KEY")
            fields = iss.get("fields", {})

            summary = fields.get("summary", "Untitled Issue")
            desc_raw = fields.get("description")
            desc_text = _parse_adf_text(desc_raw).strip() if desc_raw else ""

            status = fields.get("status", {}).get("name", "Unknown Status")
            priority = fields.get("priority", {}).get("name", "Normal")
            issuetype = fields.get("issuetype", {}).get("name", "Task")
            project_name = fields.get("project", {}).get("name") or fields.get("project", {}).get("key", "General")

            assignee = fields.get("assignee", {}).get("displayName") or "Unassigned"
            reporter = fields.get("reporter", {}).get("displayName") or "Anonymous"
            labels = fields.get("labels", [])
            created = fields.get("created", "")
            updated = fields.get("updated", "")

            if updated and updated > max_updated:
                max_updated = updated

            # Fetch issue comments
            comments = self.get_issue_comments(key)
            formatted_comments = []
            for c in comments:
                author = c.get("author", {}).get("displayName", "User")
                body = _parse_adf_text(c.get("body")).strip()
                date = c.get("created", "")
                formatted_comments.append(f"- **{author}** ({date}):\n  {body}")

            # Construct clean Markdown document
            text_doc = (
                f"# Jira Issue [{key}]: {summary}\n"
                f"- **Project**: {project_name}\n"
                f"- **Type**: {issuetype}\n"
                f"- **Status**: {status}\n"
                f"- **Priority**: {priority}\n"
                f"- **Assignee**: {assignee}\n"
                f"- **Reporter**: {reporter}\n"
                f"- **Labels**: {', '.join(labels) if labels else 'None'}\n"
                f"- **Created**: {created}\n"
                f"- **Last Updated**: {updated}\n\n"
            )
            if desc_text:
                text_doc += f"## Description\n{desc_text}\n\n"
            if formatted_comments:
                text_doc += "## Discussion & Comments\n" + "\n\n".join(formatted_comments) + "\n"

            documents.append({
                "id": f"jira_issue_{key}",
                "key": key,
                "summary": summary,
                "project": project_name,
                "type": issuetype,
                "status": status,
                "priority": priority,
                "assignee": assignee,
                "reporter": reporter,
                "labels": labels,
                "created": created,
                "updated": updated,
                "comments_count": len(comments),
                "text": text_doc
            })

        return {
            "cursor": max_updated,
            "issues_count": len(documents),
            "issues": documents
        }


def jira_data(
    domain: Optional[str] = None,
    client: Optional[JiraClient] = None,
    projects: Optional[Union[str, List[str]]] = None,
    since_timestamp: Optional[str] = None,
    max_issues: Optional[int] = None
) -> Iterator[Dict[str, Any]]:
    """Yields Cognee-compatible documents for Jira issues, sprint parameters, and comments."""
    cli = client or JiraClient(domain=domain)

    if isinstance(projects, str):
        projects = [p.strip() for p in projects.split(",") if p.strip()]

    pack = cli.fetch_issues_data(projects=projects, since_timestamp=since_timestamp, max_issues=max_issues)

    for iss in pack["issues"]:
        yield {
            "id": iss["id"],
            "type": "jira_issue",
            "title": f"Jira [{iss['key']}]: {iss['summary']}",
            "text": iss["text"],
            "metadata": iss
        }


@_optional_source
def jira_source(
    domain: Optional[str] = None,
    projects: Optional[Union[str, List[str]]] = None,
    since_timestamp: Optional[str] = None,
    max_issues: Optional[int] = None
):
    """Cognee / DLT source wrapper for Jira issues."""
    @_optional_resource(name="jira_data", write_disposition="append")
    def _data():
        yield from jira_data(
            domain=domain,
            projects=projects,
            since_timestamp=since_timestamp,
            max_issues=max_issues
        )

    return _data()
