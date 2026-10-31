"""Complete materialized snapshots; no rows escape before all reads succeed."""

import copy
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ._verified import get_paginated_data
from .attachments import TEXT_TYPES, extract_text
from .render import attachment_row, comment_row, issue_row
from .transport import numeric_id

CONTEXT = "updated,summary,project,status,assignee,reporter,priority,issuetype,parent,labels"
POLICY_VERSION = 2


class MissingDocuments(ValueError):
    """Absence can be a deletion or reduced visibility; explicit policy required."""

    def __init__(self, identities):
        self.identities = tuple(sorted(identities))
        super().__init__(
            "Jira documents disappeared; verify access and acknowledge these IDs with "
            "confirmed_removed, or explicitly choose mirror_visibility: "
            + ", ".join(self.identities)
        )


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Jira updated timestamp is missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid Jira updated timestamp") from None
    if result.tzinfo is None:
        raise ValueError("Jira updated timestamp must include an offset")
    return result.astimezone(UTC)


def window(query, cutoff, now, timezone):
    zone = ZoneInfo(timezone)
    # Jira date literals have minute precision in the requesting user's timezone.
    lower = (timestamp(cutoff) - timedelta(minutes=5)).astimezone(zone)
    upper = (now + timedelta(minutes=1)).astimezone(zone)
    return (
        f'({query}) AND updated >= "{lower:%Y-%m-%d %H:%M}" '
        f'AND updated < "{upper:%Y-%m-%d %H:%M}" ORDER BY updated ASC, key ASC'
    )


def search(reader, query, fields, page_size, max_pages, reconcile=(), max_records=1000):
    params = {
        "jql": query,
        "fields": fields,
        "failFast": "true",
        "includeArchivedProjects": "false",
    }
    if reconcile:
        params["reconcileIssues"] = [int(x) for x in reconcile]
    result = {}
    size = 0
    for page in get_paginated_data(
        reader, page_size, "rest/api/3/search/jql", "issues", params, True, max_pages
    ):
        for issue in page:
            identity = numeric_id(issue.get("id"))
            if identity in result:
                raise ValueError("Jira repeated an issue during pagination; retry")
            validate_issue(issue, fields == "*all")
            result[identity] = issue
            size += len(json.dumps(issue, ensure_ascii=False).encode())
            if len(result) > max_records or size > 32 * 1024 * 1024:
                raise ValueError("Jira search exceeds the materialized inventory budget")
    return result


def validate_issue(issue, full=False):
    fields = issue.get("fields")
    if not isinstance(fields, dict) or not isinstance(issue.get("key"), str):
        raise ValueError("Invalid Jira issue")
    if not issue["key"] or not isinstance(fields.get("summary"), str):
        raise ValueError("Missing Jira summary or key")
    timestamp(fields.get("updated"))
    if not isinstance(fields.get("project"), dict):
        raise ValueError("Missing Jira project")
    numeric_id(fields["project"].get("id"))
    if full and "description" not in fields:
        raise ValueError("Jira omitted description; snapshot aborted")


def inventory(reader, query, previous_ids, page_size, max_pages, max_issues):
    result = search(
        reader,
        query + " ORDER BY updated ASC, key ASC",
        CONTEXT,
        page_size,
        max_pages,
        max_records=max_issues,
    )
    # Reconcile both retained IDs and general-search discoveries. Otherwise a
    # removed ID could return from a stale index after leaving retained state.
    # This still cannot discover unknown IDs absent from general search.
    known = sorted(set(previous_ids) | set(result), key=int)
    for offset in range(0, len(known), 50):
        batch = known[offset : offset + 50]
        scoped = f"({query}) AND id in ({','.join(batch)}) ORDER BY updated ASC, key ASC"
        reconciled = search(reader, scoped, CONTEXT, page_size, max_pages, batch)
        if not set(reconciled).issubset(batch):
            raise ValueError("Jira reconciliation escaped its requested IDs")
        # The complete reconciled query is authoritative for this known-ID batch,
        # including exclusions from a stale general search result.
        for identity in batch:
            result.pop(identity, None)
        result.update(reconciled)
        if len(result) > max_issues:
            raise ValueError("Jira issue count exceeds snapshot budget")
    if len(result) > max_issues:
        raise ValueError("Jira issue count exceeds snapshot budget")
    return result


