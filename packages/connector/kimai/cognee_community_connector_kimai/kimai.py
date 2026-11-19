"""Kimai time-tracking platform data-source connector for cognee.

Extracts customers, projects, activities, and timesheets from self-hosted
Kimai instances or offline JSON snapshots into clean semantic markdown documents
for Cognee graph ingestion and RAG querying.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Union
from urllib.parse import urlparse

# Cognee convention for document routing
DOCUMENT_SOURCE_ATTR = "kimai"


def format_duration(seconds: int | float | None) -> str:
    """Format duration in seconds into human-readable hours and minutes."""
    if seconds is None:
        return "0m"
    try:
        sec = int(seconds)
        hours = sec // 3600
        minutes = (sec % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes}m ({sec}s)"
        return f"{minutes}m ({sec}s)"
    except (ValueError, TypeError):
        return f"{seconds}s"


def format_customer_document(customer: Dict[str, Any], base_url: str = "") -> str:
    """Render a Kimai customer record into a clean markdown document."""
    cid = customer.get("id", "N/A")
    name = customer.get("name", "Unnamed Customer")
    number = customer.get("number", "N/A")
    country = customer.get("country", "N/A")
    currency = customer.get("currency", "USD")
    comment = (customer.get("comment") or "").strip()
    visible = customer.get("visible", True)

    lines = [
        f"# [Kimai Customer] {name} (ID: {cid})",
        "",
        "## Customer Metadata",
        f"- **Customer ID:** `{cid}`",
        f"- **Customer Name:** `{name}`",
        f"- **Customer Number:** `{number}`",
        f"- **Country:** `{country}`",
        f"- **Currency:** `{currency}`",
        f"- **Status:** `{'Active / Visible' if visible else 'Archived / Hidden'}`",
    ]
    if base_url:
        lines.append(f"- **Kimai Instance:** `{base_url}`")
    if comment:
        lines.extend(["", "## Notes & Profile", comment])

    return "\n".join(lines)


def format_project_document(project: Dict[str, Any], base_url: str = "") -> str:
    """Render a Kimai project record into a clean markdown document."""
    pid = project.get("id", "N/A")
    name = project.get("name", "Unnamed Project")
    number = project.get("number", "N/A")
    comment = (project.get("comment") or "").strip()
    order_number = project.get("orderNumber", "N/A")
    visible = project.get("visible", True)

    customer_info = project.get("customer")
    if isinstance(customer_info, dict):
        cust_name = customer_info.get("name", "N/A")
        cust_id = customer_info.get("id", "N/A")
    else:
        cust_name = "N/A"
        cust_id = str(customer_info or "N/A")

    lines = [
        f"# [Kimai Project] {name} (ID: {pid})",
        "",
        "## Project Metadata",
        f"- **Project ID:** `{pid}`",
        f"- **Project Name:** `{name}`",
        f"- **Project Number:** `{number}`",
        f"- **Order Number:** `{order_number}`",
        f"- **Parent Customer:** `{cust_name}` (ID: `{cust_id}`)",
        f"- **Status:** `{'Active / Visible' if visible else 'Archived / Ended'}`",
    ]
    if base_url:
        lines.append(f"- **Kimai Instance:** `{base_url}`")
    if comment:
        lines.extend(["", "## Project Scope & Instructions", comment])

    return "\n".join(lines)


def format_activity_document(activity: Dict[str, Any], base_url: str = "") -> str:
    """Render a Kimai activity record into a clean markdown document."""
    aid = activity.get("id", "N/A")
    name = activity.get("name", "Unnamed Activity")
    comment = (activity.get("comment") or "").strip()
    visible = activity.get("visible", True)

    project_info = activity.get("project")
    if isinstance(project_info, dict):
        proj_name = project_info.get("name", "N/A")
        proj_id = project_info.get("id", "N/A")
        scope = f"`{proj_name}` (ID: `{proj_id}`)"
    elif project_info is not None:
        scope = f"Project ID `{project_info}`"
    else:
        scope = "Global (All Projects)"

    lines = [
        f"# [Kimai Activity] {name} (ID: {aid})",
        "",
        "## Activity Metadata",
        f"- **Activity ID:** `{aid}`",
        f"- **Activity Name:** `{name}`",
        f"- **Assigned Scope:** {scope}",
        f"- **Status:** `{'Active / Visible' if visible else 'Archived / Hidden'}`",
    ]
    if base_url:
        lines.append(f"- **Kimai Instance:** `{base_url}`")
    if comment:
        lines.extend(["", "## Activity Description", comment])

    return "\n".join(lines)


def format_timesheet_document(
    timesheet: Dict[str, Any],
    base_url: str = "",
    include_rates: bool = False,
) -> str:
    """Render a Kimai timesheet entry into an agent-queryable markdown document."""
    tid = timesheet.get("id", "N/A")
    begin = timesheet.get("begin", "Unknown Start")
    end = timesheet.get("end", "In Progress")
    duration_sec = timesheet.get("duration", 0)
    duration_str = format_duration(duration_sec)
    description = (timesheet.get("description") or "No description provided.").strip()
    billable = timesheet.get("billable", True)

    # User display name (emails explicitly excluded to protect privacy)
    user_info = timesheet.get("user")
    if isinstance(user_info, dict):
        user_name = user_info.get("alias") or user_info.get("username") or "Unknown Team Member"
        user_id = user_info.get("id", "N/A")
    else:
        user_name = "Team Member"
        user_id = str(user_info or "N/A")

    # Project info
    project_info = timesheet.get("project")
    if isinstance(project_info, dict):
        proj_name = project_info.get("name", "N/A")
        proj_id = project_info.get("id", "N/A")
        cust_info = project_info.get("customer")
        if isinstance(cust_info, dict):
            cust_name = cust_info.get("name", "N/A")
            cust_id = cust_info.get("id", "N/A")
        else:
            cust_name = "N/A"
            cust_id = str(cust_info or "N/A")
    else:
        proj_name = "N/A"
        proj_id = str(project_info or "N/A")
        cust_name = "N/A"
        cust_id = "N/A"

    # Activity info
    act_info = timesheet.get("activity")
    if isinstance(act_info, dict):
        act_name = act_info.get("name", "N/A")
        act_id = act_info.get("id", "N/A")
    else:
        act_name = "N/A"
        act_id = str(act_info or "N/A")

    tags = timesheet.get("tags", [])
    if isinstance(tags, list):
        tag_str = ", ".join(f"`{t}`" for t in tags) if tags else "None"
    else:
        tag_str = str(tags)

    lines = [
        f"# [Kimai Work Log] Entry #{tid} — {proj_name} / {act_name}",
        "",
        "## Summary & Metadata",
        f"- **Timesheet Entry ID:** `{tid}`",
        f"- **Contributor:** `{user_name}` (User ID: `{user_id}`)",
        f"- **Customer:** `{cust_name}` (ID: `{cust_id}`)",
        f"- **Project:** `{proj_name}` (ID: `{proj_id}`)",
        f"- **Activity:** `{act_name}` (ID: `{act_id}`)",
        f"- **Time Interval:** `{begin}` to `{end}`",
        f"- **Logged Duration:** {duration_str}",
        f"- **Billing Classification:** `{'Billable' if billable else 'Non-Billable'}`",
        f"- **Tags:** {tag_str}",
    ]
    if base_url:
        lines.append(f"- **Kimai Instance:** `{base_url}`")

    # Financial rates are strictly opt-in
    if include_rates:
        hourly_rate = timesheet.get("hourlyRate")
        rate = timesheet.get("rate")
        lines.extend([
            f"- **Hourly Rate:** `{hourly_rate}`",
            f"- **Calculated Fee:** `{rate}`",
        ])

    lines.extend([
        "",
        "## Logged Work Description",
        f"> {description}",
    ])

    return "\n".join(lines)


def parse_kimai_records(
    data: Dict[str, Any],
    base_url: str = "",
    include_rates: bool = False,
) -> List[Dict[str, Any]]:
    """Parse customers, projects, activities, and timesheets from raw dictionary payload.

    Filters running timers (where end time is null or empty) from text indexing,
    while calculating graph node sets and deterministic content hashes.
    """
    documents = []
    host = urlparse(base_url).netloc if base_url else "kimai-instance"

    # 1. Customers
    customers = data.get("customers", [])
    for cust in customers:
        cid = cust.get("id")
        doc_text = format_customer_document(cust, base_url=base_url)
        content_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
        documents.append({
            "id": f"kimai_{host}_customer_{cid}",
            "type": "customer",
            "entity_id": cid,
            "text": doc_text,
            "hash": content_hash,
            "node_sets": [f"{host}:customer:{cid}"],
            "metadata": {
                "source": DOCUMENT_SOURCE_ATTR,
                "entity": "customer",
                "customer_id": cid,
                "name": cust.get("name"),
            },
        })

    # 2. Projects
    projects = data.get("projects", [])
    for proj in projects:
        pid = proj.get("id")
        cid = proj.get("customer", {}).get("id") if isinstance(proj.get("customer"), dict) else proj.get("customer")
        doc_text = format_project_document(proj, base_url=base_url)
        content_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
        node_sets = [f"{host}:project:{pid}"]
        if cid:
            node_sets.append(f"{host}:customer:{cid}")
        documents.append({
            "id": f"kimai_{host}_project_{pid}",
            "type": "project",
            "entity_id": pid,
            "text": doc_text,
            "hash": content_hash,
            "node_sets": node_sets,
            "metadata": {
                "source": DOCUMENT_SOURCE_ATTR,
                "entity": "project",
                "project_id": pid,
                "customer_id": cid,
                "name": proj.get("name"),
            },
        })

    # 3. Activities
    activities = data.get("activities", [])
    for act in activities:
        aid = act.get("id")
        pid = act.get("project", {}).get("id") if isinstance(act.get("project"), dict) else act.get("project")
        doc_text = format_activity_document(act, base_url=base_url)
        content_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
        node_sets = [f"{host}:activity:{aid}"]
        if pid:
            node_sets.append(f"{host}:project:{pid}")
        documents.append({
            "id": f"kimai_{host}_activity_{aid}",
            "type": "activity",
            "entity_id": aid,
            "text": doc_text,
            "hash": content_hash,
            "node_sets": node_sets,
            "metadata": {
                "source": DOCUMENT_SOURCE_ATTR,
                "entity": "activity",
                "activity_id": aid,
                "project_id": pid,
                "name": act.get("name"),
            },
        })

    # 4. Timesheets
    timesheets = data.get("timesheets", [])
    for ts in timesheets:
        tid = ts.get("id")
        end = ts.get("end")

        # Exclude running timers with no end time from searchable text
        if not end:
            continue

        doc_text = format_timesheet_document(ts, base_url=base_url, include_rates=include_rates)
        content_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()

        # Build node set links
        node_sets = [f"{host}:timesheet:{tid}"]
        pid = ts.get("project", {}).get("id") if isinstance(ts.get("project"), dict) else ts.get("project")
        if pid:
            node_sets.append(f"{host}:project:{pid}")
        cid = ts.get("project", {}).get("customer", {}).get("id") if isinstance(ts.get("project"), dict) else None
        if cid:
            node_sets.append(f"{host}:customer:{cid}")
        aid = ts.get("activity", {}).get("id") if isinstance(ts.get("activity"), dict) else ts.get("activity")
        if aid:
            node_sets.append(f"{host}:activity:{aid}")
        uid = ts.get("user", {}).get("id") if isinstance(ts.get("user"), dict) else ts.get("user")
        if uid:
            node_sets.append(f"{host}:user:{uid}")

        documents.append({
            "id": f"kimai_{host}_timesheet_{tid}",
            "type": "timesheet",
            "entity_id": tid,
            "text": doc_text,
            "hash": content_hash,
            "node_sets": node_sets,
            "metadata": {
                "source": DOCUMENT_SOURCE_ATTR,
                "entity": "timesheet",
                "timesheet_id": tid,
                "project_id": pid,
                "user_id": uid,
                "billable": ts.get("billable", True),
                "duration": ts.get("duration", 0),
            },
        })

    return documents


def kimai_source(
    base_url: str = "",
    api_token: str = "",
    since: Optional[str] = None,
    include_rates: bool = False,
    snapshot_data: Optional[Union[Dict[str, Any], str, Path]] = None,
) -> Any:
    """Cognee DLT source for indexing Kimai time tracking records into agent memory."""
    # Pre-validate file path existence before importing optional dependencies
    if isinstance(snapshot_data, (str, Path)):
        p = Path(snapshot_data)
        if not p.exists():
            raise FileNotFoundError(f"Kimai snapshot file not found: {p}")
        with open(p, "r", encoding="utf-8") as f:
            raw_payload = json.load(f)
    elif isinstance(snapshot_data, dict):
        raw_payload = snapshot_data
    elif base_url and api_token:
        # Live REST API execution
        import requests

        clean_base = base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "User-Agent": "Cognee-Kimai-Connector/0.1.0",
        }

        # 1. Fetch Customers
        cust_res = requests.get(f"{clean_base}/api/customers?visible=3", headers=headers, timeout=30)
        cust_res.raise_for_status()
        customers = cust_res.json()

        # 2. Fetch Projects
        proj_res = requests.get(f"{clean_base}/api/projects?visible=3&ignoreDates=1", headers=headers, timeout=30)
        proj_res.raise_for_status()
        projects = proj_res.json()

        # 3. Fetch Activities
        act_res = requests.get(f"{clean_base}/api/activities?visible=3", headers=headers, timeout=30)
        act_res.raise_for_status()
        activities = act_res.json()

        # 4. Fetch Timesheets (paged)
        timesheet_params = {"user": "all", "full": "true", "size": "500", "page": "1"}
        if since:
            timesheet_params["begin"] = since

        timesheets: List[Dict[str, Any]] = []
        page = 1
        while True:
            timesheet_params["page"] = str(page)
            ts_res = requests.get(f"{clean_base}/api/timesheets", headers=headers, params=timesheet_params, timeout=45)
            ts_res.raise_for_status()
            batch = ts_res.json()
            if not batch:
                break
            timesheets.extend(batch)
            if len(batch) < 500:
                break
            page += 1

        raw_payload = {
            "customers": customers,
            "projects": projects,
            "activities": activities,
            "timesheets": timesheets,
        }
    else:
        raise ValueError("Must provide either 'snapshot_data' or both 'base_url' and 'api_token'.")

    parsed_docs = parse_kimai_records(raw_payload, base_url=base_url, include_rates=include_rates)

    try:
        import dlt
    except ImportError as e:
        raise ImportError(
            "The Kimai connector requires dlt: pip install 'cognee-community-connector-kimai' or pip install 'dlt[sqlalchemy]'."
        ) from e

    @dlt.source(name="kimai")
    def _source() -> Generator[Any, None, None]:
        @dlt.resource(name="records", write_disposition="replace")
        def _records() -> Generator[Dict[str, Any], None, None]:
            for doc in parsed_docs:
                yield {
                    "id": doc["id"],
                    "text": doc["text"],
                    "cognee_document_source": DOCUMENT_SOURCE_ATTR,
                    "cognee_node_set": doc.get("node_sets", []),
                    "metadata": doc.get("metadata", {}),
                }

        yield _records()

    return _source()
