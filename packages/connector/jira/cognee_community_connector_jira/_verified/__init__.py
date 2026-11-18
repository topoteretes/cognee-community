"""Adapted dlt-hub verified Jira source (Apache-2.0).

Origin: sources/jira/__init__.py at 3957506893a7da821dbcc6acd51c7ca4475d1f53.
See VERIFIED_SOURCE.md for the explicit changes.
"""

from collections.abc import Iterator

import dlt


@dlt.source(max_table_nesting=0)
def jira_search(transport, page_size: int = 50):
    @dlt.resource(write_disposition="replace")
    def issues(
        jql_queries: list[str],
        fields: str = "*all",
        reconcile_ids=None,
        include_archived: bool = False,
        max_pages: int = 200,
    ):
        for jql in jql_queries:
            params = {
                "fields": fields,
                "jql": jql,
                "failFast": "true",
                "includeArchivedProjects": str(include_archived).lower(),
            }
            if reconcile_ids:
                params["reconcileIssues"] = [int(value) for value in reconcile_ids]
            yield from get_paginated_data(
                transport, page_size, "rest/api/3/search/jql", "issues", params, True, max_pages
            )

    return issues


def get_paginated_data(
    transport,
    page_size: int,
    api_path: str,
    data_path: str,
    params: dict | None = None,
    use_cursor_pagination: bool = False,
    max_pages: int = 200,
) -> Iterator[list[dict]]:
    """Verified source's page loop with complete-page and cursor validation."""
    params = dict(params or {}, maxResults=page_size)
    start_at = 0
    seen_tokens = set()
    expected_total = None
    seen_ids = set()
    if not use_cursor_pagination:
        params["startAt"] = 0
    for _ in range(max_pages):
        result = transport.json(api_path, params)
        if result.get("warnings", []) != []:
            raise ValueError("Jira search warnings prevent complete snapshot publication")
        page = result.get(data_path)
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise ValueError("Jira returned an invalid page; snapshot aborted")
        if use_cursor_pagination:
            token = result.get("nextPageToken")
            marker = result.get("isLast")
            if "isLast" in result and type(marker) is not bool:
                raise ValueError("Jira returned a malformed terminal marker")
            if token is not None and (not isinstance(token, str) or not token):
                raise ValueError("Jira returned an invalid continuation token")
            if result.get("isLast") is True:
                if token:
                    raise ValueError("Jira returned conflicting pagination markers")
                yield page
                return
            if not token:
                if result.get("isLast") is False:
                    raise ValueError("Jira returned an incomplete search cursor")
                yield page
                return
            if not isinstance(token, str) or token in seen_tokens:
                raise ValueError("Jira repeated or invalidated its search cursor")
            seen_tokens.add(token)
            yield page
            params["nextPageToken"] = token
        else:
            total = result.get("total")
            if (
                type(total) is not int
                or total < 0
                or type(result.get("startAt")) is not int
                or result.get("startAt") != start_at
            ):
                raise ValueError("Jira returned invalid comment pagination")
            if expected_total is not None and total != expected_total:
                raise ValueError("Comments changed during pagination; retry the snapshot")
            expected_total = total
            for row in page:
                identity = str(row.get("id", ""))
                if not identity.isdigit() or identity in seen_ids:
                    raise ValueError("Comment IDs missing or repeated; retry the snapshot")
                seen_ids.add(identity)
            if start_at + len(page) > total or (not page and start_at < total):
                raise ValueError("Jira returned a truncated comment page")
            yield page
            start_at += len(page)
            if start_at == total:
                return
            params["startAt"] = start_at
    raise ValueError("Jira pagination exceeded the configured page limit")
