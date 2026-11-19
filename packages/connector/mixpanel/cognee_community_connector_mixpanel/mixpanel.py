"""Selected definition inventories → incremental documents + deletion tombstones."""

import asyncio
import contextlib
import fcntl
import hashlib
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .client import MixpanelClient
from .documents import canonical, document, semantic_hash
from .errors import MixpanelConnectorError, ResponseContractError

MIXPANEL_TABLE_NAME = "mixpanel_definitions"
MIXPANEL_SOURCE_NAME = "mixpanel"


def dataset_name_for_scope(scope: str) -> str:
    return "mixpanel_" + hashlib.sha256(scope.encode()).hexdigest()[:24]


@contextlib.contextmanager
def _sync_lock(directory: Path, scope: str):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = directory / (hashlib.sha256(scope.encode()).hexdigest() + ".lock")
    with filename.open("a") as lock:
        os.chmod(filename, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MixpanelConnectorError(
                "A sync for this access scope is already running."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _selected(
    items: list[dict], kind: str, ids: tuple | None, report_types: tuple | None
) -> list[dict]:
    result, identities = [], set()
    for item in items:
        if kind == "schema":
            if item.get("entityType") not in {"event", "profile", "custom_event"}:
                raise ResponseContractError("Schema inventory has an unknown entity type.")
            if item["entityType"] != "event":
                continue
            key = item.get("name")
            if not isinstance(key, str) or not key:
                raise ResponseContractError("Schema inventory has an invalid event name.")
        else:
            key = item.get("id")
            if isinstance(key, bool) or not isinstance(key, int) or key <= 0:
                raise ResponseContractError("Inventory has an invalid definition ID.")
        if key in identities:
            raise ResponseContractError("Inventory has duplicate definition identities.")
        identities.add(key)
        if ids is not None and key not in ids:
            continue
        if kind == "report" and report_types is not None and item.get("type") not in report_types:
            continue
        if item.get("can_view") is False or item.get("is_visible") is False:
            raise ResponseContractError(
                "Selected definition became inaccessible; refusing partial sync."
            )
        result.append(item)
    return result


def _capture(client, resources, selections, report_types, max_items, max_bytes, include_sensitive):
    before = client.access_fingerprint()
    first, rows = {}, []
    for kind in resources:
        items = _selected(client.inventory(kind), kind, selections[kind], report_types)
        first[kind] = items
        if len(rows) + len(items) > max_items:
            raise ResponseContractError("Selected definitions exceed max_items; narrow selection.")
        for item in items:
            if kind != "schema":
                item = client.detail(kind, item["id"])
            rows.append(document(kind, item, client.scope, client.project_url, include_sensitive))
        if sum(len(canonical(row).encode()) for row in rows) > max_bytes:
            raise ResponseContractError("Selected definitions exceed max_batch_bytes.")
    # Two independent inventory reads catch missing pages, moves and changes
    # detectable in provider metadata. They are not an atomic provider snapshot.
    for kind in resources:
        second = _selected(client.inventory(kind), kind, selections[kind], report_types)

        def signatures(items):
            return sorted(
                canonical(
                    {
                        k: v
                        for k, v in item.items()
                        if k
                        not in {
                            "count",
                            "last_queried",
                            "unique_view_count",
                            "total_view_count",
                        }
                    }
                )
                for item in items
            )

        if signatures(first[kind]) != signatures(second):
            raise ResponseContractError("Inventory changed during sync; retry without publishing.")
    if before != client.access_fingerprint():
        raise ResponseContractError("Access metadata changed during sync; staging retained.")
    return rows, before


def mixpanel_source(
    *,
    project_id: str | None = None,
    service_account_username: str | None = None,
    service_account_secret: str | None = None,
    region: str | None = None,
    workspace_id: int | None = None,
    resources=("schema", "report", "cohort"),
    report_ids=None,
    cohort_ids=None,
    schema_names=None,
    report_types=None,
    include_sensitive_literals=False,
    max_items=25,
    max_batch_bytes=2_000_000,
    state_directory=None,
    client: Any = None,
):
    """Return a document-mode source; call sync_mixpanel to guarantee merge/scoping.

    Missing objects are removed from the selected, visible corpus only after two
    consistent inventories and unchanged access metadata. This is visibility
    reconciliation, not proof an absent object was physically deleted upstream.
    Live inventory completeness and predicate shapes still need tenant validation.
    """
    import dlt
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    def selection(values, name, valid):
        if values is None:
            return None
        if isinstance(values, (str, bytes, dict)) or not isinstance(values, Iterable):
            raise ValueError(f"{name} must be an iterable of valid values")
        values = tuple(values)
        if any(not valid(v) for v in values):
            raise ValueError(f"{name} contains an invalid value")
        return tuple(sorted(set(values)))

    resources = selection(resources, "resources", lambda v: isinstance(v, str))
    if not resources or set(resources) - {"schema", "report", "cohort"}:
        raise ValueError("resources must select schema, report and/or cohort")
    for name, value in (("max_items", max_items), ("max_batch_bytes", max_batch_bytes)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")

    def valid_id(v):
        return isinstance(v, int) and not isinstance(v, bool) and v > 0

    def valid_name(v):
        return isinstance(v, str) and bool(v)

    selections = {
        "report": selection(report_ids, "report_ids", valid_id),
        "cohort": selection(cohort_ids, "cohort_ids", valid_id),
        "schema": selection(schema_names, "schema_names", valid_name),
    }
    report_types = selection(report_types, "report_types", valid_name)
    if not isinstance(include_sensitive_literals, bool):
        raise ValueError("include_sensitive_literals must be boolean")

    def make_client():
        if client is not None:
            if any(
                v is not None
                for v in (
                    project_id,
                    service_account_username,
                    service_account_secret,
                    region,
                    workspace_id,
                    state_directory,
                )
            ):
                raise ValueError("An injected client owns its auth, scope and state directory")
            return client
        username = service_account_username or os.getenv("MIXPANEL_SERVICE_ACCOUNT_USERNAME")
        secret = service_account_secret or os.getenv("MIXPANEL_SERVICE_ACCOUNT_SECRET")
        if not username or not secret:
            raise ValueError(
                "Set MIXPANEL_SERVICE_ACCOUNT_USERNAME and MIXPANEL_SERVICE_ACCOUNT_SECRET"
            )
        project = project_id or os.getenv("MIXPANEL_PROJECT_ID")
        if not project:
            raise ValueError("Set MIXPANEL_PROJECT_ID or pass project_id")
        return MixpanelClient(
            project_id=project,
            username=username,
            secret=secret,
            region=region or os.getenv("MIXPANEL_REGION", "us"),
            workspace_id=workspace_id,
            state_directory=state_directory,
        )

    api = make_client()
    scope = api.scope
    if not isinstance(scope, str) or not scope or not isinstance(api.project_url, str):
        raise ValueError("Client must provide a non-empty scope and project URL")
    state_path = Path(api.state_directory)

    @dlt.resource(
        name=MIXPANEL_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def definitions() -> Iterator[dict]:
        state = dlt.current.resource_state()
        if state.get("scope", scope) != scope:
            raise ResponseContractError(
                "Staging belongs to a different access scope; use a new pipeline."
            )
        if state.get("checkpoint_version", 1) != 1:
            raise ResponseContractError("Unsupported checkpoint version; migration is required.")
        with api:
            rows, access = _capture(
                api,
                resources,
                selections,
                report_types,
                max_items,
                max_batch_bytes,
                include_sensitive_literals,
            )
        old = state.get("inventory", {})
        known = set(state.get("known_ids", old))
        present = {row["id"] for row in rows}
        for kind in resources:
            if selections[kind] is not None:
                requested = {canonical([scope, kind, key]) for key in selections[kind]}
                if requested - present - known:
                    raise ResponseContractError(
                        "An explicitly selected definition has never been visible in this scope. "
                        "Verify its exact ID/name and report-type filter."
                    )
        old_access = state.get("access_fingerprint")
        if old_access is not None and access != old_access:
            raise ResponseContractError(
                "Service-account access changed since last sync; reconcile explicitly."
            )
        current = {row["id"]: semantic_hash(row) for row in rows}
        pending = [row for row in rows if old.get(row["id"]) != current[row["id"]]]
        pending.extend({"id": key, "_deleted": True} for key in sorted(set(old) - set(current)))
        yield from pending
        # dlt persists state after a successful load; failures do not advance it.
        state["inventory"] = current
        state["access_fingerprint"] = access
        state["checkpoint_version"] = 1
        state["scope"] = scope
        state["known_ids"] = sorted(known | present)

    @dlt.source(name=MIXPANEL_SOURCE_NAME)
    def source():
        return definitions

    result = source()
    setattr(result, DOCUMENT_SOURCE_ATTR, MIXPANEL_SOURCE_NAME)
    setattr(result, PIPELINE_SCOPE_ATTR, scope)
    result.mixpanel_state_directory = state_path
    return result


async def sync_mixpanel(source, *, cognify=True):
    """Serialize a scoped add/cognify run, retaining dlt staging for recovery."""
    import cognee
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR

    scope = getattr(source, PIPELINE_SCOPE_ATTR, None)
    if not isinstance(scope, str) or not scope or source.name != MIXPANEL_SOURCE_NAME:
        raise ValueError("sync_mixpanel expects a source returned by mixpanel_source")
    dataset = dataset_name_for_scope(scope)

    async def execute():
        await cognee.add(
            source,
            dataset_name=dataset,
            primary_key="id",
            write_disposition="merge",
            run_in_background=False,
        )
        if cognify:
            await cognee.cognify(datasets=[dataset])

    with _sync_lock(Path(source.mixpanel_state_directory), scope):
        # Cognee stages with asyncio.to_thread. Cancellation must not release
        # the lock while that worker can still write to this scope's database.
        task = asyncio.create_task(execute())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Retrieve a downstream exception, retaining cancellation semantics.
            if not task.cancelled():
                task.exception()
            raise
    return dataset