def comments(reader, identity, page_size, max_pages):
    result = []
    for page in get_paginated_data(
        reader,
        page_size,
        f"rest/api/3/issue/{identity}/comment",
        "comments",
        {"orderBy": "created"},
        False,
        max_pages,
    ):
        for item in page:
            numeric_id(item.get("id"))
            if "body" not in item:
                raise ValueError("Jira omitted comment body")
        result.extend(page)
        if len(result) > 20_000 or len(json.dumps(result).encode()) > 32 * 1024 * 1024:
            raise ValueError("Jira comments exceed snapshot budget")
    return result


def attachments(reader, identity):
    data = reader.json(f"rest/api/3/issue/{identity}", {"fields": "attachment"})
    if numeric_id(data.get("id")) != identity:
        raise ValueError("Jira returned a different issue")
    fields = data.get("fields")
    items = fields.get("attachment") if isinstance(fields, dict) else None
    if not isinstance(items, list):
        raise ValueError("Jira omitted attachment inventory")
    if len(items) > 20_000:
        raise ValueError("Jira attachment count exceeds snapshot budget")
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid Jira attachment")
        key = numeric_id(item.get("id"))
        if key in result or type(item.get("size")) is not int or item["size"] < 0:
            raise ValueError("Invalid Jira attachment inventory")
        if not all(isinstance(item.get(x), str) and item[x] for x in ("filename", "mimeType")):
            raise ValueError("Missing Jira attachment metadata")
        # Do not cache provider content/self URLs, thumbnails or arbitrary metadata.
        result[key] = {x: item.get(x) for x in ("id", "filename", "mimeType", "size", "created")}
    return result


def signature(items):
    return {key: {"key": issue["key"], "fields": issue["fields"]} for key, issue in items.items()}


