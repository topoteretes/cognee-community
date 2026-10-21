"""Selected Grafana documents with complete replacement and retained incremental rendering."""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .client import GrafanaClient, GrafanaSyncError, SyncStats, ensure_active
from .rendering import annotation_row, cleaner, dashboard_row, tags

_API = "dashboard.grafana.app/v1beta1"
_RENDER_VERSION = 4
_UID = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")


class GrafanaReader(Protocol):
    def get(self, path: str, params=None) -> Any: ...


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _uid(value):
    if not isinstance(value, str) or not _UID.fullmatch(value):
        raise GrafanaSyncError("Invalid Grafana dashboard/folder UID")
    return value


def _selection(value, name):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list of UIDs/tags")
    if any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{name} entries must be nonempty strings")
    if name == "organization_annotation_tags":
        normalized = set()
        for item in value:
            # v12.3.1 trims ASCII spaces, drops empty keys and silently truncates
            # extra colon components. Reject the latter two instead of broadening
            # a caller's selection; normalize duplicates before SQL counts filters.
            if item.count(":") > 1:
                raise ValueError("Annotation tag filters may contain at most one colon")
            key, _, tag_value = item.partition(":")
            key, tag_value = key.strip(" "), tag_value.strip(" ")
            if not key:
                raise ValueError("Annotation tag filters require a nonempty key")
            normalized.add(key + (":" + tag_value if tag_value else ""))
        return sorted(normalized)
    for item in value:
        try:
            _uid(item)
        except GrafanaSyncError:
            raise ValueError(f"Invalid {name} entry") from None
    return sorted(set(value))


def _tag_matches(selected, actual):
    key, _, value = selected.partition(":")
    for item in actual:
        # Grafana tag.ParseTagPairs splits at colons and uses the first two parts.
        parts = item.split(":")
        item_key = parts[0].strip(" ")
        item_value = parts[1].strip(" ") if len(parts) > 1 else ""
        if key == item_key and (not value or value == item_value):
            return True
    return False


class _Reads:
    def __init__(self, service, stats, check_active, max_requests):
        self.service, self.stats = service, stats
        self.check_active, self.remaining = check_active, max_requests

    def get(self, path, params=None):
        ensure_active(self.check_active)
        if self.remaining <= 0:
            raise GrafanaSyncError("Grafana request budget exhausted")
        self.remaining -= 1
        self.stats.requests += 1
        try:
            result = self.service.get(path, params=params)
        except GrafanaSyncError:
            raise
        except Exception as exc:
            raise GrafanaSyncError(f"Grafana service read failed ({type(exc).__name__})") from None
        ensure_active(self.check_active)
        return result


def _authority(reads, org_id):
    org = reads.get("/api/org")
    user = reads.get("/api/user")
    permissions = reads.get("/api/access-control/user/permissions")
    if not isinstance(org, dict) or type(org.get("id")) is not int or org["id"] != org_id:
        raise GrafanaSyncError("Grafana organization differs from configured organization")
    if (
        not isinstance(user, dict)
        or user.get("orgId") != org_id
        or user.get("isDisabled") is True
        or not re.fullmatch(r"service-account:[1-9][0-9]*", str(user.get("uid", "")))
    ):
        raise GrafanaSyncError(
            "A service-account identity in the configured organization is required"
        )
    if not isinstance(permissions, dict):
        raise GrafanaSyncError("Invalid Grafana effective permissions")
    for action, scope in (
        ("dashboards:read", "dashboards:*"),
        ("folders:read", "folders:*"),
        ("annotations:read", "annotations:*"),
    ):
        granted = permissions.get(action)
        if not isinstance(granted, list) or scope not in granted:
            raise GrafanaSyncError(
                f"Complete inventory requires {action} on {scope}; restricted visibility is unsafe"
            )
    account_id = int(user["uid"].split(":", 1)[1])
    current_account = reads.get(f"/api/serviceaccounts/{account_id}")
    if (
        not isinstance(current_account, dict)
        or current_account.get("id") != account_id
        or current_account.get("orgId") != org_id
        or current_account.get("role") != "Admin"
        or current_account.get("isDisabled") is not False
    ):
        raise GrafanaSyncError(
            "Current Grafana service account must be an enabled organization Admin"
        )
    return user["uid"]


