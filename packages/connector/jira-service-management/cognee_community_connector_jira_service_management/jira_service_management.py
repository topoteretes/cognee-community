"""JSM Cloud request documents with incremental dlt merge and hard deletes."""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOURCE_NAME = "jira_service_management"
TABLE_NAME = "jira_service_management_requests"
API = "/rest/servicedeskapi"


def _base_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base_url must be an HTTPS Atlassian site URL without credentials")
    if parsed.query or parsed.fragment or parsed.path.strip("/"):
        raise ValueError("base_url must be the site root")
    return value.rstrip("/")


def _make_session(email, api_token):
    if not email or not api_token:
        raise ValueError("email and api_token are required (or inject a session)")
    client = requests.Session()
    client.auth = (email, api_token)
    client.headers.update({"Accept": "application/json"})
    client.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(
                total=4,
                backoff_factor=1,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods={"GET"},
                respect_retry_after_header=True,
            )
        ),
    )
    return client


def _get(session, base_url, path, params=None):
    response = session.get(base_url + path, params=params or {}, timeout=60, allow_redirects=False)
    if 300 <= response.status_code < 400:
        raise ValueError("Unexpected redirect from Atlassian API")
    response.raise_for_status()
    return response.json()


def _pages(session, base_url, path, **params):
    start = 0
    while True:
        page = _get(session, base_url, path, {**params, "start": start, "limit": 100})
        values = page["values"]
        if not isinstance(values, list) or not isinstance(page.get("isLastPage"), bool):
            raise ValueError("Incomplete JSM pagination response")
        yield from values
        if page["isLastPage"]:
            return
        if not values:
            raise ValueError("JSM pagination made no progress")
        start += len(values)


def list_service_desks(*, base_url, email=None, api_token=None, session=None):
    """List accessible desks so users can select their numeric IDs."""
    base_url = _base_url(base_url)
    client = session if session is not None else _make_session(email, api_token)
    try:
        return list(_pages(client, base_url, f"{API}/servicedesk"))
    finally:
        if session is None:
            client.close()


def _search(session, base_url, jql, fields):
    params = {"jql": jql + " ORDER BY updated ASC, key ASC", "fields": fields, "maxResults": 100}
    seen = set()
    while True:
        page = _get(session, base_url, "/rest/api/3/search/jql", params)
        yield from page["issues"]
        token = page.get("nextPageToken")
        if page.get("isLast") is True:
            return
        if not token or token in seen:
            raise ValueError("Incomplete or repeated Jira search pagination token")
        seen.add(token)
        params["nextPageToken"] = token