def build_snapshot(
    reader,
    previous,
    *,
    query,
    projects,
    page_size,
    max_pages,
    max_issues,
    max_attachment_bytes,
    max_snapshot_bytes,
    force_refresh,
    missing_policy,
    confirmed_removed,
    now=None,
    stats=None,
):
    stats = stats if stats is not None else {}
    now = now or datetime.now(UTC)
    user = reader.json("rest/api/3/myself")
    owner, timezone = user.get("accountId"), user.get("timeZone")
    if not isinstance(owner, str) or not owner or not isinstance(timezone, str):
        raise ValueError("Jira account and timezone must be verifiable")
    ZoneInfo(timezone)
    if previous.get("account_id", owner) != owner:
        raise ValueError("Jira account changed; use a separate resource_name")
    if previous and previous.get("version") != POLICY_VERSION:
        force_refresh = True
    cached = previous.get("issues", {})
    if not isinstance(cached, dict):
        raise ValueError("Invalid retained Jira state; restore or retire the corpus")
    current = inventory(reader, query, cached, page_size, max_pages, max_issues)
    selected = set(projects)
    for item in current.values():
        project = item["fields"]["project"]
        if project.get("key") not in selected and str(project.get("id")) not in selected:
            raise ValueError("Jira search escaped the selected projects")
    full = {}
    same_query = previous.get("query") == query
    if cached and same_query and not force_refresh and previous.get("timezone") == timezone:
        cutoff = previous.get("cutoff")
        full = search(
            reader,
            window(query, cutoff, now, timezone),
            "*all",
            page_size,
            max_pages,
            max_records=max_issues,
        )
    rows, retained = [], {}
    for identity, compact in sorted(current.items(), key=lambda pair: int(pair[0])):
        old = cached.get(identity, {})
        detail = full.get(identity)
        if (
            force_refresh
            or not old
            or not same_query
            or old.get("updated") != compact["fields"]["updated"]
        ):
            if detail is None:
                detail = reader.json(f"rest/api/3/issue/{identity}", {"fields": "*all"})
            if numeric_id(detail.get("id")) != identity:
                raise ValueError("Jira returned a different issue")
            validate_issue(detail, True)
            if detail["fields"]["updated"] != compact["fields"]["updated"]:
                raise ValueError("Issue changed during snapshot; retry")
            description = detail["fields"]["description"]
            stats["fetched"] = stats.get("fetched", 0) + 1
        else:
            description = old["description"]
            stats["reused"] = stats.get("reused", 0) + 1
        issue = copy.deepcopy(compact)
        issue["fields"]["description"] = description
        parent = issue_row(issue, reader.site_url)
        rows.append(parent)
        first_comments = comments(reader, identity, page_size, max_pages)
        rendered = [comment_row(item, parent) for item in first_comments]
        rows.extend(rendered)
        files = attachments(reader, identity)
        file_cache = {}
        for key, item in sorted(files.items()):
            mime = item["mimeType"].split(";", 1)[0].lower().strip()
            old_file = old.get("attachments", {}).get(key, {})
            extracted = False
            if item["size"] > max_attachment_bytes:
                text = "[Attachment omitted: exceeds configured byte limit]"
            elif mime not in TEXT_TYPES | {"application/pdf"}:
                text = "[Attachment omitted: unsupported MIME type; OCR is not supported]"
            elif (
                not force_refresh
                and old_file.get("metadata") == item
                and old_file.get("extracted") is True
            ):
                text = old_file["text"]
                extracted = True
            else:
                blob = reader.attachment(key, max_attachment_bytes)
                if len(blob) != item["size"]:
                    raise ValueError("Attachment size changed or content was truncated")
                text = extract_text(blob, mime)
                extracted = True
            file_cache[key] = {"metadata": item, "text": text, "extracted": extracted}
            rows.append(attachment_row(item, parent, text))
        # Detect common edits during offset pagination and attachment retrieval.
        # Two equal reads still cannot certify an atomic provider snapshot.
        if rendered != [
            comment_row(item, parent) for item in comments(reader, identity, page_size, max_pages)
        ] or files != attachments(reader, identity):
            raise ValueError("Jira child content changed during snapshot; retry")
        retained[identity] = {
            "updated": compact["fields"]["updated"],
            "description": description,
            "attachments": file_cache,
        }
        if len(rows) > 20_000:
            raise ValueError("Jira document count exceeds snapshot budget")
        if len(json.dumps([rows, retained], ensure_ascii=False).encode()) > max_snapshot_bytes:
            raise ValueError("Jira materialized snapshot exceeds its byte budget")
    if signature(current) != signature(
        inventory(reader, query, cached, page_size, max_pages, max_issues)
    ):
        raise ValueError("Jira inventory changed during snapshot; retry")
    final_user = reader.json("rest/api/3/myself")
    if (final_user.get("accountId"), final_user.get("timeZone")) != (owner, timezone):
        raise ValueError("Jira account or timezone changed during snapshot")
    ids = {row["id"] for row in rows}
    # Selection changes are explicit corpus replacement, not evidence of upstream deletion.
    missing = set(previous.get("document_ids", [])) - ids if same_query else set()
    if set(confirmed_removed) & ids:
        raise ValueError("Cannot confirm removal of a currently visible document")
    unresolved = missing - set(confirmed_removed)
    if unresolved and missing_policy == "require_confirmation":
        raise MissingDocuments(unresolved)
    next_state = {
        "version": POLICY_VERSION,
        "account_id": owner,
        "timezone": timezone,
        "query": query,
        "cutoff": now.isoformat(),
        "issues": retained,
        "document_ids": sorted(ids),
    }
    # Bound both accumulated staging and durable cached state before publishing any row.
    if len(json.dumps([rows, next_state], ensure_ascii=False).encode()) > max_snapshot_bytes:
        raise ValueError("Jira materialized snapshot exceeds its byte budget")
    stats.update(selected=len(current), documents=len(rows))
    return rows, next_state
