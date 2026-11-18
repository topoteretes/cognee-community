"""DLT source for New Relic observability data.

Ingests alert policies, NRQL conditions, dashboards, and incidents/issues
via New Relic's NerdGraph GraphQL API (https://api.newrelic.com/graphql)
into Cognee memory.

Architecture & Document-Mode:
- Sets DOCUMENT_SOURCE_ATTR = "new_relic" and PIPELINE_SCOPE_ATTR = "new_relic".
- Sets write_disposition="merge" with primary_key="id" and
  columns={"_deleted": {"data_type": "bool", "hard_delete": True}}.
- Alert policies and dashboards form authoritative inventories: deleted items
  upstream trigger forget-on-delete tombstoning ({"id": ..., "_deleted": True})
  to remove orphaned nodes and vectors from memory.
- Incidents support incremental sync via time-window cursors.
- Supports US (default) and EU data centers, User API key authentication via the
  API-Key header, and robust retry logic on rate limits.
"""

import os
import time
from typing import Any, Generator, Iterable

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("new_relic_connector")

NEW_RELIC_SOURCE_NAME = "new_relic"
NEW_RELIC_TABLE_NAME = "new_relic_entities"

US_NERDGRAPH_URL = "https://api.newrelic.com/graphql"
EU_NERDGRAPH_URL = "https://api.eu.newrelic.com/graphql"

_MAX_RETRIES = 5
_INITIAL_RETRY_DELAY = 1.0

# GraphQL query for Alert Policies and NRQL Conditions
ALERT_POLICIES_QUERY = """
query GetAlertPolicies($accountId: Int!, $cursor: String) {
  actor {
    account(id: $accountId) {
      alerts {
        policiesSearch(cursor: $cursor) {
          policies {
            id
            name
            incidentPreference
          }
          nextCursor
        }
        nrqlConditionsSearch {
          nrqlConditions {
            id
            name
            policyId
            enabled
            description
            nrql {
              query
            }
            runbookUrl
            terms {
              operator
              priority
              threshold
              thresholdDuration
            }
          }
        }
      }
    }
  }
}
"""

# GraphQL query for Dashboards
DASHBOARDS_QUERY = """
query GetDashboards($cursor: String) {
  actor {
    entitySearch(query: "type = 'DASHBOARD'") {
      results(cursor: $cursor) {
        entities {
          ... on DashboardEntityOutline {
            guid
            name
            description
            accountId
            updatedAt
            tags {
              key
              values
            }
          }
        }
        nextCursor
      }
    }
  }
}
"""

# GraphQL query for Incidents / Issues
ISSUES_QUERY = """
query GetIssues($accountId: Int!, $cursor: String) {
  actor {
    account(id: $accountId) {
      aiIssues {
        issues(cursor: $cursor) {
          issues {
            issueId
            title
            priority
            state
            activatedAt
            closedAt
            sources
            mutingState
            conditionName
          }
          nextCursor
        }
      }
    }
  }
}
"""


class NewRelicAPIError(RuntimeError):
    """Raised on fatal NerdGraph GraphQL or HTTP API failures."""


def _is_transient(exc: Exception) -> bool:
    """Determine if an error is transient and retryable."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code in {429, 500, 502, 503, 504}
    return False


def _execute_nerdgraph(
    client: httpx.Client,
    base_url: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a GraphQL query against NerdGraph with exponential backoff retries."""
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.post(base_url, json=payload)
            response.raise_for_status()
            data = response.json()

            errors = data.get("errors")
            resp_data = data.get("data")

            # Partial error handling: log warnings when non-fatal warnings accompany data
            if errors and resp_data:
                logger.warning("NerdGraph returned partial GraphQL errors: %s", errors)
                return resp_data
            elif errors and not resp_data:
                raise NewRelicAPIError(f"NerdGraph GraphQL errors: {errors}")

            if resp_data is None:
                raise NewRelicAPIError(f"Empty data response from NerdGraph: {data}")

            return resp_data
        except Exception as exc:
            last_error = exc
            if not _is_transient(exc) or attempt == _MAX_RETRIES - 1:
                raise
            sleep_sec = _INITIAL_RETRY_DELAY * (2**attempt)
            logger.warning(
                "Transient error contacting New Relic NerdGraph (attempt %d/%d): %s. "
                "Retrying in %.1fs...",
                attempt + 1,
                _MAX_RETRIES,
                exc,
                sleep_sec,
            )
            time.sleep(sleep_sec)

    if last_error:
        raise last_error
    raise NewRelicAPIError("NerdGraph GraphQL execution failed after retries.")


