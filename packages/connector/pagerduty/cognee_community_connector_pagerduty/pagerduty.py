import os
import hashlib
from typing import List, Dict, Any, Optional
import requests

PAGERDUTY_API_BASE_URL = "https://api.pagerduty.com"

def get_auth_headers(api_token: str) -> Dict[str, str]:
    """
    Constructs authorization headers for PagerDuty REST API v2.
    """
    token_val = api_token.strip()
    if not token_val.startswith("Token token="):
        auth_header = f"Token token={token_val}"
    else:
        auth_header = token_val

    return {
        "Authorization": auth_header,
        "Accept": "application/vnd.pagerduty+json;version=2",
        "Content-Type": "application/json",
        "User-Agent": "cognee-pagerduty-connector/0.1.0"
    }

def fetch_pagerduty_incidents(
    api_token: str,
    statuses: Optional[List[str]] = None,
    urgencies: Optional[List[str]] = None,
    limit: int = 50,
    offset: int = 0,
    api_base_url: str = PAGERDUTY_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches incidents from PagerDuty REST API v2.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/incidents"
    params: Dict[str, Any] = {
        "limit": min(limit, 100),
        "offset": offset,
        "total": "false"
    }
    if statuses:
        params["statuses[]"] = statuses
    if urgencies:
        params["urgencies[]"] = urgencies

    response = http.get(url, headers=get_auth_headers(api_token), params=params, timeout=20)
    response.raise_for_status()

    data = response.json()
    return data.get("incidents", [])

def fetch_pagerduty_services(
    api_token: str,
    limit: int = 50,
    offset: int = 0,
    api_base_url: str = PAGERDUTY_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches service catalog entries from PagerDuty REST API v2.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/services"
    params = {
        "limit": min(limit, 100),
        "offset": offset
    }

    response = http.get(url, headers=get_auth_headers(api_token), params=params, timeout=20)
    response.raise_for_status()

    data = response.json()
    return data.get("services", [])

def fetch_pagerduty_oncalls(
    api_token: str,
    limit: int = 50,
    api_base_url: str = PAGERDUTY_API_BASE_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches active on-call responders from PagerDuty REST API v2.
    """
    http = session or requests.Session()
    url = f"{api_base_url.rstrip('/')}/oncalls"
    params = {
        "limit": min(limit, 100)
    }

    response = http.get(url, headers=get_auth_headers(api_token), params=params, timeout=20)
    response.raise_for_status()

    data = response.json()
    return data.get("oncalls", [])

def format_incident_markdown(incident: Dict[str, Any]) -> str:
    """
    Synthesizes a PagerDuty incident record into semantic Markdown for Cognee Knowledge Graph ingestion.
    """
    inc_id = incident.get("id", "Unknown")
    inc_number = incident.get("incident_number", "N/A")
    title = incident.get("title") or incident.get("summary") or "Untitled Incident"
    status = incident.get("status", "unknown").upper()
    urgency = incident.get("urgency", "unknown").upper()
    created_at = incident.get("created_at", "N/A")
    html_url = incident.get("html_url", "")

    # Service extraction
    service_info = incident.get("service", {})
    service_name = service_info.get("summary") or service_info.get("name") or "Unassigned Service"
    service_id = service_info.get("id", "N/A")

    # Assignments extraction
    assignments = incident.get("assignments", [])
    assignees = []
    for assign in assignments:
        assignee = assign.get("assignee", {})
        name = assignee.get("summary") or assignee.get("name")
        if name:
            assignees.append(name)
    assignees_str = ", ".join(assignees) if assignees else "None"

    # Escalation policy
    ep_info = incident.get("escalation_policy", {})
    ep_name = ep_info.get("summary") or ep_info.get("name") or "Default"

    # Last status change
    last_status_change = incident.get("last_status_change_at", "N/A")

    lines = [
        f"# PagerDuty Incident #{inc_number}: {title}",
        "",
        f"- **Incident ID:** `{inc_id}`",
        f"- **Status:** {status}",
        f"- **Urgency:** {urgency}",
        f"- **Service:** {service_name} (`{service_id}`)",
        f"- **Assigned Responders:** {assignees_str}",
        f"- **Escalation Policy:** {ep_name}",
        f"- **Triggered At:** {created_at}",
        f"- **Last Status Change:** {last_status_change}",
    ]
    if html_url:
        lines.append(f"- **PagerDuty Console:** [{title}]({html_url})")

    lines.append("")
    lines.append("## Overview & Context")
    lines.append(f"Incident #{inc_number} affecting {service_name} is currently in {status} state with {urgency} urgency.")
    if assignees:
        lines.append(f"Active responders on duty: {assignees_str}.")

    return "\n".join(lines)

def format_service_markdown(service: Dict[str, Any]) -> str:
    """
    Formats a PagerDuty service into structured Markdown.
    """
    svc_id = service.get("id", "Unknown")
    name = service.get("name", "Unnamed Service")
    description = service.get("description") or "No description provided."
    status = service.get("status", "active")
    html_url = service.get("html_url", "")

    lines = [
        f"# PagerDuty Service: {name}",
        "",
        f"- **Service ID:** `{svc_id}`",
        f"- **Operational Status:** {status.upper()}",
        f"- **Description:** {description}",
    ]
    if html_url:
        lines.append(f"- **Console URL:** [{name}]({html_url})")

    lines.append("")
    lines.append("## Operational Context")
    lines.append(f"Service '{name}' is managed within PagerDuty for on-call escalation and alert correlation.")
    return "\n".join(lines)

class PagerDutyConnector:
    """
    Cognee data connector for PagerDuty.
    """
    def __init__(
        self,
        api_token: Optional[str] = None,
        api_base_url: str = PAGERDUTY_API_BASE_URL,
        statuses: Optional[List[str]] = None,
        urgencies: Optional[List[str]] = None,
        include_services: bool = True
    ):
        self.api_token = api_token or os.environ.get("PAGERDUTY_API_TOKEN", "")
        if not self.api_token:
            raise ValueError("PagerDuty API token must be provided or set in PAGERDUTY_API_TOKEN environment variable.")

        self.api_base_url = api_base_url
        self.statuses = statuses or ["triggered", "acknowledged", "resolved"]
        self.urgencies = urgencies
        self.include_services = include_services
        self.session = requests.Session()

    def fetch_documents(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Fetches PagerDuty incidents and services, synthesizing them into document nodes for Cognee entity extraction.
        """
        documents: List[Dict[str, Any]] = []

        # 1. Fetch incidents
        incidents = fetch_pagerduty_incidents(
            api_token=self.api_token,
            statuses=self.statuses,
            urgencies=self.urgencies,
            limit=limit,
            api_base_url=self.api_base_url,
            session=self.session
        )

        for inc in incidents:
            text = format_incident_markdown(inc)
            doc_id = hashlib.sha256(f"pagerduty_incident_{inc.get('id', '')}".encode("utf-8")).hexdigest()
            service_info = inc.get("service", {})

            documents.append({
                "id": doc_id,
                "text": text,
                "metadata": {
                    "source": "pagerduty",
                    "type": "incident",
                    "incident_id": inc.get("id"),
                    "incident_number": inc.get("incident_number"),
                    "status": inc.get("status"),
                    "urgency": inc.get("urgency"),
                    "service_id": service_info.get("id"),
                    "service_name": service_info.get("summary") or service_info.get("name"),
                    "created_at": inc.get("created_at"),
                }
            })

        # 2. Optionally fetch services
        if self.include_services:
            services = fetch_pagerduty_services(
                api_token=self.api_token,
                limit=limit,
                api_base_url=self.api_base_url,
                session=self.session
            )
            for svc in services:
                text = format_service_markdown(svc)
                doc_id = hashlib.sha256(f"pagerduty_service_{svc.get('id', '')}".encode("utf-8")).hexdigest()
                documents.append({
                    "id": doc_id,
                    "text": text,
                    "metadata": {
                        "source": "pagerduty",
                        "type": "service",
                        "service_id": svc.get("id"),
                        "service_name": svc.get("name"),
                        "status": svc.get("status"),
                    }
                })

        return documents
