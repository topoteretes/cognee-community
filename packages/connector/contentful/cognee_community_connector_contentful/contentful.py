"""A scoped Contentful resource with streaming extraction and recoverable staging."""

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from ._http import DeliveryClient, identifier
from ._render import document, row_id
from ._sync import EventSpool, resolve_event
from .exceptions import ContentfulResponseError, ContentfulSourceError, ContentfulSyncTokenError

if TYPE_CHECKING:
    import httpx
    from dlt.extract.resource import DltResource


# Cognee excludes dlt-internal columns from document content hashes. The priority
# must not make an unchanged document appear edited after a conflict or refresh.
_PRIORITY = "contentful_dlt_priority"


def _selection(values, label, *, ordered=False):
    if values is None:
        return None
    if isinstance(values, str) or not isinstance(values, (list, tuple)) or not values:
        raise ValueError(f"Contentful {label} must be a nonempty list or None.")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"Contentful {label} must contain nonempty strings.")
    return list(dict.fromkeys(values)) if ordered else sorted(set(values))


def contentful_source(
    *,
    space_id: str,
    token: str,
    resource_name: str,
    environment: str = "master",
    content_type_ids: list[str] | None = None,
    locales: list[str] | None = None,
    include_assets: bool = True,
    source_id: str = "default",
    host: str = "cdn.contentful.com",
    service: "httpx.Client | None" = None,
    check_active: Callable[[], None] | None = None,
) -> "DltResource":
    """Build a document resource for foreground ``cognee.remember(..., merge)``.

    Keep ``resource_name`` fixed and unique per logical source in a dataset. It
    names the staging table and pipeline scope. ``service`` is a synchronous
    httpx client owned by the caller. Configuration never reads the environment.
    ``check_active`` may raise to stop extraction before another request.
    """
    import dlt
    from cognee.tasks.ingestion import dlt_utils

    if (
        getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1
        or not getattr(dlt_utils, "PIPELINE_SCOPE_ATTR", None)
        or not callable(getattr(dlt_utils, "pipeline_name_for_source", None))
    ):
        raise ContentfulSourceError(
            "Contentful requires Cognee's scoped document-sync contract; upgrade Cognee."
        )
    space = identifier(space_id, "space_id")
    environment = identifier(environment, "environment")
    if (
        not isinstance(token, str)
        or not token.strip()
        or not token.isascii()
        or any(not 33 <= ord(char) <= 126 for char in token)
    ):
        raise ValueError("Pass a nonempty Contentful Delivery token.")
    if not isinstance(resource_name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", resource_name):
        raise ValueError("Contentful resource_name must be a lowercase snake_case table name.")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("Contentful source_id must be a nonempty string.")
    if not isinstance(include_assets, bool):
        raise ValueError("Contentful include_assets must be boolean.")
    if check_active is not None and not callable(check_active):
        raise ValueError("Contentful check_active must be callable or None.")
    selection = {
        "content_type_ids": _selection(content_type_ids, "content_type_ids"),
        "locales": _selection(locales, "locales", ordered=True),
        "include_assets": include_assets,
    }
    identity = [host, space, environment, source_id]
    stats = dict.fromkeys(("synced", "deleted", "failed_rate_limit", "failed_auth"), 0)
    transport = DeliveryClient(host, space, environment, token, service, check_active, stats)

    def selected_type(model_id):
        return selection["content_type_ids"] is None or model_id in selection["content_type_ids"]

    def selected(item):
        if item["sys"]["type"] == "Asset":
            return include_assets
        link = item["sys"].get("contentType")
        system = link.get("sys") if isinstance(link, dict) else None
        model_id = system.get("id") if isinstance(system, dict) else None
        if not isinstance(model_id, str) or not model_id:
            raise ContentfulResponseError("Contentful entry has no content-type identity.")
        return selected_type(model_id)

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={
            "id": {"data_type": "text", "nullable": False},
            "title": {"data_type": "text", "nullable": True},
            "content": {"data_type": "text", "nullable": True},
            "url": {"data_type": "text", "nullable": True},
            "_deleted": {"data_type": "bool", "hard_delete": True},
            _PRIORITY: {"data_type": "bigint", "dedup_sort": "desc"},
        },
    )
    def documents():
        stats.update(dict.fromkeys(stats, 0))
        schema = dlt.current.resource().compute_table_schema()
        primary_key = {
            name for name, column in schema["columns"].items() if column.get("primary_key")
        }
        if (
            schema.get("write_disposition") != "merge"
            or primary_key != {"id"}
            or schema.get("x-merge-strategy") not in (None, "delete-insert")
            or schema.get("x-stage-data-deduplicated")
            or schema["columns"][_PRIORITY].get("dedup_sort") != "desc"
            or schema["columns"]["_deleted"].get("hard_delete") is not True
            or schema["columns"]["_deleted"].get("data_type") != "bool"
            or schema["name"] != resource_name
        ):
            raise ContentfulSourceError(
                "Contentful requires default merge, primary_key='id', and its dedup_sort hint."
            )
        state = dlt.current.resource_state()
        if state and (
            state.get("version") != 2
            or not isinstance(state.get("sync_token"), str)
            or not state["sync_token"]
            or not isinstance(state.get("selection"), dict)
            or not isinstance(state.get("members"), list)
            or not isinstance(state.get("models"), dict)
            or not isinstance(state.get("display_fields"), dict)
        ):
            raise ContentfulSourceError(
                "Contentful resume state is invalid; inspect the source state."
            )
        if state and state.get("identity") != identity:
            raise ContentfulSourceError(
                "Contentful resource_name already belongs to a different source."
            )
        old_members = set(state.get("members", []))
        emitted = set()
        with transport as delivery, EventSpool() as spool:
            models, hashes = delivery.model_inventory()
            retained_models = set()
            for model_id in state.get("models", {}):
                if model_id not in models and selected_type(model_id):
                    for _attempt in range(2):
                        found = delivery.get(f"content_types/{model_id}", missing=True)
                        if found is None:
                            break
                        delivery.validate_item(found, {"ContentType"})
                        if found["sys"]["id"] != model_id:
                            raise ContentfulResponseError(
                                "Contentful model lookup returned a different resource."
                            )
                    if found is not None:
                        # The list may lag the detail CDN. Keep the old document and retry next run.
                        models[model_id] = {
                            **found,
                            "displayField": state["display_fields"].get(model_id),
                        }
                        hashes[model_id] = state["models"][model_id]
                        retained_models.add(model_id)
            display_fields = {
                key: (
                    state["display_fields"].get(key)
                    if key in retained_models
                    else model.get("displayField")
                )
                for key, model in models.items()
                if selected_type(key)
            }
            refresh = (
                not state
                or state["selection"] != selection
                or any(
                    state["display_fields"].get(key) != value
                    for key, value in display_fields.items()
                )
            )
            token = None if refresh else state["sync_token"]
            for attempt in range(2):
                priority = attempt * 2
                members = set() if refresh else old_members.copy()
                synced = set()
                spool.clear()
                terminal_token = None
                try:
                    for items, page_token in delivery.iter_sync(token):
                        terminal_token = page_token
                        for item in items:
                            if not spool.add(item) or item["sys"]["type"].startswith("Deleted"):
                                continue
                            if selected(item):
                                row = document(
                                    item, space, environment, selection["locales"], models
                                )
                                emitted.add(row["id"])
                                yield {**row, _PRIORITY: priority}
                    break
                except ContentfulSyncTokenError:
                    if attempt:
                        raise
                    # Already yielded provisional rows have lower priority than this full refresh.
                    refresh, token = True, None
            if not terminal_token:
                raise ContentfulResponseError("Contentful Sync did not complete its traversal.")
            pending_deletes = set()
            for kind, upstream_id, count in spool.identities():
                item = resolve_event(delivery, spool, kind, upstream_id, count)
                key = row_id(space, environment, kind, upstream_id)
                if item["sys"]["type"].startswith("Deleted") or not selected(item):
                    members.discard(key)
                    if key in old_members or key in emitted:
                        pending_deletes.add(key)
                    continue
                members.add(key)
                synced.add(key)
                if count > 1:
                    row = document(item, space, environment, selection["locales"], models)
                    yield {**row, _PRIORITY: priority + 1}
            selected_models = {}
            for model_id in sorted(models):
                if not selected_type(model_id):
                    continue
                key = row_id(space, environment, "ContentType", model_id)
                members.add(key)
                selected_models[model_id] = hashes[model_id]
                if model_id not in retained_models and (
                    refresh or state.get("models", {}).get(model_id) != hashes[model_id]
                ):
                    row = document(
                        models[model_id], space, environment, selection["locales"], models
                    )
                    synced.add(key)
                    yield {**row, _PRIORITY: priority + 1}
            for model_id in state.get("models", {}):
                if model_id not in selected_models:
                    key = row_id(space, environment, "ContentType", model_id)
                    members.discard(key)
                    pending_deletes.add(key)
            if refresh:
                pending_deletes.update((old_members | emitted) - members)
            for key in sorted(pending_deletes):
                yield {"id": key, "_deleted": True, _PRIORITY: priority + 1}
            if check_active is not None:
                check_active()
            state.update(
                version=2,
                identity=identity,
                sync_token=terminal_token,
                selection=selection,
                members=sorted(members),
                models=selected_models,
                display_fields=display_fields,
            )
            stats.update(synced=len(synced), deleted=len(pending_deletes))

    result = documents()
    setattr(result, dlt_utils.DOCUMENT_SOURCE_ATTR, "contentful")
    setattr(result, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    result.cognee_sync_stats = stats
    return result
