"""A scoped Contentful DLT source with delta sync and recoverable document staging."""

import os
from datetime import datetime
from typing import TYPE_CHECKING

from ._http import DeliveryClient, fingerprint, identifier
from ._render import document, row_id

if TYPE_CHECKING:
    import httpx
    from dlt.extract.source import DltSource


def _selection(values, label):
    if values is None:
        return None
    if isinstance(values, str) or not isinstance(values, (list, tuple)) or not values:
        raise ValueError(f"Contentful {label} must be a nonempty list or None.")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"Contentful {label} must contain nonempty strings.")
    return sorted(set(values))


def _timestamp(item):
    system = item["sys"]
    value = system.get("deletedAt") if system["type"].startswith("Deleted") else None
    value = value or system.get("updatedAt")
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError("Contentful returned an invalid event timestamp.") from None


def _events(client, events):
    grouped = {}
    for item in events:
        kind = item["sys"]["type"].removeprefix("Deleted")
        key = (kind, item["sys"]["id"])
        grouped.setdefault(key, {})[fingerprint(item)] = item
    resolved = []
    for (kind, upstream_id), variants in grouped.items():
        items = list(variants.values())
        if len(items) == 1:
            resolved.append(items[0])
            continue
        endpoint = "entries" if kind == "Entry" else "assets"
        current = client.get(f"{endpoint}/{upstream_id}", {"locale": "*"}, missing=True)
        if current is None:
            live = [item for item in items if item["sys"]["type"] == kind]
            deleted = [item for item in items if item["sys"]["type"] == f"Deleted{kind}"]
            live_times = [_timestamp(item) for item in live]
            deleted_times = [_timestamp(item) for item in deleted]
            if live and (
                not deleted
                or any(stamp is None for stamp in live_times + deleted_times)
                or max(deleted_times) <= max(live_times)
            ):
                raise RuntimeError(
                    "Contentful missing-resource lookup contradicts live Sync evidence."
                )
            resolved.append({"sys": {"type": f"Deleted{kind}", "id": upstream_id}})
            continue
        client.validate_item(current, {kind})
        if current["sys"]["id"] != upstream_id:
            raise RuntimeError("Contentful conflict lookup returned a different resource.")
        evidence = [stamp for item in items if (stamp := _timestamp(item)) is not None]
        stamp = _timestamp(current)
        if evidence and (stamp is None or stamp < max(evidence)):
            raise RuntimeError("Contentful conflict lookup contradicts newer Sync evidence.")
        resolved.append(current)
    return resolved


