"""Sync selected Box folders through an opaque user-events cursor."""

import copy
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

RESOURCES = ("files", "folders", "comments")
FIELDS = (
    "id,type,name,description,size,etag,file_version,parent,"
    "path_collection,item_status,representations"
)
TEXT_EXTENSIONS = {"txt", "md", "markdown", "csv", "json", "xml", "yaml", "yml", "log"}
IGNORED_EVENTS = {
    "DOWNLOAD",
    "PREVIEW",
    "ITEM_DOWNLOAD",
    "ITEM_PREVIEW",
    "ITEM_OPEN",
    "LOGIN",
    "FAVORITE",
    "UNFAVORITE",
}


class BoxSyncError(RuntimeError):
    """A failed or incomplete read that must not advance the sync cursor."""

    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class BoxAPI:
    """Adapt the official SDK's authenticated, retrying session."""

    def __init__(self, client):
        self.client = client

    def _url(self, path):
        if path.startswith("https://"):
            parsed = urlparse(path)
            host = parsed.hostname or ""
            if not (host == "api.box.com" or host.endswith(".boxcloud.com")):
                raise BoxSyncError("Representation URL is not a Box content endpoint")
            return path
        return f"https://api.box.com/2.0/{path}"

    def get_json(self, path, params=None, headers=None):
        from boxsdk.exception import BoxAPIException

        try:
            result = self.client.make_request(
                "GET", self._url(path), params=params, headers=headers
            )
            payload = result.json()
        except BoxAPIException as exc:
            # SDK errors may include request details; keep credentials out of messages.
            raise BoxSyncError("Box API read failed", status=exc.status) from None
        if not isinstance(payload, dict):
            raise BoxSyncError("Invalid Box response envelope")
        return payload

    def get_text(self, path, max_bytes, params=None):
        from boxsdk.exception import BoxAPIException

        try:
            response = self.client.make_request(
                "GET", self._url(path), params=params, stream=True, expect_json_response=False
            )
            raw = response.network_response.request_response
            data = bytearray()
            try:
                for chunk in raw.iter_content(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise BoxSyncError("File text exceeds max_content_bytes")
            finally:
                raw.close()
        except BoxAPIException as exc:
            raise BoxSyncError("Box content download failed", status=exc.status) from None
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise BoxSyncError("File content is not UTF-8 text") from exc


def token_store(token_path):
    """Persist each rotated OAuth token pair before continuing API requests."""

    def store_tokens(access_token, refresh_token):
        target = Path(token_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"access_token": access_token, "refresh_token": refresh_token}),
            encoding="utf-8",
        )
        temporary.replace(target)

    return store_tokens


def _sdk_client(*, jwt_config_path=None, token_path=None):
    from boxsdk import Client, JWTAuth, OAuth2

    jwt_config_path = jwt_config_path or os.getenv("BOX_JWT_CONFIG_PATH")
    if jwt_config_path:
        return Client(JWTAuth.from_settings_file(jwt_config_path))
    token_path = token_path or os.getenv("BOX_TOKEN_PATH")
    tokens = {}
    if token_path and Path(token_path).is_file():
        tokens = json.loads(Path(token_path).read_text(encoding="utf-8"))
    access = tokens.get("access_token") or os.getenv("BOX_ACCESS_TOKEN")
    refresh = tokens.get("refresh_token") or os.getenv("BOX_REFRESH_TOKEN")
    client_id, secret = os.getenv("BOX_CLIENT_ID"), os.getenv("BOX_CLIENT_SECRET")
    if not all((client_id, secret, access, refresh, token_path)):
        raise ValueError(
            "OAuth requires client ID/secret, access/refresh tokens and BOX_TOKEN_PATH"
        )

    return Client(
        OAuth2(
            client_id,
            secret,
            access_token=access,
            refresh_token=refresh,
            store_tokens=token_store(token_path),
        )
    )