def _policy_to_row(
    policy: dict[str, Any],
    conditions: list[dict[str, Any]],
    account_id: int,
) -> dict[str, Any]:
    """Format an alert policy and its NRQL conditions into a Markdown document row."""
    policy_id = str(policy.get("id", ""))
    name = (policy.get("name") or "Unnamed Policy").strip()
    pref = policy.get("incidentPreference") or "PER_POLICY"

    lines = [
        f"# Alert Policy: {name}",
        "",
        f"**Policy ID:** {policy_id}",
        f"**Account ID:** {account_id}",
        f"**Incident Preference:** {pref}",
        "",
        "---",
        "### NRQL Conditions",
    ]

    matching_conditions = [c for c in conditions if str(c.get("policyId")) == policy_id]
    if matching_conditions:
        for cond in matching_conditions:
            c_name = cond.get("name") or "Condition"
            enabled = "Enabled" if cond.get("enabled", True) else "Disabled"
            nrql_query = (cond.get("nrql") or {}).get("query", "N/A")
            runbook = cond.get("runbookUrl") or "None"
            desc = cond.get("description") or ""

            terms_str = ""
            for term in cond.get("terms") or []:
                prio = term.get("priority", "CRITICAL")
                op = term.get("operator", "ABOVE")
                val = term.get("threshold", "N/A")
                dur = term.get("thresholdDuration", "300")
                terms_str += f" [{prio}: {op} {val} for {dur}s]"

            lines.append(f"- **{c_name}** ({enabled}){terms_str}")
            lines.append(f"  - **NRQL:** `{nrql_query}`")
            if desc:
                lines.append(f"  - **Description:** {desc}")
            if runbook != "None":
                lines.append(f"  - **Runbook:** {runbook}")
    else:
        lines.append("*(No conditions configured)*")

    content = "\n".join(lines)
    return {
        "id": f"new_relic_policy_{policy_id}",
        "title": f"Alert Policy: {name}",
        "content": content,
        "url": f"https://one.newrelic.com/launcher/nr1-core.explorer?account={account_id}",
        "resource_type": "policy",
        "account_id": account_id,
    }


def _dashboard_to_row(dashboard: dict[str, Any]) -> dict[str, Any]:
    """Format a New Relic dashboard entity into a Markdown document row."""
    guid = str(dashboard.get("guid", ""))
    name = (dashboard.get("name") or "Untitled Dashboard").strip()
    desc = (dashboard.get("description") or "").strip()
    updated_at = str(dashboard.get("updatedAt") or "")
    account_id = dashboard.get("accountId") or 0

    lines = [
        f"# Dashboard: {name}",
        "",
        f"**GUID:** {guid}",
        f"**Account ID:** {account_id}",
        f"**Updated At:** {updated_at}",
    ]

    if desc:
        lines.append(f"**Description:** {desc}")

    tags = dashboard.get("tags") or []
    if tags:
        tag_strs = [
            f"{t.get('key')}: {', '.join(t.get('values', []))}" for t in tags if t.get("key")
        ]
        if tag_strs:
            lines.append(f"**Tags:** {'; '.join(tag_strs)}")

    lines.append("\n---\n")
    pages = dashboard.get("pages") or []
    if pages:
        lines.append("### Pages & Widgets")
        for page in pages:
            p_name = page.get("name") or "Overview Page"
            lines.append(f"#### Page: {p_name}")
            for widget in page.get("widgets") or []:
                w_title = widget.get("title") or "Widget"
                w_config = widget.get("rawConfiguration") or {}
                nrql = (w_config.get("nrqlQueries") or [{}])[0].get("query")
                if nrql:
                    lines.append(f"- **{w_title}**: `{nrql}`")
                else:
                    lines.append(f"- **{w_title}**")
    else:
        lines.append("*(Dashboard outline indexed)*")

    content = "\n".join(lines)
    return {
        "id": f"new_relic_dashboard_{guid}",
        "title": f"Dashboard: {name}",
        "content": content,
        "url": f"https://one.newrelic.com/redirect/entity/{guid}",
        "updated_at": updated_at,
        "resource_type": "dashboard",
        "account_id": account_id,
    }


