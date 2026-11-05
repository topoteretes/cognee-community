"""Backlog (Nulab) Data-Source Connector for Cognee.

Table of Contents:
------------------
1. Constants & Data Structures
   - DOCUMENT_SOURCE_ATTR
   - BacklogRateLimitState (@dataclass)
   - BacklogComment (@dataclass)
   - BacklogIssueRecord (@dataclass)
   - BacklogDocumentPage (@dataclass)
2. Security, Privacy & Rate-Limit Pure Functions
   - redact_api_key
   - validate_space_url
   - sanitize_user_display_name
   - parse_rate_limit_headers
3. Issue & Document Parsers
   - parse_backlog_issue
   - parse_backlog_document
4. Incremental Sync, Offset Pagination & Tombstone Engine
   - sync_backlog_projects
   - backlog_source
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import re
from typing import Any
import urllib.parse

DOCUMENT_SOURCE_ATTR: str = "cognee_source"
MAX_PAGE_COUNT: int = 100
DEFAULT_RATE_LIMIT_LOW_WATERMARK: int = 5


# ---------------------------------------------------------------------------
# 1. Constants & Data Structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BacklogRateLimitState:
    """Represents parsed Nulab Backlog per-minute rate limit headers."""

    limit: int
    remaining: int
    reset_epoch: int

    def should_throttle(self, low_watermark: int = DEFAULT_RATE_LIMIT_LOW_WATERMARK) -> bool:
        """Return True when remaining requests drop to or below the low watermark."""
        return self.remaining <= low_watermark

    def compute_backoff_seconds(self, status_code: int, now_epoch: float) -> float:
        """Compute wait duration in seconds for 429 responses or low-remaining throttling."""
        if status_code == 429:
            return max(1.0, float(self.reset_epoch) - float(now_epoch))
        if self.should_throttle():
            wait_window = max(0.5, float(self.reset_epoch) - float(now_epoch))
            return min(2.0, wait_window / max(1, self.remaining + 1))
        return 0.0


@dataclass(frozen=True)
class BacklogComment:
    """Represents a single ordered comment on a Backlog issue (display name only, no email)."""

    comment_id: int
    author_name: str
    content: str
    created_at: str


@dataclass(frozen=True)
class BacklogIssueRecord:
    """Normalized Backlog issue with ordered comments for Cognee document graph ingestion."""

    issue_id: int
    issue_key: str
    project_key: str
    summary: str
    description: str
    issue_type: str
    status: str
    priority: str
    assignee_name: str
    milestones: list[str]
    updated_at: str
    comments: list[BacklogComment] = field(default_factory=list)
    is_deleted: bool = False

    def to_document_row(self) -> dict[str, Any]:
        """Serialize issue record into a Cognee dlt merge row with _deleted tombstone support."""
        comments_block = "\n".join(
            f"[{c.created_at}] {c.author_name}: {c.content}" for c in self.comments if c.content
        )
        milestones_str = ", ".join(self.milestones) if self.milestones else "None"
        assignee_label = self.assignee_name or "Unassigned"
        text_body = (
            f"Backlog Issue {self.issue_key}: {self.summary}\n"
            f"Type: {self.issue_type} | Status: {self.status} | Priority: {self.priority}\n"
            f"Assignee: {assignee_label} | Milestones: {milestones_str}\n\n"
            f"{self.description}\n"
        )
        if comments_block:
            text_body += f"\nComments:\n{comments_block}\n"

        return {
            "id": f"backlog:issue:{self.issue_key}",
            "issue_id": self.issue_id,
            "issue_key": self.issue_key,
            "project_key": self.project_key,
            "summary": self.summary,
            "status": self.status,
            "priority": self.priority,
            "assignee_name": self.assignee_name,
            "updated_at": self.updated_at,
            "text": text_body.strip(),
            "cognee_node_set": ["backlog", f"backlog_project:{self.project_key}"],
            "_deleted": self.is_deleted,
        }


@dataclass(frozen=True)
class BacklogDocumentPage:
    """Represents a Backlog Document page (`/api/v2/documents`)."""

    document_id: str
    project_key: str
    title: str
    plain_content: str
    updated_at: str
    is_deleted: bool = False

    def to_document_row(self) -> dict[str, Any]:
        """Serialize Backlog document page into a Cognee dlt merge row."""
        doc_text = f"Backlog Document [{self.project_key}] {self.title}\n\n{self.plain_content}"
        node_set = ["backlog", f"backlog_project:{self.project_key}", "backlog_documents"]
        return {
            "id": f"backlog:doc:{self.project_key}:{self.document_id}",
            "document_id": self.document_id,
            "project_key": self.project_key,
            "title": self.title,
            "updated_at": self.updated_at,
            "text": doc_text.strip(),
            "cognee_node_set": node_set,
            "_deleted": self.is_deleted,
        }


# ---------------------------------------------------------------------------
# 2. Security, Privacy & Rate-Limit Pure Functions
# ---------------------------------------------------------------------------


def redact_api_key(text: str, api_key: str = "") -> str:
    """Redact Backlog `apiKey` query parameters and raw API key tokens from logs, URLs, and docs."""
    if not text:
        return ""
    redacted = re.sub(r"([?&]apiKey=)[^&\s\"']+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    if api_key and len(api_key.strip()) >= 4:
        redacted = redacted.replace(api_key.strip(), "[REDACTED]")
    return redacted


def validate_space_url(space_url: str) -> str:
    """Validate and normalize a configured Backlog space URL (`backlog.com` or `backlog.jp`)."""
    cleaned = space_url.strip().rstrip("/")
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"Backlog space_url must be an explicit HTTPS URL, got: {space_url!r}")
    host = parsed.netloc.lower()
    valid_suffixes = (".backlog.com", ".backlog.jp", ".backlogtool.com")
    if not host.endswith(valid_suffixes):
        raise ValueError(
            f"Backlog space_url host '{host}' must be a valid Backlog domain "
            "(.backlog.com or .backlog.jp)"
        )
    return f"https://{host}"


def sanitize_user_display_name(user_obj: dict[str, Any] | None) -> str:
    """Extract only the user's display name, stripping email addresses for PII safety."""
    if not isinstance(user_obj, dict):
        return ""
    name = str(user_obj.get("name") or "").strip()
    # Never fall back to mailAddress; strip any accidental inline email address
    name = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL_REDACTED]", name)
    return name