def _inventory(reads, route, namespace):
    result, seen = {}, set()
    cursor = None
    while True:
        page = reads.get(route, {"limit": 100, **({"continue": cursor} if cursor else {})})
        if (
            not isinstance(page, dict)
            or page.get("kind") != "DashboardList"
            or page.get("apiVersion") != _API
            or not isinstance(page.get("items"), list)
            or not isinstance(page.get("metadata"), dict)
        ):
            raise GrafanaSyncError("Unsupported or incomplete Grafana dashboard listing")
        for item in page["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
                raise GrafanaSyncError("Missing Grafana dashboard metadata")
            meta = item["metadata"]
            uid = _uid(meta.get("name"))
            revision = meta.get("resourceVersion")
            if (
                meta.get("namespace") != namespace
                or not isinstance(revision, str)
                or not revision
                or not isinstance(item.get("spec"), dict)
            ):
                raise GrafanaSyncError("Invalid Grafana dashboard revision/namespace/spec")
            if uid in result:
                raise GrafanaSyncError("Repeated Grafana dashboard route UID")
            annotations = meta.get("annotations", {})
            if not isinstance(annotations, dict):
                raise GrafanaSyncError("Invalid Grafana dashboard annotations metadata")
            if meta.get("deletionTimestamp"):
                # This API's normal inventory must contain current resources, not tombstones.
                raise GrafanaSyncError("Dashboard tombstone appeared in current inventory")
            result[uid] = item
        cursor = page["metadata"].get("continue")
        if cursor in (None, ""):
            return result
        if not isinstance(cursor, str) or cursor in seen:
            raise GrafanaSyncError("Invalid/repeated Grafana continuation token")
        seen.add(cursor)


def _selected(inventory, dashboards, folders):
    return {
        uid: item
        for uid, item in inventory.items()
        if uid in dashboards
        or item["metadata"].get("annotations", {}).get("grafana.app/folder") in folders
    }


def _annotation_scan(reads, queries, start, end, limit):
    result = {}
    for query in queries:
        pending = [(start, end)]
        observed = {}
        while pending:
            lower, upper = pending.pop()
            items = reads.get(
                "/api/annotations",
                [*query, ("from", lower), ("to", upper), ("limit", limit), ("type", "annotation")],
            )
            if not isinstance(items, list):
                raise GrafanaSyncError("Invalid Grafana annotation listing")
            for item in items:
                if not isinstance(item, dict):
                    raise GrafanaSyncError("Invalid Grafana annotation")
                if any(type(item.get(field)) is not int for field in ("id", "time")):
                    raise GrafanaSyncError("Invalid Grafana annotation ID/time")
                finish = item.get("timeEnd", item["time"])
                if (
                    item["id"] <= 0
                    or type(finish) is not int
                    or finish < item["time"]
                    or item["time"] > upper
                    or finish < lower
                    or item.get("alertId", 0) != 0
                ):
                    raise GrafanaSyncError("Annotation outside requested human-event interval")
                uid = item.get("dashboardUID") or ""
                if uid:
                    _uid(uid)
                item_tags = tags(item.get("tags"))
                # Validate provider filtering, including rows later excluded from org-only scope.
                if any(key == "dashboardUID" and uid != value for key, value in query):
                    raise GrafanaSyncError("Annotation belongs to another dashboard")
                if any(
                    key == "tags" and not _tag_matches(value, item_tags) for key, value in query
                ):
                    raise GrafanaSyncError("Annotation did not match selected tags")
                key = str(item["id"])
                if key in observed and observed[key] != item:
                    raise GrafanaSyncError("Annotation changed during window traversal")
                observed[key] = item
            if len(items) >= limit:
                if lower == upper:
                    raise GrafanaSyncError("Annotation cap saturated at one millisecond")
                middle = (lower + upper) // 2
                pending.extend([(lower, middle), (middle + 1, upper)])
                continue
            for item in items:
                # Organization tag queries also return tagged dashboard events on this release.
                if query and query[0][0] == "tags" and item.get("dashboardUID"):
                    continue
                key = str(item["id"])
                if key in result and result[key] != item:
                    raise GrafanaSyncError("Annotation changed between selected queries")
                result[key] = item
    return result


def _snapshot(
    reads,
    state,
    *,
    origin,
    org_id,
    namespace,
    dashboards,
    folders,
    org_tags,
    start,
    end,
    limit,
    redactions,
    stats,
):
    account = _authority(reads, org_id)
    binding = [origin, org_id, namespace, account]
    if state:
        if state.get("version") != 1 or state.get("binding") != binding:
            raise GrafanaSyncError(
                "Grafana resource binding/state changed; use the original account"
            )
        if not isinstance(state.get("cache"), dict) or not isinstance(state.get("rows"), dict):
            raise GrafanaSyncError("Invalid Grafana snapshot state")
        if end < state.get("end", end):
            raise GrafanaSyncError("Annotation upper bound cannot move backwards")
    route = f"/apis/{_API}/namespaces/{namespace}/dashboards"
    first = _selected(_inventory(reads, route, namespace), dashboards, folders)
    queries = [[("dashboardUID", uid)] for uid in sorted(first)]
    if org_tags:
        queries.append([("tags", tag) for tag in org_tags])
    events = _annotation_scan(reads, queries, start, end, limit)
    if events != _annotation_scan(reads, queries, start, end, limit):
        raise GrafanaSyncError("Annotation inventory changed between complete scans")
    if first != _selected(_inventory(reads, route, namespace), dashboards, folders):
        raise GrafanaSyncError("Selected dashboard inventory changed during sync")
    if _authority(reads, org_id) != account:
        raise GrafanaSyncError("Grafana account changed during sync")
    clean = cleaner(redactions)
    rows, cache = {}, {}
    old_cache = state.get("cache", {})
    for uid, item in sorted(first.items()):
        conversion = item.get("status", {}).get("conversion", {})
        if not isinstance(conversion, dict) or conversion.get("failed") is not False:
            raise GrafanaSyncError("Grafana dashboard conversion was not verified successful")
        signature = _digest([_RENDER_VERSION, item, redactions])
        previous = old_cache.get(uid, {})
        if previous.get("signature") == signature:
            row = deepcopy(previous["row"])
        else:
            row = dashboard_row(uid, item, origin, org_id, clean)
        cache[uid] = {"signature": signature, "row": row}
        rows[row["id"]] = row
    for item in events.values():
        row = annotation_row(item, origin, org_id, clean)
        rows[row["id"]] = row
    identity = _digest([origin, org_id])[:24]
    rows = {f"{identity}:{key}": {**row, "id": f"{identity}:{key}"} for key, row in rows.items()}
    old_rows = state.get("rows", {})
    stats.dashboards, stats.annotations = len(first), len(events)
    stats.unchanged = sum(old_rows.get(key) == row for key, row in rows.items())
    stats.changed = len(rows) - stats.unchanged
    stats.removed = len(set(old_rows) - set(rows))
    return [rows[key] for key in sorted(rows)], {
        "version": 1,
        "binding": binding,
        "end": end,
        "cache": cache,
        "rows": rows,
    }


def grafana_source(
    *,
    base_url: str,
    token: str,
    org_id: int,
    owner_id: str,
    resource_name: str,
    annotation_from_ms: int,
    dashboard_uids: list[str] | None = None,
    folder_uids: list[str] | None = None,
    organization_annotation_tags: list[str] | None = None,
    namespace: str = "default",
    annotation_to_ms: int | None = None,
    service: GrafanaReader | None = None,
    check_active: Callable | None = None,
    stats: SyncStats | None = None,
    annotation_limit: int = 100,
    max_requests: int = 1000,
    redact_values: Iterable[str] = (),
):
    """Create a network-lazy dlt document source for Grafana OSS 12.3.1.

    Requires organization-wide dashboard/folder/annotation read scopes to reconcile
    removals safely. In OSS these were verified with an organization Admin service
    account. The connector only issues GET requests. Restricted readers fail closed.

    ``owner_id`` + ``resource_name`` identify one corpus, independent of selection.
    Reuse them for reruns; use a different resource_name for independent sources.
    Intentional selection/lower-bound changes evict excluded documents in that
    same corpus. Tokens are never used in its identity. Account changes are refused.
    Caller-supplied services implement get(path, params=None) and remain caller-owned.
    """
    import dlt
    from cognee.tasks.ingestion import dlt_utils

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("Grafana requires Cognee document sync version >= 1")
    if not isinstance(token, str) or not token.strip() or any(ord(c) < 32 for c in token):
        raise ValueError("A nonempty service-account token without control characters is required")
    if not isinstance(base_url, str):
        raise ValueError("Grafana base_url must be HTTP(S)")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(ord(c) < 32 for c in base_url)
    ):
        raise ValueError(
            "Grafana base_url must have no credentials, query, fragment or control characters"
        )
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Grafana requires HTTPS except on localhost")
    origin = str(httpx.URL(base_url)).rstrip("/")
    if type(org_id) is not int or org_id != 1 or namespace != "default":
        raise ValueError("Initial OSS support is organization 1, namespace default")
    if any(not isinstance(x, str) or not x.strip() for x in (owner_id, resource_name)):
        raise ValueError("owner_id and distinct resource_name are required")
    if type(annotation_from_ms) is not int or annotation_from_ms < 1:
        raise ValueError("annotation_from_ms must be a strictly positive epoch millisecond")
    if annotation_to_ms is not None and (
        type(annotation_to_ms) is not int or annotation_to_ms < annotation_from_ms
    ):
        raise ValueError("annotation_to_ms must be >= annotation_from_ms")
    if type(annotation_limit) is not int or not 2 <= annotation_limit <= 100:
        raise ValueError("annotation_limit must be between 2 and 100")
    if type(max_requests) is not int or max_requests < 10:
        raise ValueError("max_requests must be >= 10")
    if isinstance(redact_values, (str, bytes)):
        raise ValueError("redact_values must be an iterable of strings")
    try:
        # Snapshot once before validation: generators must not be consumed and
        # discarded, and later caller mutations must not alter retained policy.
        redactions = tuple(redact_values)
    except TypeError:
        raise ValueError("redact_values must be an iterable of strings") from None
    if any(not isinstance(value, str) for value in redactions):
        raise ValueError("redact_values must be an iterable of strings")
    redactions += (token,)
    dashboards = _selection(dashboard_uids, "dashboard_uids")
    folders = _selection(folder_uids, "folder_uids")
    org_tags = _selection(organization_annotation_tags, "organization_annotation_tags")
    # Explicit empty selections are useful: they retire this corpus without minting another table.
    if dashboard_uids is folder_uids is organization_annotation_tags is None:
        raise ValueError(
            "Explicit dashboard, folder or organization-annotation selection is required"
        )
    name = "grafana_" + _digest([owner_id, resource_name])[:32]
    stats = stats if stats is not None else SyncStats()

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="replace",
        columns={key: {"data_type": "text"} for key in ("id", "title", "content")},
    )
    def documents():
        from cognee.shared.logging_utils import get_logger

        for field in vars(stats):
            setattr(stats, field, 0)
        active_service = service
        try:
            ensure_active(check_active)
            if active_service is None:
                active_service = GrafanaClient(origin, token, org_id, check_active=check_active)
            reads = _Reads(active_service, stats, check_active, max_requests)
            state = dlt.current.source_state().setdefault("grafana_snapshot", {})
            if not isinstance(state, dict):
                raise GrafanaSyncError("Invalid Grafana snapshot state")
            end = annotation_to_ms if annotation_to_ms is not None else int(time.time() * 1000)
            if end < annotation_from_ms:
                raise GrafanaSyncError("Annotation upper bound precedes initial history")
            rows, next_state = _snapshot(
                reads,
                state,
                origin=origin,
                org_id=org_id,
                namespace=namespace,
                dashboards=dashboards,
                folders=folders,
                org_tags=org_tags,
                start=annotation_from_ms,
                end=end,
                limit=annotation_limit,
                redactions=redactions,
                stats=stats,
            )
            ensure_active(check_active)
            yield rows
            state.clear()
            state.update(next_state)
            get_logger(__name__).info(
                "Grafana snapshot: %d dashboards, %d annotations, "
                "%d changed, %d unchanged, %d removed",
                stats.dashboards,
                stats.annotations,
                stats.changed,
                stats.unchanged,
                stats.removed,
            )
        except Exception as exc:
            stats.failures += 1
            get_logger(__name__).warning("Grafana snapshot withheld (%s)", type(exc).__name__)
            raise
        finally:
            if service is None and active_service is not None:
                active_service.close()

    @dlt.source(name=name)
    def source():
        return documents

    result = source()
    setattr(result, dlt_utils.DOCUMENT_SOURCE_ATTR, "grafana")
    setattr(result, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    return result