def _issue_to_row(issue: dict[str, Any], account_id: int) -> dict[str, Any]:
    """Format an incident / issue event into a Markdown document row."""
    issue_id = str(issue.get("issueId", ""))
    title = (issue.get("title") or "Unnamed Incident").strip()
    priority = issue.get("priority") or "CRITICAL"
    state = issue.get("state") or "ACTIVATED"
    activated_at = str(issue.get("activatedAt") or "")
    closed_at = str(issue.get("closedAt") or "Active / Open")
    sources = ", ".join(issue.get("sources") or [])
    cond_name = issue.get("conditionName") or "N/A"

    lines = [
        f"# Incident: {title}",
        "",
        f"**Issue ID:** {issue_id}",
        f"**Account ID:** {account_id}",
        f"**Priority:** {priority}",
        f"**State:** {state}",
        f"**Activated At:** {activated_at}",
        f"**Closed At:** {closed_at}",
        f"**Condition:** {cond_name}",
    ]
    if sources:
        lines.append(f"**Sources:** {sources}")

    lines.append("\n---\n*(Incident event record)*")
    content = "\n".join(lines)

    return {
        "id": f"new_relic_incident_{issue_id}",
        "title": f"Incident: {title}",
        "content": content,
        "url": (
            f"https://one.newrelic.com/launcher/nr1-core.explorer"
            f"?account={account_id}&pane=eyJuZXJkbGV0SWQiOiJpbmNpZGVudHMtbmVyZGxldCJ9"
        ),
        "activated_at": activated_at,
        "state": state,
        "resource_type": "incident",
        "account_id": account_id,
    }


