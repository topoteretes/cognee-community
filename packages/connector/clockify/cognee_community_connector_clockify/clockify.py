import os
import hashlib
from typing import List, Dict, Any, Optional
import requests

CLOCKIFY_API_BASE_URL = "https://api.clockify.me/api/v1"

def get_auth_headers(api_key: str) -> Dict[str, str]:
    return {
        "X-Api-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "cognee-clockify-connector/0.1.0"
    }

def fetch_clockify_workspaces(
    api_key: str,
    api_base_url: str = CLOCKIFY_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches workspaces accessible to the authenticated user.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/workspaces"
    response = http.get(url, headers=get_auth_headers(api_key), timeout=20)
    response.raise_for_status()

    data = response.json()
    if isinstance(data, list):
        return data
    return []

def fetch_clockify_projects(
    api_key: str,
    workspace_id: str,
    api_base_url: str = CLOCKIFY_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches all projects inside a specific Clockify workspace.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/workspaces/{workspace_id}/projects"
    response = http.get(url, headers=get_auth_headers(api_key), timeout=20)
    response.raise_for_status()

    data = response.json()
    if isinstance(data, list):
        return data
    return []

def fetch_clockify_time_entries(
    api_key: str,
    workspace_id: str,
    user_id: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    api_base_url: str = CLOCKIFY_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches time tracking entries for a workspace/user with optional ISO date filters.
    """
    http = session or requests.Session()
    target_path = f"user/{user_id}/time-entries" if user_id else "time-entries"
    url = f"{api_base_url.rstrip('/')}/workspaces/{workspace_id}/{target_path}"

    params: Dict[str, Any] = {
        "page": page,
        "page-size": min(page_size, 500)
    }
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    response = http.get(url, headers=get_auth_headers(api_key), params=params, timeout=25)
    response.raise_for_status()

    data = response.json()
    if isinstance(data, list):
        return data
    return []

def format_time_entry_markdown(
    entry: Dict[str, Any],
    project_name: Optional[str] = None,
    client_name: Optional[str] = None
) -> str:
    """
    Renders a Clockify time entry into structured Markdown for Cognee semantic indexing.
    """
    entry_id = entry.get("id", "unknown")
    description = entry.get("description", "No description provided").strip()
    time_interval = entry.get("timeInterval", {})
    start_time = time_interval.get("start", "N/A")
    end_time = time_interval.get("end", "Ongoing")
    duration = time_interval.get("duration", "N/A")
    billable = entry.get("billable", False)
    tags = [t.get("name") for t in entry.get("tags", []) if isinstance(t, dict) and t.get("name")]
    tag_str = ", ".join(tags) if tags else "None"

    lines = [
        f"# Time Log: {description}",
        "",
        f"**Entry ID:** {entry_id}",
        f"**Project:** {project_name or entry.get('projectId', 'Unassigned')}",
        f"**Client:** {client_name or 'Internal'}",
        f"**Billable:** {'Yes' if billable else 'No'}",
        f"**Tags:** {tag_str}",
        "",
        "## Timeline & Duration",
        f"- **Start Time:** {start_time}",
        f"- **End Time:** {end_time}",
        f"- **ISO Duration:** {duration}",
        "",
        "## Activity Notes",
        description or "No notes recorded."
    ]

    return "\n".join(lines)

class ClockifyConnector:
    """
    Connector to ingest Clockify workspaces, projects, and time-tracking entries into Cognee.
    """
    def __init__(
        self,
        api_key: str,
        api_base_url: str = CLOCKIFY_API_BASE_URL
    ):
        if not api_key:
            raise ValueError("Clockify API Key is required.")
        self.api_key = api_key
        self.api_base_url = api_base_url

    def fetch_documents(
        self,
        workspace_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page_size: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Extracts projects and time logs, returning a list of Cognee-ready documents.
        """
        # Resolve workspace
        if not workspace_id:
            workspaces = fetch_clockify_workspaces(self.api_key, self.api_base_url)
            if not workspaces:
                return []
            workspace_id = workspaces[0]["id"]

        # Cache project mapping
        projects_raw = fetch_clockify_projects(self.api_key, workspace_id, self.api_base_url)
        project_map = {p["id"]: p.get("name", "Unknown Project") for p in projects_raw if "id" in p}

        # Fetch time logs
        entries = fetch_clockify_time_entries(
            self.api_key,
            workspace_id,
            start=start_date,
            end=end_date,
            page_size=page_size,
            api_base_url=self.api_base_url
        )

        documents = []
        for entry in entries:
            entry_id = entry.get("id", "entry")
            proj_id = entry.get("projectId")
            proj_name = project_map.get(proj_id, "General / No Project")

            doc_id = hashlib.sha256(f"clockify_{workspace_id}_{entry_id}".encode("utf-8")).hexdigest()

            documents.append({
                "id": doc_id,
                "text": format_time_entry_markdown(entry, project_name=proj_name),
                "metadata": {
                    "source": "clockify",
                    "workspace_id": workspace_id,
                    "entry_id": entry_id,
                    "project_id": proj_id,
                    "project_name": proj_name,
                    "billable": entry.get("billable", False),
                    "start": entry.get("timeInterval", {}).get("start")
                }
            })

        return documents
