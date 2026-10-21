"""Public lazy dlt factory for scoped Jira Cloud document snapshots."""

import hashlib
import json
import re

from .snapshot import build_snapshot
from .transport import Transport, site_root


def has_ordering_clause(jql: str) -> bool:
    """Find ORDER BY outside JQL's quoted values/identifiers and escapes."""
    outside, quote, escaped = [], None, False
    for char in jql:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            outside.append(" ")
        else:
            outside.append(char)
    if quote is not None:
        raise ValueError("jql contains an unterminated quoted value")
    return bool(re.search(r"\border\s+by\b", "".join(outside), re.I))


def jira_source(
    site_url: str,
    email: str,
    api_token: str,
    projects,
    *,
    resource_name: str,
    missing_policy: str,
    jql: str | None = None,
    token_mode: str = "unscoped",
    client=None,
    confirmed_removed=(),
    force_refresh: bool = False,
    page_size: int = 50,
    max_pages: int = 200,
    max_issues: int = 1000,
    max_attachment_bytes: int = 2 * 1024 * 1024,
    max_snapshot_bytes: int = 32 * 1024 * 1024,
):
    """Build a source without network I/O. Use foreground, unlimited-row replace.

    ``resource_name`` identifies one persistent corpus; keep it stable when changing
    selection, and use different names for independent corpora. Choose an explicit
    removal policy: mirror_visibility automatically mirrors a completely read visible
    scope; require_confirmation preserves ambiguous missing records until acknowledged.
    Client injection must implement json, attachment and site_url.
    """
    import dlt
    from cognee.shared.logging_utils import get_logger
    from cognee.tasks.ingestion import dlt_utils

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Jira requires Cognee document sync version >= 1")
    site_url = site_root(site_url)
    if not isinstance(email, str) or not email.strip() or "@" not in email:
        raise ValueError("An Atlassian account email is required")
    if not isinstance(api_token, str) or not api_token.strip():
        raise ValueError("An unscoped API token is required")
    if token_mode != "unscoped":
        raise ValueError("Only unscoped site-route tokens are supported")
    if not isinstance(resource_name, str) or not re.fullmatch(
        r"[a-z][a-z0-9_]{0,62}", resource_name
    ):
        raise ValueError("resource_name must be a lowercase identifier, 1..63 characters")
    if isinstance(projects, (str, bytes)):
        raise ValueError("projects must be an explicit sequence of keys or numeric IDs")
    projects = tuple(sorted(set(projects)))
    if not projects or any(
        not isinstance(x, str) or not re.fullmatch(r"(?:[A-Z][A-Z0-9_]*|[1-9][0-9]{0,18})", x)
        for x in projects
    ):
        raise ValueError("Select at least one valid project key or numeric ID")
    if jql is not None and (
        not isinstance(jql, str) or not jql.strip() or len(jql) > 1500 or has_ordering_clause(jql)
    ):
        raise ValueError("jql must be a nonempty filter up to 1500 characters without ORDER BY")
    if len(projects) > 20:
        raise ValueError("At most 20 selected projects are supported")
    query = "project in (" + ",".join(f'"{x}"' for x in projects) + ")"
    if jql:
        query += f" AND ({jql})"
    if missing_policy not in {"require_confirmation", "mirror_visibility"}:
        raise ValueError("Unknown missing_policy")
    if type(force_refresh) is not bool:
        raise ValueError("force_refresh must be boolean")
    for value, lower, upper in [
        (page_size, 1, 100),
        (max_pages, 1, 1000),
        (max_issues, 1, 10_000),
        (max_attachment_bytes, 1, 16 * 1024 * 1024),
        (max_snapshot_bytes, 1024, 128 * 1024 * 1024),
    ]:
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError("Invalid Jira extraction limit")
    if isinstance(confirmed_removed, (str, bytes)):
        raise ValueError("confirmed_removed must be a sequence of document IDs")
    confirmed_removed = tuple(confirmed_removed)
    if any(
        not isinstance(x, str)
        or not re.fullmatch(
            r"issue:[1-9][0-9]{0,18}(?::(?:comment|attachment):[1-9][0-9]{0,18})?", x
        )
        for x in confirmed_removed
    ):
        raise ValueError("Invalid confirmed_removed document ID")
    namespace = hashlib.sha256(
        json.dumps([site_url, email.strip().lower(), resource_name], separators=(",", ":")).encode()
    ).hexdigest()[:32]
    name = f"jira_{namespace}"
    stats = {
        "extractions": 0,
        "selected": 0,
        "fetched": 0,
        "reused": 0,
        "documents": 0,
        "failed": 0,
    }
    logger = get_logger("jira_connector")

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="replace",
        columns={key: {"data_type": "text"} for key in ("id", "title", "content", "url")},
    )
    def documents():
        stats.update({key: 0 for key in stats})
        stats["extractions"] = 1
        reader = client or Transport(site_url, email, api_token)
        try:
            if reader.site_url != site_url:
                raise ValueError("Injected Jira client does not match the configured site")
            state = dlt.current.source_state().setdefault("jira_snapshot", {})
            rows, next_state = build_snapshot(
                reader,
                state,
                query=query,
                projects=projects,
                page_size=page_size,
                max_pages=max_pages,
                max_issues=max_issues,
                max_attachment_bytes=max_attachment_bytes,
                max_snapshot_bytes=max_snapshot_bytes,
                force_refresh=force_refresh,
                missing_policy=missing_policy,
                confirmed_removed=confirmed_removed,
                stats=stats,
            )
            yield rows if rows else dlt.mark.materialize_table_schema()
            state.clear()
            state.update(next_state)
            logger.info(
                "Jira prepared %d documents; fetched %d issues, reused %d. "
                "Downstream ingestion/cleanup is not verified by these counts.",
                stats["documents"],
                stats["fetched"],
                stats["reused"],
            )
        except Exception:
            stats["failed"] += 1
            logger.warning("Jira extraction failed; complete snapshot withheld")
            raise
        finally:
            if client is None:
                reader.close()

    @dlt.source(name=name, max_table_nesting=0)
    def source():
        return documents

    result = source()
    result.cognee_sync_stats = stats
    setattr(result, dlt_utils.DOCUMENT_SOURCE_ATTR, "jira")
    setattr(result, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    return result