def parse_rate_limit_headers(headers: dict[str, str]) -> BacklogRateLimitState:
    """Parse `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers."""
    norm = {k.lower(): v for k, v in headers.items()}
    limit = int(norm.get("x-ratelimit-limit", "60") or 60)
    remaining = int(norm.get("x-ratelimit-remaining", "60") or 60)
    reset_epoch = int(norm.get("x-ratelimit-reset", "0") or 0)
    return BacklogRateLimitState(limit=limit, remaining=remaining, reset_epoch=reset_epoch)


# ---------------------------------------------------------------------------
# 3. Issue & Document Parsers
# ---------------------------------------------------------------------------


def parse_backlog_issue(
    raw_issue: dict[str, Any],
    raw_comments: list[dict[str, Any]] | None = None,
    api_key: str = "",
) -> BacklogIssueRecord:
    """Parse a raw Backlog `/api/v2/issues` JSON object and its ordered comments."""
    issue_id = int(raw_issue.get("id", 0))
    issue_key = str(raw_issue.get("issueKey", "")).strip()
    fallback_proj = str(raw_issue.get("projectId", "PROJECT"))
    project_key = issue_key.split("-")[0] if "-" in issue_key else fallback_proj
    summary = redact_api_key(str(raw_issue.get("summary") or "").strip(), api_key)
    description = redact_api_key(str(raw_issue.get("description") or "").strip(), api_key)

    issue_type_obj = raw_issue.get("issueType") or {}
    issue_type = (
        str(issue_type_obj.get("name", "Task")) if isinstance(issue_type_obj, dict) else "Task"
    )

    status_obj = raw_issue.get("status") or {}
    status = str(status_obj.get("name", "Open")) if isinstance(status_obj, dict) else "Open"

    priority_obj = raw_issue.get("priority") or {}
    priority = (
        str(priority_obj.get("name", "Normal")) if isinstance(priority_obj, dict) else "Normal"
    )

    assignee_name = sanitize_user_display_name(raw_issue.get("assignee"))

    milestones_raw = raw_issue.get("milestone") or []
    milestones = [
        str(m.get("name", "")).strip()
        for m in milestones_raw
        if isinstance(m, dict) and m.get("name")
    ]
    updated_at = str(raw_issue.get("updated") or raw_issue.get("created") or "").strip()

    comments: list[BacklogComment] = []
    if raw_comments:
        sorted_raw = sorted(
            [c for c in raw_comments if isinstance(c, dict)],
            key=lambda c: (str(c.get("created") or ""), int(c.get("id", 0))),
        )
        for c in sorted_raw:
            content_clean = redact_api_key(str(c.get("content") or "").strip(), api_key)
            if not content_clean:
                continue
            author_name = sanitize_user_display_name(c.get("createdUser"))
            comments.append(
                BacklogComment(
                    comment_id=int(c.get("id", 0)),
                    author_name=author_name or "User",
                    content=content_clean,
                    created_at=str(c.get("created") or "").strip(),
                )
            )

    return BacklogIssueRecord(
        issue_id=issue_id,
        issue_key=issue_key,
        project_key=project_key,
        summary=summary,
        description=description,
        issue_type=issue_type,
        status=status,
        priority=priority,
        assignee_name=assignee_name,
        milestones=milestones,
        updated_at=updated_at,
        comments=comments,
        is_deleted=False,
    )