def _text(value):
    """Render plain text and Atlassian Document Format without HTML markup."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    if isinstance(value, dict):
        text = value.get("text", "") or _text(value.get("content", []))
        if value.get("type") in {"paragraph", "heading", "listItem", "hardBreak"}:
            text += "\n"
        return text
    return "" if value is None else str(value)


def _request_type_field(session, base_url):
    """Resolve the JSM field by type, independent of its translated/renamed label."""
    fields = _get(session, base_url, "/rest/api/3/field")
    matches = {
        field["id"]
        for field in fields
        if (field.get("schema") or {}).get("custom") == "com.atlassian.servicedesk:vp-origin"
    }
    if len(matches) != 1:
        raise ValueError("Expected exactly one accessible JSM request-type field")
    field_id = matches.pop()
    number = field_id.removeprefix("customfield_")
    if not field_id.startswith("customfield_") or not number.isascii() or not number.isdecimal():
        raise ValueError("Invalid JSM request-type custom field ID")
    return f"cf[{number}]"


def _document(
    session, base_url, issue_id, include_comments, include_internal_comments, include_sla
):
    request = _get(session, base_url, f"{API}/request/{issue_id}")
    fields = {
        field["fieldId"]: field.get("value") for field in request.get("requestFieldValues", [])
    }
    title = _text(request.get("summary")) or _text(fields.get("summary")) or request["issueKey"]
    lines = [title, f"Status: {request['currentStatus']['status']}"]
    for field in request.get("requestFieldValues", []):
        if field.get("fieldId") == "description":
            lines.append(_text(field.get("value")))
    if include_comments:
        comments = list(_pages(session, base_url, f"{API}/request/{issue_id}/comment"))
        comments.sort(key=lambda c: (c.get("created", {}).get("epochMillis", 0), str(c["id"])))
        for comment in comments:
            if comment.get("public") is not True and not include_internal_comments:
                continue
            visibility = "Public" if comment.get("public") is True else "Internal"
            author = comment.get("author", {}).get("displayName", "")
            lines.append(f"{visibility} comment by {author}: {_text(comment.get('body'))}")
    active_sla = False
    if include_sla:
        for sla in _pages(session, base_url, f"{API}/request/{issue_id}/sla"):
            active_sla = active_sla or bool(sla.get("ongoingCycle"))
            lines.append(_sla_text(sla))
    row = {
        "id": str(request["issueId"]),
        "title": title,
        "url": f"{base_url}/browse/{request['issueKey']}",
        "content": "\n\n".join(lines),
        "_deleted": False,
    }
    return row, active_sla


def _sla_text(sla):
    """Keep meaningful SLA transitions without ticking clocks or API metadata."""
    cycle = sla.get("ongoingCycle")
    if cycle:
        status = "paused" if cycle.get("paused") else "ongoing"
    elif sla.get("completedCycles"):
        cycle = max(
            sla["completedCycles"],
            key=lambda item: item.get("stopTime", {}).get("epochMillis", 0),
        )
        status = "completed"
    else:
        return f"{sla['name']}: not started"
    breached = cycle.get("breached")
    breach = (
        "breached"
        if breached is True
        else ("not breached" if breached is False else "breach status unknown")
    )
    return f"{sla['name']}: {status}, {breach}"


def sync_requests(
    session,
    base_url,
    state,
    *,
    service_desk_ids,
    include_comments=True,
    include_internal_comments=False,
    include_sla=True,
):
    """Yield changed documents and deletions; commit state only after a full successful run.

    A full ID sweep is authoritative for *visibility*, including removed access.
    Explicit desk validation and strict pagination prevent failed sweeps becoming deletes.
    The overlapping cursor tolerates search-index delay; hashes suppress repeated rows.
    """
    started = int(time.time() * 1000)
    projects = []
    for desk_id in service_desk_ids:
        desk = _get(session, base_url, f"{API}/servicedesk/{desk_id}")
        project_id = str(desk["projectId"])
        if not project_id.isdecimal():
            raise ValueError("Expected a numeric Jira project ID")
        projects.append(project_id)
    request_type_field = _request_type_field(session, base_url)
    scope = f"project IN ({','.join(projects)}) AND {request_type_field} IS NOT EMPTY"
    current = {str(issue["id"]) for issue in _search(session, base_url, scope, "id")}
    known = state.get("documents", {})
    since = state.get("cursor_ms")
    query = scope if since is None else f"{scope} AND updated >= {max(0, since - 300000)}"
    changed = {str(issue["id"]) for issue in _search(session, base_url, query, "updated")}
    # Newly visible/restored old requests must not be hidden by the cursor.
    candidates = (
        changed
        | (current - set(known))
        | {key for key, value in known.items() if value.get("active_sla")}
    )
    # Search is eventually consistent. Keep delta results even if the sweep missed them.
    current.update(changed)
    # A search-index omission alone is not a deletion. Probe missing requests
    # directly; auth/rate-limit/server errors abort the run, preserving state.
    for issue_id in set(known) - current:
        try:
            request = _get(session, base_url, f"{API}/request/{issue_id}")
        except requests.HTTPError as exc:
            if exc.response.status_code != 404:
                raise
        else:
            if str(request["serviceDeskId"]) in service_desk_ids:
                current.add(issue_id)
                candidates.add(issue_id)
    updated = {key: dict(value) for key, value in known.items() if key in current}
    for issue_id in sorted(candidates & current):
        row, active = _document(
            session, base_url, issue_id, include_comments, include_internal_comments, include_sla
        )
        digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        if digest != known.get(issue_id, {}).get("hash"):
            yield row
        updated[issue_id] = {"hash": digest, "active_sla": active}
    for issue_id in sorted(set(known) - current):
        yield {"id": issue_id, "_deleted": True}
    state.update(documents=updated, cursor_ms=started)


def jira_service_management_source(
    *,
    base_url,
    service_desk_ids,
    email=None,
    api_token=None,
    include_comments=True,
    include_internal_comments=False,
    include_sla=True,
    session=None,
):
    """Build a document-mode dlt source for ``cognee.remember``.

    Use a dedicated, stable dataset and pipeline for each account and selection.
    Internal comments are excluded unless explicitly enabled. JSM Cloud only.
    """
    import dlt

    base_url = _base_url(base_url)
    desks = sorted({str(value) for value in service_desk_ids})
    if not desks or any(not value.isdecimal() for value in desks):
        raise ValueError("service_desk_ids must contain numeric service desk IDs")
    if session is None and not (email and api_token):
        raise ValueError("email and api_token are required (or inject a session)")
    scope = [base_url, email, desks, include_comments, include_internal_comments, include_sla]
    scope_hash = hashlib.sha256(json.dumps(scope).encode()).hexdigest()

    @dlt.resource(
        name=TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def requests_resource():
        state = dlt.current.resource_state()
        if state.get("scope", scope_hash) != scope_hash:
            raise ValueError("Connection or selection changed: use a new dedicated dataset")
        client = session if session is not None else _make_session(email, api_token)
        try:
            yield from sync_requests(
                client,
                base_url,
                state,
                service_desk_ids=desks,
                include_comments=include_comments,
                include_internal_comments=include_internal_comments,
                include_sla=include_sla,
            )
            state["scope"] = scope_hash
        finally:
            if session is None:
                client.close()

    @dlt.source(name=SOURCE_NAME)
    def source():
        return requests_resource

    result = source()
    # Cognee's DOCUMENT_SOURCE_ATTR value; keep importing the connector lightweight.
    result.cognee_document_source = SOURCE_NAME
    return result