def box_source(
    *,
    folder_ids,
    resources=RESOURCES,
    client=None,
    jwt_config_path=None,
    token_path=None,
    max_content_bytes=5_000_000,
):
    """Create a document-mode dlt source; use write_disposition='merge' in remember.

    Pass an authenticated boxsdk.Client, or configure OAuth tokens / JWT locally.
    State and staging belong to one account, selected folder scope and dataset.
    """
    import dlt
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    selected_folders, selected_resources = _scope(folder_ids, resources, max_content_bytes)
    api = (
        client
        if hasattr(client, "get_json")
        else BoxAPI(client or _sdk_client(jwt_config_path=jwt_config_path, token_path=token_path))
    )
    scope = hashlib.sha256(json.dumps([selected_folders, selected_resources]).encode()).hexdigest()[
        :16
    ]

    @dlt.resource(
        name=f"box_{scope}_documents",
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            **{k: {"data_type": "text"} for k in ("id", "title", "content", "url", "file_id")},
        },
    )
    def documents():
        yield from sync_box(
            api,
            dlt.current.resource_state(),
            folder_ids=selected_folders,
            resources=selected_resources,
            max_content_bytes=max_content_bytes,
        )

    @dlt.source(name=f"box_{scope}")
    def source():
        return documents

    result = source()
    setattr(result, DOCUMENT_SOURCE_ATTR, "box")
    setattr(result, PIPELINE_SCOPE_ATTR, f"box:{scope}")
    return result


def _scope(folder_ids, resources, max_content_bytes):
    folders = sorted({str(value) for value in folder_ids})
    selected = sorted(set(resources))
    if not folders or any(not value.isdecimal() for value in folders):
        raise ValueError("Select at least one numeric Box folder ID")
    if not selected or set(selected) - set(RESOURCES):
        raise ValueError(f"Select resources from {RESOURCES}")
    if not isinstance(max_content_bytes, int) or max_content_bytes < 1:
        raise ValueError("max_content_bytes must be positive")
    return folders, selected


def _entries(payload):
    if not isinstance(payload.get("entries"), list):
        raise BoxSyncError("Incomplete Box listing")
    return payload["entries"]


def _marker_items(api, folder_id):
    marker, seen = None, set()
    while True:
        params = {"usemarker": "true", "limit": 1000, "fields": FIELDS}
        if marker is not None:
            params["marker"] = marker
        payload = api.get_json(f"folders/{folder_id}/items", params=params)
        yield from _entries(payload)
        marker = payload.get("next_marker")
        if not marker:
            return
        if marker in seen:
            raise BoxSyncError("Box folder pagination did not advance")
        seen.add(marker)


def _comments(api, file_id):
    offset = 0
    expected_total, seen = None, set()
    while True:
        payload = api.get_json(
            f"files/{file_id}/comments", params={"offset": offset, "limit": 1000}
        )
        entries = _entries(payload)
        total = payload.get("total_count")
        if not isinstance(total, int) or total < 0:
            raise BoxSyncError("Missing Box comments total_count")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise BoxSyncError("Box comments changed during pagination; retry the sync")
        limit = payload.get("limit")
        if payload.get("offset") != offset or not isinstance(limit, int) or limit < 1:
            raise BoxSyncError("Invalid Box comments pagination")
        for entry in entries:
            identifier = entry.get("id")
            if not identifier or str(identifier) in seen:
                raise BoxSyncError("Missing or duplicate Box comment identity")
            seen.add(str(identifier))
        yield from entries
        offset += limit
        if offset >= total:
            return


def _file_text(api, item, max_bytes):
    extension = item["name"].rsplit(".", 1)[-1].lower()
    if extension in TEXT_EXTENSIONS:
        if item.get("size", 0) > max_bytes:
            raise BoxSyncError("File exceeds max_content_bytes")
        return api.get_text(f"files/{item['id']}/content", max_bytes)
    # Box extracts prose from PDF and Office files. Never turn arbitrary binaries
    # into prose or silently acknowledge an unfinished representation.
    representations = (item.get("representations") or {}).get("entries") or []
    representation = next(
        (r for r in representations if r.get("representation") == "extracted_text"), None
    )
    if representation is None:
        return ""  # Unsupported binary: metadata and comments remain searchable.
    if (representation.get("status") or {}).get("state") != "success":
        info_url = (representation.get("info") or {}).get("url")
        if info_url:
            representation = api.get_json(info_url)
        if (representation.get("status") or {}).get("state") != "success":
            raise BoxSyncError("Box extracted text is not ready; retry the sync")
    template = (representation.get("content") or {}).get("url_template")
    if not template or "{+asset_path}" not in template:
        raise BoxSyncError("Invalid Box text representation URL")
    return api.get_text(template.replace("{+asset_path}", ""), max_bytes)