def contentful_source(
    space_id: str | None = None,
    *,
    token: str | None = None,
    environment: str | None = None,
    content_type_ids: list[str] | None = None,
    locales: list[str] | None = None,
    include_assets: bool = True,
    source_id: str = "default",
    host: str = "cdn.contentful.com",
    client: "httpx.Client | None" = None,
) -> "DltSource":
    """Return a document source for foreground ``cognee.remember(..., merge)``.

    Reuse ``source_id`` to replace its selection, or choose another ID for an
    independently retained selection. Locale values are exact, without fallback.
    The configured environment must be a concrete ID. No requests run until
    extraction, and an injected HTTP client remains owned by the caller.
    """
    import dlt
    from cognee.tasks.ingestion import dlt_utils

    if (
        getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1
        or not getattr(dlt_utils, "PIPELINE_SCOPE_ATTR", None)
        or not callable(getattr(dlt_utils, "pipeline_name_for_source", None))
    ):
        raise RuntimeError(
            "Contentful requires Cognee's scoped document-sync contract; upgrade Cognee."
        )
    space = identifier(
        os.getenv("CONTENTFUL_SPACE_ID") if space_id is None else space_id, "space_id"
    )
    environment = identifier(
        os.getenv("CONTENTFUL_ENVIRONMENT_ID", "master") if environment is None else environment,
        "environment",
    )
    token = token if token is not None else os.getenv("CONTENTFUL_DELIVERY_TOKEN")
    if (
        not isinstance(token, str)
        or not token.strip()
        or not token.isascii()
        or any(not 33 <= ord(char) <= 126 for char in token)
    ):
        raise ValueError("Pass a Contentful token or set CONTENTFUL_DELIVERY_TOKEN.")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("Contentful source_id must be a nonempty string.")
    if not isinstance(include_assets, bool):
        raise ValueError("Contentful include_assets must be boolean.")
    selection = {
        "content_type_ids": _selection(content_type_ids, "content_type_ids"),
        "locales": _selection(locales, "locales"),
        "include_assets": include_assets,
    }
    # Constructing transport validates identity and host before any extraction.
    transport = DeliveryClient(host, space, environment, token, client)
    digest = fingerprint([host, space, environment, source_id])[:24]
    resource_name = f"contentful_documents_{digest}"

    def selected_type(model_id):
        return selection["content_type_ids"] is None or model_id in selection["content_type_ids"]

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
        },
    )
    def documents():
        current = dlt.current.resource()
        schema = current.compute_table_schema()
        primary_key = {
            name for name, column in schema["columns"].items() if column.get("primary_key")
        }
        if (
            schema.get("write_disposition") != "merge"
            or primary_key != {"id"}
            or schema.get("x-merge-strategy") not in (None, "delete-insert", "upsert")
        ):
            raise RuntimeError(
                "Contentful requires write_disposition='merge' and primary_key='id'."
            )
        state = dlt.current.resource_state()
        if state and (
            state.get("version") != 1
            or not isinstance(state.get("sync_token"), str)
            or not state["sync_token"]
            or not isinstance(state.get("selection"), dict)
            or not isinstance(state.get("members"), list)
            or not isinstance(state.get("models"), dict)
        ):
            raise RuntimeError("Contentful resume state is invalid; rebuild into a fresh dataset.")
        refresh = not state or state["selection"] != selection
        old_members = set(state.get("members", []))
        members = set() if refresh else old_members.copy()
        rows = {}
        with transport as delivery:
            events, terminal = delivery.sync(None if refresh else state["sync_token"])
            models, hashes = delivery.model_inventory()
            for model_id in state.get("models", {}):
                if model_id not in models and selected_type(model_id):
                    found = delivery.get(f"content_types/{model_id}", missing=True)
                    if found is not None:
                        delivery.validate_item(found, {"ContentType"})
                        raise RuntimeError(
                            "Contentful model lookup contradicts the complete inventory."
                        )
            for item in _events(delivery, events):
                system = item["sys"]
                kind = system["type"].removeprefix("Deleted")
                key = row_id(space, environment, kind, system["id"])
                if system["type"].startswith("Deleted"):
                    members.discard(key)
                    if key in old_members:
                        rows[key] = {"id": key, "_deleted": True}
                    continue
                if kind == "Asset":
                    selected = include_assets
                else:
                    model_id = system.get("contentType", {}).get("sys", {}).get("id")
                    if not isinstance(model_id, str) or not model_id:
                        raise RuntimeError("Contentful entry has no content-type identity.")
                    selected = selected_type(model_id)
                if selected:
                    rows[key] = document(item, space, environment, selection["locales"])
                    members.add(key)
                elif key in members:
                    members.remove(key)
                    rows[key] = {"id": key, "_deleted": True}
            selected_models = {}
            for model_id, item in models.items():
                if not selected_type(model_id):
                    continue
                key = row_id(space, environment, "ContentType", model_id)
                members.add(key)
                selected_models[model_id] = hashes[model_id]
                if refresh or state.get("models", {}).get(model_id) != hashes[model_id]:
                    rows[key] = document(item, space, environment, selection["locales"])
            for model_id in state.get("models", {}):
                if model_id not in selected_models:
                    key = row_id(space, environment, "ContentType", model_id)
                    members.discard(key)
                    rows[key] = {"id": key, "_deleted": True}
            if refresh:
                for key in old_members - members:
                    rows[key] = {"id": key, "_deleted": True}
            # Render every model even if unchanged, so invalid provider data
            # cannot be accepted merely because its fingerprint was retained.
            for item in models.values():
                document(item, space, environment, selection["locales"])
            yield from (rows[key] for key in sorted(rows))
            state.update(
                version=1,
                sync_token=terminal,
                selection=selection,
                members=sorted(members),
                models=selected_models,
            )

    @dlt.source(name=f"contentful_{digest}")
    def source():
        return documents

    result = source()
    setattr(result, dlt_utils.DOCUMENT_SOURCE_ATTR, "contentful")
    setattr(result, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    return result