def _iter_new_relic_rows(
    client: httpx.Client,
    base_url: str,
    account_id: int,
    resources: list[str],
    incremental: bool = True,
    resource_state: dict[str, Any] | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Fetch New Relic observability data, apply incremental filtering, and tombstone removals."""
    state = resource_state if resource_state is not None else {}
    seen_inventory: list[str] = state.get("new_relic_seen_inventory", [])
    seen_inventory_set: set[str] = set(seen_inventory)
    incident_cursor: str = state.get("new_relic_incident_cursor", "")

    current_inventory_ids: set[str] = set()
    latest_incident_time = incident_cursor

    # 1. Alert Policies & NRQL Conditions
    if "policies" in resources:
        cursor: str | None = None
        has_next = True
        while has_next:
            variables = {"accountId": account_id, "cursor": cursor}
            data = _execute_nerdgraph(client, base_url, ALERT_POLICIES_QUERY, variables)
            acct_data = (data.get("actor") or {}).get("account") or {}
            alerts_data = acct_data.get("alerts") or {}
            policies_search = alerts_data.get("policiesSearch") or {}
            policies = policies_search.get("policies") or []
            conditions_search = alerts_data.get("nrqlConditionsSearch") or {}
            conditions = conditions_search.get("nrqlConditions") or []

            for policy in policies:
                row = _policy_to_row(policy, conditions, account_id)
                current_inventory_ids.add(row["id"])
                yield row

            cursor = policies_search.get("nextCursor")
            has_next = bool(cursor)

    # 2. Dashboards
    if "dashboards" in resources:
        cursor = None
        has_next = True
        while has_next:
            variables = {"cursor": cursor}
            data = _execute_nerdgraph(client, base_url, DASHBOARDS_QUERY, variables)
            actor_data = data.get("actor") or {}
            entity_search = (actor_data.get("entitySearch") or {}).get("results") or {}
            entities = entity_search.get("entities") or []

            for entity in entities:
                # Filter by account ID if entity provides accountId
                entity_account = entity.get("accountId")
                if entity_account and int(entity_account) != account_id:
                    continue
                row = _dashboard_to_row(entity)
                current_inventory_ids.add(row["id"])
                yield row

            cursor = entity_search.get("nextCursor")
            has_next = bool(cursor)

    # 3. Incidents / Issues
    if "incidents" in resources:
        cursor = None
        has_next = True
        while has_next:
            variables = {"accountId": account_id, "cursor": cursor}
            data = _execute_nerdgraph(client, base_url, ISSUES_QUERY, variables)
            acct_data = (data.get("actor") or {}).get("account") or {}
            ai_issues = (acct_data.get("aiIssues") or {}).get("issues") or {}
            issues = ai_issues.get("issues") or []

            for issue in issues:
                row = _issue_to_row(issue, account_id)
                act_time = row.get("activated_at") or ""
                if act_time > latest_incident_time:
                    latest_incident_time = act_time

                if incremental and incident_cursor and act_time <= incident_cursor:
                    continue
                yield row

            cursor = ai_issues.get("nextCursor")
            has_next = bool(cursor)

    # Forget-on-delete tombstoning for authoritative inventories (policies & dashboards)
    if "policies" in resources or "dashboards" in resources:
        vanished_ids = seen_inventory_set - current_inventory_ids
        for vanished_id in sorted(vanished_ids):
            yield {"id": vanished_id, "_deleted": True}
        state["new_relic_seen_inventory"] = sorted(current_inventory_ids)

    if latest_incident_time:
        state["new_relic_incident_cursor"] = latest_incident_time


def new_relic_source(
    api_key: str | None = None,
    account_id: int | str | None = None,
    region: str = "US",
    base_url: str | None = None,
    resources: list[str] | None = None,
    incremental: bool = True,
    client: httpx.Client | None = None,
) -> Any:
    """Create a DLT resource for New Relic observability data compatible with Cognee document-mode.

    Args:
        api_key: New Relic User API key. Defaults to NEW_RELIC_API_KEY environment variable.
        account_id: Target New Relic Account ID. Defaults to NEW_RELIC_ACCOUNT_ID.
        region: "US" (default) or "EU".
        base_url: Custom NerdGraph endpoint URL.
        resources: Subsets of ["policies", "dashboards", "incidents"]. Defaults to all.
        incremental: Whether to use incremental syncing for incidents.
        client: Optional pre-configured httpx.Client (useful for testing and mocks).

    Returns:
        dlt.resource yielding New Relic document rows.
    """
    key = api_key or os.environ.get("NEW_RELIC_API_KEY")
    if not key and client is None:
        raise ValueError("New Relic API key is required: provide api_key or set NEW_RELIC_API_KEY.")

    raw_account_id = account_id or os.environ.get("NEW_RELIC_ACCOUNT_ID")
    if not raw_account_id and client is None:
        raise ValueError(
            "New Relic Account ID is required: provide account_id or set NEW_RELIC_ACCOUNT_ID."
        )

    try:
        acct_id = int(raw_account_id) if raw_account_id else 0
    except ValueError as err:
        raise ValueError(f"Account ID must be an integer, got: {raw_account_id}") from err

    if base_url:
        endpoint = base_url
    elif region.upper() == "EU":
        endpoint = EU_NERDGRAPH_URL
    else:
        endpoint = US_NERDGRAPH_URL

    valid_resources = {"policies", "dashboards", "incidents"}
    selected_resources = [r.lower() for r in (resources or ["policies", "dashboards", "incidents"])]
    for r in selected_resources:
        if r not in valid_resources:
            raise ValueError(
                f"Invalid resource '{r}'. Valid options are: {sorted(valid_resources)}"
            )

    headers = {
        "API-Key": key.strip() if key else "",
        "Content-Type": "application/json",
        "User-Agent": "cognee-community-connector-new-relic/0.1.0",
    }

    try:
        import dlt
    except ImportError as err:
        raise ImportError(
            "The New Relic connector requires dlt: pip install dlt[sqlalchemy]"
        ) from err

    @dlt.resource(
        name=NEW_RELIC_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def new_relic_resource_gen() -> Iterable[dict[str, Any]]:
        state = dlt.current.resource_state()
        if client is not None:
            yield from _iter_new_relic_rows(
                client=client,
                base_url=endpoint,
                account_id=acct_id,
                resources=selected_resources,
                incremental=incremental,
                resource_state=state,
            )
        else:
            with httpx.Client(headers=headers, timeout=30.0) as http_client:
                yield from _iter_new_relic_rows(
                    client=http_client,
                    base_url=endpoint,
                    account_id=acct_id,
                    resources=selected_resources,
                    incremental=incremental,
                    resource_state=state,
                )

    res = new_relic_resource_gen()
    # Mark for Cognee document-mode ingestion and pipeline scoping
    setattr(res, DOCUMENT_SOURCE_ATTR, NEW_RELIC_SOURCE_NAME)
    setattr(res, dlt_utils.PIPELINE_SCOPE_ATTR, NEW_RELIC_SOURCE_NAME)
    return res
