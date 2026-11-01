"""Elasticsearch documents as an incremental, deletion-aware dlt source."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
from typing import Any

SOURCE_NAME = "elasticsearch"


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _identity(hit: dict) -> str:
    # _id is only unique inside one concrete index, including through aliases.
    if any(not isinstance(hit.get(key), str) or not hit[key] for key in ("_index", "_id")):
        raise ValueError(
            "Elasticsearch document identity must contain an index and a non-empty id."
        )
    return json.dumps([hit["_index"], hit["_id"]], separators=(",", ":"))


def _complete(response: Any) -> None:
    if response.get("timed_out") or response.get("terminated_early"):
        raise RuntimeError("Elasticsearch returned an incomplete search; sync aborted.")
    shards = response.get("_shards")
    if (
        not isinstance(shards, dict)
        or type(shards.get("failed")) is not int
        or shards["failed"] != 0
        or type(shards.get("total")) is not int
        or type(shards.get("successful")) is not int
        or shards["total"] < 1
        or shards.get("successful") != shards["total"]
    ):
        raise RuntimeError("Elasticsearch shard completeness could not be confirmed.")
    if response.get("_clusters", {}).get("skipped", 0):
        raise RuntimeError("Elasticsearch skipped a remote cluster; sync aborted.")


def _sort_key(hit: dict) -> tuple[int, int]:
    values = hit.get("sort")
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("Each document needs an update date and PIT tie-breaker.")
    date, tie_breaker = values
    if type(date) is not int and not isinstance(date, str):
        raise ValueError("Elasticsearch update date sort value must be integer epoch milliseconds.")
    if type(tie_breaker) is not int or tie_breaker < 0:
        raise ValueError("Elasticsearch PIT tie-breaker must be a non-negative integer.")
    try:
        return int(date), tie_breaker
    except ValueError as exc:
        raise ValueError("Elasticsearch update date sort value is not epoch milliseconds.") from exc


def _scan(client, pit: dict, *, query, sort, page_size, fields):
    """Keep the entire sort tuple and latest PIT id; cursors never cross runs."""
    cursor = None
    previous_key = None
    while True:
        kwargs = {
            "pit": dict(pit),
            "query": query,
            "sort": sort,
            "size": page_size,
            "source": fields,
            "seq_no_primary_term": True,
            "docvalue_fields": [{"field": next(iter(sort[0])), "format": "epoch_millis"}],
            "track_total_hits": False,
            "allow_partial_search_results": False,
        }
        if cursor is not None:
            kwargs["search_after"] = cursor
        response = client.search(**kwargs)
        if response.get("pit_id"):
            pit["id"] = response["pit_id"]
        _complete(response)
        hits = response.get("hits", {}).get("hits")
        if not isinstance(hits, list):
            raise RuntimeError("Elasticsearch returned no hits list; sync aborted.")
        if not hits:
            return
        for hit in hits:
            dates = hit.get("fields", {}).get(next(iter(sort[0])))
            if not isinstance(dates, list) or len(dates) != 1:
                raise ValueError("Each selected document needs one readable update date value.")
            key = _sort_key(hit)
            if previous_key is not None and key <= previous_key:
                raise RuntimeError("Elasticsearch pagination did not advance; sync aborted.")
            previous_key = key
            yield hit
        cursor = hits[-1]["sort"]


def _revision(hit: dict) -> list[int]:
    date, _ = _sort_key(hit)
    sequence, term = hit.get("_seq_no"), hit.get("_primary_term")
    if type(sequence) is not int or sequence < 0 or type(term) is not int or term < 1:
        raise ValueError("Elasticsearch document revision must have integer sequence/primary term.")
    return [date, sequence, term]


def _field(document: dict, name: str | None):
    if not name:
        return None
    if name in document:
        return document[name]
    value: Any = document
    for part in name.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _row(hit: dict, *, scope: str, title_field: str | None) -> dict:
    document = hit.get("_source")
    if not isinstance(document, dict):
        raise ValueError("Elasticsearch _source must be enabled and readable.")
    title = _field(document, title_field)
    return {
        "id": f"{scope}:{_identity(hit)}",
        "title": "" if title is None else str(title),
        "content": json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2),
        "deleted": False,
    }


def _index_uuids(client, index: str) -> dict[str, str]:
    """Index incarnations prevent recycled sequence numbers hiding edits."""
    settings = client.indices.get_settings(
        index=index, name="index.uuid", ignore_unavailable=False, allow_no_indices=False
    )
    uuids = {}
    for name, details in settings.items():
        uuid = details.get("settings", {}).get("index", {}).get("uuid")
        if not isinstance(uuid, str) or not uuid:
            raise ValueError(
                "Elasticsearch did not return a readable UUID for each selected index."
            )
        uuids[name] = uuid
    if not uuids:
        raise RuntimeError("Elasticsearch index selection is empty; sync aborted.")
    return uuids


def _sync(
    client,
    state: dict,
    *,
    index,
    query,
    updated_field,
    fields,
    title_field,
    page_size,
    keep_alive,
    scope,
) -> list[dict]:
    """Complete both PIT scans before exposing rows or advancing dlt state.

    The metadata inventory is necessary because Elasticsearch has no delete
    feed. Sequence/term revisions also recover newcomers or edits with dates
    older than the watermark. They are per-document cursors, not global offsets.
    """
    index_uuids = _index_uuids(client, index)
    old_uuids = state.get("index_uuids", {})
    reset_indices = {name for name, uuid in index_uuids.items() if old_uuids.get(name) != uuid}
    opened = client.open_point_in_time(
        index=index,
        keep_alive=keep_alive,
        allow_partial_search_results=False,
        ignore_unavailable=False,
    )
    pit = {"id": opened["id"], "keep_alive": keep_alive}
    rows = []
    current = {}
    fingerprints = dict(state.get("fingerprints", {}))
    known = state.get("revisions", {})
    watermark = state.get("watermark")
    sort = [
        {updated_field: {"order": "asc", "numeric_type": "date", "format": "epoch_millis"}},
        {"_shard_doc": "asc"},
    ]
    try:
        _complete(opened)
        changed = defaultdict(list)
        for hit in _scan(client, pit, query=query, sort=sort, page_size=page_size, fields=False):
            identity = _identity(hit)
            if identity in current:
                raise RuntimeError("Duplicate document identity in Elasticsearch inventory.")
            if hit["_index"] not in index_uuids:
                raise RuntimeError("Elasticsearch index selection changed while opening the PIT.")
            revision = _revision(hit)
            current[identity] = revision
            if known.get(identity) != revision or hit["_index"] in reset_indices:
                # Normal updates use the inclusive date watermark. Backdated
                # edits / newly matching old documents use a targeted recovery.
                recent = watermark is None or revision[0] >= watermark
                changed[(hit["_index"], recent)].append(hit["_id"])

        fetched = set()
        for (concrete_index, recent), ids in changed.items():
            for offset in range(0, len(ids), page_size):
                filters = [
                    query,
                    {"term": {"_index": concrete_index}},
                    {"ids": {"values": ids[offset : offset + page_size]}},
                ]
                if recent and watermark is not None:
                    filters.append(
                        {"range": {updated_field: {"gte": watermark, "format": "epoch_millis"}}}
                    )
                for hit in _scan(
                    client,
                    pit,
                    query={"bool": {"filter": filters}},
                    sort=sort,
                    page_size=page_size,
                    fields=fields,
                ):
                    identity = _identity(hit)
                    if identity in fetched or current.get(identity) != _revision(hit):
                        raise RuntimeError("Elasticsearch delta disagrees with its PIT inventory.")
                    fetched.add(identity)
                    row = _row(hit, scope=scope, title_field=title_field)
                    fingerprint = _digest(row)
                    if fingerprints.get(identity) != fingerprint:
                        rows.append(row)
                    fingerprints[identity] = fingerprint
        expected = {
            identity
            for identity, revision in current.items()
            if known.get(identity) != revision or json.loads(identity)[0] in reset_indices
        }
        if fetched != expected:
            raise RuntimeError("Elasticsearch returned an incomplete document delta.")
        if _index_uuids(client, index) != index_uuids:
            raise RuntimeError("Elasticsearch indices changed during the PIT scan; retry sync.")
        for identity in sorted(known.keys() - current.keys()):
            rows.append({"id": f"{scope}:{identity}", "deleted": True})
            fingerprints.pop(identity, None)
    finally:
        # A failed close also aborts before state/rows are published. On a
        # request failure the PIT is still released instead of leaking readers.
        closed = client.close_point_in_time(id=pit["id"])
        if closed.get("succeeded") is not True:
            raise RuntimeError("Elasticsearch could not confirm PIT closure; sync aborted.")

    state["index_uuids"] = index_uuids
    state["revisions"] = current
    state["fingerprints"] = fingerprints
    timestamps = [revision[0] for revision in current.values()]
    if watermark is not None:
        timestamps.append(watermark)
    if timestamps:
        state["watermark"] = max(timestamps)
    return rows


def elasticsearch_source(
    *,
    source_id: str,
    index: str,
    url: str | None = None,
    api_key: str | None = None,
    query: dict | None = None,
    updated_field: str = "updated_at",
    fields: list[str] | None = None,
    title_field: str | None = "title",
    page_size: int = 500,
    keep_alive: str = "2m",
    ca_certs: str | None = None,
    client: Any = None,
):
    """Return a document-mode dlt source for ``cognee.remember(..., merge)``.

    ``source_id`` names this cluster/permission scope without storing secrets.
    ``updated_field`` must be a date/date_nanos field with doc values. Field
    selection filters _source; query membership defines the deletion scope.
    API keys default to ELASTICSEARCH_API_KEY; URL to ELASTICSEARCH_URL.
    An injected synchronous official client remains owned by its caller.
    """
    import dlt
    from cognee.tasks.ingestion import dlt_utils

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1 or not hasattr(
        dlt_utils, "PIPELINE_SCOPE_ATTR"
    ):
        raise RuntimeError("Elasticsearch requires Cognee's scoped document sync (>=1.6.3).")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id must be a stable, non-empty name for this permission scope.")
    if not isinstance(index, str) or not index.strip() or ":" in index:
        raise ValueError("index must select local Elasticsearch indices (no remote clusters).")
    if (
        not isinstance(updated_field, str)
        or not updated_field.strip()
        or not isinstance(keep_alive, str)
        or not keep_alive.strip()
    ):
        raise ValueError("updated_field and keep_alive must be non-empty.")
    if title_field is not None and not isinstance(title_field, str):
        raise ValueError("title_field must be a field name or None.")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 10000:
        raise ValueError("page_size must be an integer between 1 and 10000.")
    if query is not None and (not isinstance(query, dict) or not query):
        raise ValueError("query must be a non-empty Elasticsearch query object.")
    if fields is not None and (
        not isinstance(fields, list)
        or not fields
        or not all(isinstance(f, str) and f.strip() for f in fields)
    ):
        raise ValueError("fields must be a non-empty list of field names, or None for all fields.")
    query = deepcopy(query) if query is not None else {"match_all": {}}
    fields = sorted(set(fields)) if fields is not None else True
    url = url or os.environ.get("ELASTICSEARCH_URL")
    api_key = api_key or os.environ.get("ELASTICSEARCH_API_KEY")
    if client is None and not (url and api_key):
        raise ValueError("Provide url and api_key or set ELASTICSEARCH_URL/ELASTICSEARCH_API_KEY.")
    scope = _digest([source_id, url, index, query, updated_field, fields, title_field])[:32]
    table = f"elasticsearch_documents_{scope}"

    @dlt.resource(
        name=table,
        primary_key="id",
        write_disposition="merge",
        columns={
            "deleted": {"data_type": "bool", "hard_delete": True},
        },
    )
    def documents():
        if dlt.current.resource().write_disposition != "merge":
            raise ValueError('Elasticsearch incremental sync requires write_disposition="merge".')
        owned = client is None
        active_client = client
        if owned:
            from elasticsearch import Elasticsearch

            options = {
                "api_key": api_key,
                "request_timeout": 60,
                "max_retries": 3,
                "retry_on_timeout": True,
            }
            if ca_certs:
                options["ca_certs"] = ca_certs
            active_client = Elasticsearch(url, **options)
        try:
            yield from _sync(
                active_client,
                dlt.current.resource_state(),
                index=index,
                query=query,
                updated_field=updated_field,
                fields=fields,
                title_field=title_field,
                page_size=page_size,
                keep_alive=keep_alive,
                scope=scope,
            )
        finally:
            if owned:
                active_client.close()

    @dlt.source(name=f"elasticsearch_{scope}")
    def source():
        return documents

    result = source()
    setattr(result, dlt_utils.DOCUMENT_SOURCE_ATTR, SOURCE_NAME)
    # Source AND destination dataset isolate durable dlt state.
    setattr(result, dlt_utils.PIPELINE_SCOPE_ATTR, scope)
    return result