def parse_backlog_document(
    raw_doc: dict[str, Any],
    project_key: str,
    api_key: str = "",
) -> BacklogDocumentPage:
    """Parse a raw Backlog `/api/v2/documents` JSON object into a BacklogDocumentPage."""
    doc_id = str(raw_doc.get("id") or "").strip()
    title = redact_api_key(str(raw_doc.get("title") or "Untitled").strip(), api_key)
    plain = redact_api_key(str(raw_doc.get("plain") or raw_doc.get("json") or "").strip(), api_key)
    updated_at = str(raw_doc.get("updated") or raw_doc.get("created") or "").strip()
    return BacklogDocumentPage(
        document_id=doc_id,
        project_key=project_key,
        title=title,
        plain_content=plain,
        updated_at=updated_at,
        is_deleted=False,
    )


# ---------------------------------------------------------------------------
# 4. Incremental Sync, Offset Pagination & Tombstone Engine
# ---------------------------------------------------------------------------


def sync_backlog_projects(
    space_url: str,
    api_key: str,
    fetch_json_fn: Callable[[str, dict[str, Any]], Any],
    project_keys: list[str] | None = None,
    state_dict: dict[str, Any] | None = None,
    include_documents: bool = False,
    page_size: int = MAX_PAGE_COUNT,
) -> list[dict[str, Any]]:
    """Synchronize Backlog issues (with comments), optional documents, and `_deleted` tombstones.

    - Paginates via `count` (max 100) + `offset`.
    - Incremental sync uses per-project `updatedSince` with `sort=updated&order=asc`.
    - Re-fetches comments ONLY for issues returned by the incremental delta.
    - Forget-on-delete compares live project issue keys against `ingested_keys` in state;
      if the live key sweep raises an error, aborts BEFORE emitting any `_deleted` tombstones.
    """
    base_url = validate_space_url(space_url)
    if not api_key or not api_key.strip():
        raise ValueError("Backlog api_key must not be empty.")

    state = state_dict if state_dict is not None else {}
    cursors: dict[str, str] = state.setdefault("cursors", {})
    ingested_keys_by_project: dict[str, dict[int, str]] = state.setdefault("ingested_issues", {})

    # 1. Resolve projects (`GET /api/v2/projects`)
    try:
        all_projects = fetch_json_fn(f"{base_url}/api/v2/projects", {"apiKey": api_key})
    except Exception as exc:
        safe_msg = redact_api_key(str(exc), api_key)
        raise RuntimeError(f"Failed to list Backlog projects: {safe_msg}") from None

    selected_projects: list[dict[str, Any]] = []
    wanted_keys = {k.upper().strip() for k in project_keys} if project_keys else None
    for p in all_projects if isinstance(all_projects, list) else []:
        if not isinstance(p, dict):
            continue
        p_key = str(p.get("projectKey", "")).upper().strip()
        if wanted_keys is None or p_key in wanted_keys:
            selected_projects.append(p)

    emitted_rows: list[dict[str, Any]] = []
    effective_count = max(1, min(MAX_PAGE_COUNT, page_size))

    for proj in selected_projects:
        proj_id = int(proj["id"])
        proj_key = str(proj["projectKey"]).upper().strip()
        last_cursor = cursors.get(proj_key)
        known_issues: dict[int, str] = ingested_keys_by_project.setdefault(proj_key, {})

        # 2. Paginate incremental changed issues (`count` + `offset`, `sort=updated&order=asc`)
        offset = 0
        latest_updated = last_cursor or ""
        while True:
            params: dict[str, Any] = {
                "apiKey": api_key,
                "projectId[]": proj_id,
                "sort": "updated",
                "order": "asc",
                "count": effective_count,
                "offset": offset,
            }
            if last_cursor:
                params["updatedSince"] = last_cursor

            try:
                batch = fetch_json_fn(f"{base_url}/api/v2/issues", params)
            except Exception as exc:
                safe_msg = redact_api_key(str(exc), api_key)
                err_msg = f"Failed to fetch Backlog issues for {proj_key}: {safe_msg}"
                raise RuntimeError(err_msg) from None

            if not isinstance(batch, list) or not batch:
                break

            for raw_issue in batch:
                issue_id = int(raw_issue.get("id", 0))
                issue_key = str(raw_issue.get("issueKey", f"{proj_key}-{issue_id}"))
                # Fetch comments ONLY for issues returned in the incremental delta
                try:
                    raw_comments = fetch_json_fn(
                        f"{base_url}/api/v2/issues/{issue_key}/comments",
                        {"apiKey": api_key, "count": MAX_PAGE_COUNT, "order": "asc"},
                    )
                except Exception as exc:
                    safe_msg = redact_api_key(str(exc), api_key)
                    err_msg = f"Failed to fetch comments for {issue_key}: {safe_msg}"
                    raise RuntimeError(err_msg) from None

                record = parse_backlog_issue(
                    raw_issue=raw_issue,
                    raw_comments=raw_comments if isinstance(raw_comments, list) else [],
                    api_key=api_key,
                )
                row = record.to_document_row()
                emitted_rows.append(row)
                known_issues[issue_id] = issue_key
                if record.updated_at > latest_updated:
                    latest_updated = record.updated_at

            if len(batch) < effective_count:
                break
            offset += effective_count

        # Save cursor ONLY after successful issue + comment load
        if latest_updated:
            cursors[proj_key] = latest_updated

        # 3. Forget-on-delete sweep: list all current issue IDs in the project (no comments fetched)
        live_ids: set[int] = set()
        sweep_offset = 0
        try:
            while True:
                live_batch = fetch_json_fn(
                    f"{base_url}/api/v2/issues",
                    {
                        "apiKey": api_key,
                        "projectId[]": proj_id,
                        "count": effective_count,
                        "offset": sweep_offset,
                    },
                )
                if not isinstance(live_batch, list) or not live_batch:
                    break
                for item in live_batch:
                    if isinstance(item, dict) and "id" in item:
                        live_ids.add(int(item["id"]))
                if len(live_batch) < effective_count:
                    break
                sweep_offset += effective_count
        except Exception as exc:
            safe_msg = redact_api_key(str(exc), api_key)
            raise RuntimeError(
                f"Aborting forget-on-delete sweep for {proj_key} to protect live data: {safe_msg}"
            ) from None

        for old_id in list(known_issues.keys()):
            if old_id not in live_ids:
                old_key = known_issues.pop(old_id)
                emitted_rows.append({
                    "id": f"backlog:issue:{old_key}",
                    "issue_id": old_id,
                    "issue_key": old_key,
                    "project_key": proj_key,
                    "_deleted": True,
                })

        # 4. Optional Document pages (`/api/v2/documents`)
        if include_documents:
            doc_offset = 0
            while True:
                doc_batch = fetch_json_fn(
                    f"{base_url}/api/v2/documents",
                    {
                        "apiKey": api_key,
                        "projectId[]": proj_id,
                        "count": effective_count,
                        "offset": doc_offset,
                    },
                )
                if not isinstance(doc_batch, list) or not doc_batch:
                    break
                for raw_doc in doc_batch:
                    if isinstance(raw_doc, dict):
                        doc_page = parse_backlog_document(raw_doc, proj_key, api_key=api_key)
                        emitted_rows.append(doc_page.to_document_row())
                if len(doc_batch) < effective_count:
                    break
                doc_offset += effective_count

    return emitted_rows


def backlog_source(
    space_url: str,
    api_key: str,
    project_keys: list[str] | None = None,
    include_documents: bool = False,
    fetch_json_fn: Callable[[str, dict[str, Any]], Any] | None = None,
    state_dict: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Pulled dlt document source for Nulab Backlog issues, comments, and documents."""
    if fetch_json_fn is None:
        raise ValueError("A fetch_json_fn callable or HTTP transport must be provided.")
    rows = sync_backlog_projects(
        space_url=space_url,
        api_key=api_key,
        fetch_json_fn=fetch_json_fn,
        project_keys=project_keys,
        state_dict=state_dict,
        include_documents=include_documents,
    )
    yield from rows


setattr(backlog_source, DOCUMENT_SOURCE_ATTR, True)