def sync_box(api, state, *, folder_ids, resources=RESOURCES, max_content_bytes=5_000_000):
    """Yield changed documents and confirmed tombstones, then commit extraction state."""
    folders, resources = _scope(folder_ids, resources, max_content_bytes)
    account = str(api.get_json("users/me").get("id") or "")
    if not account:
        raise BoxSyncError("Box account identity missing")
    scope = [folders, resources, max_content_bytes]
    if state and (state.get("scope") != scope or state.get("account") != account):
        raise ValueError("Box scope/account changed; use a separate dataset and pipeline")
    working = copy.deepcopy(state)
    objects = working.setdefault("objects", {})
    documents = working.setdefault("documents", {})
    old_documents = copy.deepcopy(documents)
    refreshed_comments = set()
    reconciled_descendants = set()
    affected = {}

    def identity(kind, identifier):
        return f"box:{account}:{kind}:{identifier}"

    def retire(kind, identifier):
        identifier = str(identifier)
        owned = {f"{kind}:{identifier}"}
        descendants = []
        if kind == "folder":
            for key, item in objects.items():
                if key != f"folder:{identifier}" and identifier in {
                    str(x["id"]) for x in (item.get("path_collection") or {}).get("entries", [])
                }:
                    descendants.append(key)
        files = {key.split(":", 1)[1] for key in owned if key.startswith("file:")}
        row_ids = {f"box:{account}:{key}" for key in owned}
        for key in owned:
            objects.pop(key, None)
        for file_id in files:
            working.setdefault("text", {}).pop(file_id, None)
        for key, row in list(documents.items()):
            if key in row_ids or row.get("file_id") in files:
                del documents[key]
        # Cached ancestry finds candidates, but cannot prove they still belong
        # to this folder: moves may precede the parent's event in the stream.
        for key in descendants:
            if key not in reconciled_descendants:
                reconciled_descendants.add(key)
                child_kind, child_id = key.split(":", 1)
                refresh(child_kind, child_id, deleted=affected.get((child_kind, child_id), False))

    def in_scope(item):
        ancestry = {str(x["id"]) for x in (item.get("path_collection") or {}).get("entries", [])}
        parent = item.get("parent") or {}
        return str(item["id"]) in folders or bool(
            set(folders) & (ancestry | {str(parent.get("id"))})
        )

    def refresh_comments(item):
        identifier = str(item["id"])
        comments = list(_comments(api, identifier))
        listed = {identity("comment", comment["id"]) for comment in comments}
        # Offset pages may be sparse or shift during edits. Absence from the
        # listing alone is not evidence that an owned comment was deleted.
        for key, row in list(documents.items()):
            if row.get("file_id") != identifier or ":comment:" not in key or key in listed:
                continue
            comment_id = key.rsplit(":", 1)[1]
            try:
                comment = api.get_json(f"comments/{comment_id}")
            except BoxSyncError as exc:
                if exc.status == 404:
                    # Box may omit comment-delete events. Confirm current
                    # access and selected ownership after the missing read;
                    # a missing/inaccessible parent remains ambiguous.
                    parent = api.get_json(f"files/{identifier}", params={"fields": FIELDS})
                    if (
                        str(parent.get("id")) != identifier
                        or parent.get("type") != "file"
                        or parent.get("item_status") != "active"
                        or not in_scope(parent)
                    ):
                        raise BoxSyncError("Box comment owning file could not be verified") from exc
                    del documents[key]
                    continue
                raise
            related = comment.get("item") or {}
            if str(comment.get("id")) != comment_id or str(related.get("id")) != identifier:
                raise BoxSyncError("Box comment identity or owning file changed")
            comments.append(comment)
        for comment in comments:
            if not comment.get("id"):
                raise BoxSyncError("Box comment has no identity")
            row = {
                "id": identity("comment", comment["id"]),
                "title": item.get("name", "Comment"),
                "content": comment.get("message", ""),
                "url": f"https://app.box.com/file/{identifier}",
                "file_id": identifier,
            }
            documents[row["id"]] = row
        refreshed_comments.add(identifier)

    def refresh(kind, identifier, *, deleted=False, crawl=False):
        identifier = str(identifier)
        try:
            item = api.get_json(
                f"{kind}s/{identifier}",
                params={"fields": FIELDS},
                headers={"x-rep-hints": "[extracted_text]"} if kind == "file" else None,
            )
        except BoxSyncError as exc:
            if deleted and exc.status == 404:
                retire(kind, identifier)
                return
            raise
        if str(item.get("id")) != identifier or item.get("type") != kind:
            raise BoxSyncError("Box item identity mismatch")
        if item.get("item_status") in {"trashed", "deleted"} or not in_scope(item):
            retire(kind, identifier)
            return
        previous = objects.get(f"{kind}:{identifier}")
        objects[f"{kind}:{identifier}"] = item
        url = f"https://app.box.com/{kind}/{identifier}"
        if f"{kind}s" in resources:
            text = ""
            if kind == "file":
                if (
                    previous
                    and item.get("file_version")
                    and previous.get("file_version") == item.get("file_version")
                    and previous.get("etag") == item.get("etag")
                ):
                    text = working.setdefault("text", {}).get(identifier, "")
                else:
                    text = _file_text(api, item, max_content_bytes)
                    working.setdefault("text", {})[identifier] = text
            content = "\n\n".join(
                str(x) for x in (item.get("name"), item.get("description"), text) if x
            )
            row = {
                "id": identity(kind, identifier),
                "title": item.get("name", kind),
                "content": content,
                "url": url,
            }
            if kind == "file":
                row["file_id"] = identifier
            documents[row["id"]] = row
        if kind == "file" and "comments" in resources:
            refresh_comments(item)
        if kind == "folder" and crawl:
            for child in _marker_items(api, identifier):
                if child.get("type") in {"file", "folder"}:
                    refresh(child["type"], child["id"], crawl=True)

    if not state:
        payload = api.get_json("events", params={"stream_position": "now", "stream_type": "all"})
        cursor = payload.get("next_stream_position")
        if cursor is None:
            raise BoxSyncError("Box did not return an initial stream position")
        for folder in folders:
            refresh("folder", folder, crawl=True)
    else:
        cursor = state["stream_position"]
        seen, affected = set(), {}
        for _ in range(10000):
            payload = api.get_json(
                "events", params={"stream_position": cursor, "stream_type": "all"}
            )
            events = _entries(payload)
            next_cursor = payload.get("next_stream_position")
            if next_cursor is None:
                raise BoxSyncError("Box event page has no stream position")
            for event in events:
                if event.get("event_type") in IGNORED_EVENTS:
                    continue
                item = event.get("source") or {}
                kind, identifier = item.get("type"), item.get("id")
                if kind == "comment":
                    owned = documents.get(identity("comment", identifier))
                    related = item.get("item") or {}
                    identifier = related.get("id") or (owned or {}).get("file_id")
                    if not identifier:
                        related = api.get_json(f"comments/{item['id']}").get("item") or {}
                        identifier = related.get("id")
                    kind = "file"
                if kind in {"file", "folder"} and identifier is not None:
                    key = (kind, str(identifier))
                    affected[key] = affected.get(key, False) or event.get("event_type") in {
                        "DELETE",
                        "ITEM_TRASH",
                    }
            if not events:
                cursor = next_cursor
                break
            if next_cursor == cursor or str(next_cursor) in seen:
                raise BoxSyncError("Box event pagination did not advance")
            seen.add(str(cursor))
            cursor = next_cursor
        else:
            raise BoxSyncError("Box event page budget exhausted; cursor unchanged")
        for (kind, identifier), deleted in affected.items():
            refresh(kind, identifier, deleted=deleted, crawl=kind == "folder")
        # Box's user-event catalog includes comment create/delete but does not
        # guarantee edits. Poll selected comments without re-downloading files.
        if "comments" in resources:
            for key, item in objects.items():
                if key.startswith("file:") and str(item["id"]) not in refreshed_comments:
                    refresh_comments(item)

    rows = [row for key, row in documents.items() if old_documents.get(key) != row]
    rows.extend({"id": key, "_deleted": True} for key in old_documents.keys() - documents.keys())
    working.update(account=account, scope=scope, stream_position=cursor)
    yield from rows
    state.clear()
    state.update(working)
