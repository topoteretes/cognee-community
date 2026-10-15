"""DLT source for Figma files and comment threads (version-gated snapshot + forget-on-delete).

Reads the text a knowledge graph can use out of Figma — file and page
metadata, frame names, text layer content and comment threads with their
authors — and yields it as documents for cognee's ingestion pipeline::

    import cognee
    from cognee_community_connector_figma import figma_source

    await cognee.remember(
        figma_source(access_token, folder_ids=["<folder id>"]),
        dataset_name="figma",
    )

Design
------
* **Scope**: a Figma file is a node canvas, not prose. Only text is kept:
  page names, frame/section/component names, the ``characters`` of text
  layers (and of FigJam stickies, shapes and connectors) and comments.
  Geometry, styles, images and vector data are never fetched. Hidden layers
  and Figma's default layer names ("Frame 12") are skipped as noise.
* **Documents**: one row per file in ``figma_files`` (``file:<key>``) and one
  row per comment thread in ``figma_comments`` (``comment:<key>:<root id>``),
  so each design discussion stays together in context for cognify. The
  source declares ``cognee_document_source = "figma"``, so rows flow through
  the normal cognify entity-extraction pipeline instead of the relational
  dlt-row path.
* **Selection**: ``team_ids`` (every folder of the team), ``folder_ids``
  (formerly "projects"; subfolders included) and ``file_keys``. Listing
  folders needs the ``folders:read`` scope, which Figma grants to private
  OAuth apps and personal access tokens only; ``file_keys`` works with any
  OAuth app.
* **Incremental sync**: Figma has no change or delete feed, so the source is
  a full snapshot (``write_disposition="replace"``). The cost lives in
  ``GET /v1/files/:key`` (the whole node tree, Figma's strictest rate-limit
  tier), so each run lists the selection, and only files whose
  ``last_modified`` stamp moved since the last run are fetched again. An
  unchanged file is re-emitted from a render cache kept in dlt's *source*
  state (``replace`` resets resource state every run, source state
  survives). Comments do not bump a file's stamp, so they are re-read every
  run with one cheap request per file. Unchanged rows keep a stable
  content-hash ``data_id``, so cognee does not re-ingest or re-cognify them.
* **Forget-on-delete**: a deleted, trashed or moved-out file drops out of the
  listing (or answers 404), and a deleted comment drops out of its file's
  comment list. Either way it is absent from the snapshot, and cognee's
  ``orphan_cleanup`` removes it from the graph and vector stores.
* **Failure safety**: errors (401/403, exhausted 429/5xx retries, network)
  are not swallowed. They abort the run before dlt commits the new snapshot
  and state, so nothing is forgotten and the next run starts over.
"""

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.auth import APIKeyAuth, BearerTokenAuth
from dlt.sources.helpers.rest_client.paginators import SinglePagePaginator
from dlt.sources.rest_api import rest_api_resources
from dlt.sources.rest_api.config_setup import create_response_hooks

logger = get_logger("figma_connector")

# dlt resource / staging-table names. Comment threads are routed to their own
# table from the same resource, so one snapshot covers both.
FIGMA_TABLE_NAME = "figma_files"
FIGMA_COMMENTS_TABLE_NAME = "figma_comments"
FIGMA_SOURCE_NAME = "figma"

FIGMA_BASE_URL = "https://api.figma.com/"
# /file/ redirects to the right editor (design, FigJam board, slides).
FIGMA_FILE_URL = "https://www.figma.com/file/{key}"

ACCESS_TOKEN_ENV = "FIGMA_ACCESS_TOKEN"
PERSONAL_ACCESS_TOKEN_ENV = "FIGMA_PERSONAL_ACCESS_TOKEN"

# Bump when rendering changes, so cached rows from an older connector are
# re-rendered instead of being re-emitted as they were.
_CACHE_VERSION = 1

# Nodes whose names are worth keeping as headings (frame/section names carry
# the structure of a design). Groups and instances are walked for their text
# but stay unnamed.
_CONTAINER_LABELS = {
    "SECTION": "Section",
    "FRAME": "Frame",
    "COMPONENT_SET": "Component set",
    "COMPONENT": "Component",
}
# Figma's auto-generated layer names say nothing about the design.
_DEFAULT_NAME_RE = re.compile(r"^(?:Frame|Group|Section|Component|Instance)(?: \d+)?$")
_EDITOR_LABELS = {
    "figma": "Figma design file",
    "figjam": "FigJam board",
    "slides": "Figma Slides deck",
}
# Longest title snippet taken from a comment thread's first message.
_TITLE_SNIPPET_CHARS = 80

# A 404 means the file or folder vanished between two requests. Skip it; it
# is absent from the snapshot and therefore forgotten. Other errors abort.
_IGNORE_NOT_FOUND = [{"status_code": 404, "action": "ignore"}]


def figma_client_config(
    access_token: str | None = None,
    *,
    personal_access_token: str | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Return the ``rest_api`` client block for the Figma REST API.

    Args:
        access_token: OAuth2 access token, sent as ``Authorization: Bearer``.
            Falls back to ``FIGMA_ACCESS_TOKEN``.
        personal_access_token: Personal access token, sent as
            ``X-Figma-Token``. Falls back to ``FIGMA_PERSONAL_ACCESS_TOKEN``.
            Used only when no OAuth token is given.
        session: ``requests`` session for the Figma calls (mainly a
            test-injection point). When omitted, dlt's retrying session is
            used, which honours Figma's ``Retry-After`` on 429.

    Returns:
        A dict usable as ``RESTAPIConfig["client"]`` or with
        :func:`figma_rest_client`.
    """
    access_token = access_token or os.environ.get(ACCESS_TOKEN_ENV)
    if access_token:
        auth: Any = BearerTokenAuth(token=access_token)
    else:
        personal_access_token = personal_access_token or os.environ.get(PERSONAL_ACCESS_TOKEN_ENV)
        if not personal_access_token:
            raise ValueError(
                "Figma token required: pass access_token= (OAuth2) or "
                f"personal_access_token=, or set {ACCESS_TOKEN_ENV} or "
                f"{PERSONAL_ACCESS_TOKEN_ENV}."
            )
        auth = APIKeyAuth(name="X-Figma-Token", api_key=personal_access_token)

    config: dict[str, Any] = {
        "base_url": FIGMA_BASE_URL,
        "auth": auth,
        # No Figma endpoint used here is paged; saying so stops dlt guessing.
        "paginator": SinglePagePaginator(),
    }
    if session is not None:
        config["session"] = session
    return config


def figma_rest_client(client_config: dict[str, Any]) -> RESTClient:
    """Build a ``RESTClient`` from a :func:`figma_client_config` block."""
    return RESTClient(
        base_url=client_config["base_url"],
        headers=client_config.get("headers"),
        auth=client_config.get("auth"),
        paginator=client_config.get("paginator"),
        session=client_config.get("session"),
    )


def figma_source(
    access_token: str | None = None,
    *,
    personal_access_token: str | None = None,
    team_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
    file_keys: list[str] | None = None,
    include_comments: bool = True,
    session: Any = None,
):
    """Create a dlt source that yields Figma files and comment threads as documents.

    Args:
        access_token: OAuth2 access token. Falls back to ``FIGMA_ACCESS_TOKEN``.
        personal_access_token: Personal access token, used when no OAuth token
            is given. Falls back to ``FIGMA_PERSONAL_ACCESS_TOKEN``.
        team_ids: Sync every folder (and subfolder) of these teams. Figma has
            no API that lists a user's teams; the id is in the team page URL.
        folder_ids: Sync these folders ("projects" in older Figma) and their
            subfolders.
        file_keys: Sync these files. The only selector that works with a
            public OAuth app, which cannot list folders.
        include_comments: Also ingest comment threads (default ``True``).

            At least one selector is required. An explicitly empty selection
            (``folder_ids=[]``) is valid and selects nothing: the snapshot is
            empty, so everything synced earlier is forgotten.
        session: ``requests`` session for the Figma calls (mainly a
            test-injection point); when omitted dlt's retrying session is used.

    Returns:
        A dlt source for ``cognee.add(...)`` / ``cognee.remember(...)``. Leave
        ``write_disposition`` at its default ``"replace"``: the source is a
        full snapshot.
    """
    if team_ids is None and folder_ids is None and file_keys is None:
        raise ValueError(
            "Select what to ingest: pass team_ids=, folder_ids= or file_keys=. Figma "
            "cannot list a user's files without one of them."
        )
    client_config = figma_client_config(
        access_token, personal_access_token=personal_access_token, session=session
    )

    @dlt.resource(name=FIGMA_TABLE_NAME, primary_key="id", write_disposition="replace")
    def figma_files():
        files = list_files(client_config, team_ids, folder_ids, file_keys)
        rows = sync_files(
            client_config,
            files,
            dlt.current.source_state(),
            include_comments=include_comments,
        )
        for table_name, row in rows:
            yield dlt.mark.with_table_name(row, table_name)

    @dlt.source(name=FIGMA_SOURCE_NAME)
    def _figma():
        return figma_files

    source = _figma()
    # Opt into the document ingestion path (file/thread → text document →
    # cognify). resolve_dlt_sources reads this marker; it never imports this
    # connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, FIGMA_SOURCE_NAME)
    # Give the source its own dlt pipeline per dataset, so the render cache in
    # its source state is not shared with unrelated dlt sources.
    setattr(source, PIPELINE_SCOPE_ATTR, FIGMA_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_folders(
    client_config: dict[str, Any],
    team_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
) -> list[dict]:
    """Return the selected folders and every subfolder beneath them.

    Each folder dict carries Figma's folder fields; folders of ``team_ids``
    also carry ``_teams_id``. The top-level folders of each team come from a
    declarative ``rest_api`` config; the subfolder walk, whose depth is only
    known at run time, uses the same client config through ``RESTClient``.
    A team that cannot be read aborts the run rather than emptying it.
    """
    folders: dict[str, dict] = {}
    if team_ids:
        for folder in _rest_api_children(
            client_config,
            parent_name="teams",
            parents=[{"id": team_id} for team_id in team_ids],
            child={
                "name": "team_folders",
                "include_from_parent": ["id"],
                "endpoint": {
                    "path": "v2/teams/{resources.teams.id}/folders",
                    "data_selector": "folders",
                },
            },
        ):
            folders.setdefault(str(folder["id"]), folder)
    for folder_id in folder_ids or []:
        folders.setdefault(str(folder_id), {"id": str(folder_id)})

    client = figma_rest_client(client_config)
    pending = list(folders)
    while pending:
        parent_id = pending.pop()
        path = f"v2/folders/{parent_id}/folders"
        hooks = create_response_hooks(_IGNORE_NOT_FOUND)
        for page in client.paginate(path, data_selector="folders", hooks=hooks):
            for subfolder in page:
                subfolder_id = str(subfolder.get("id") or "")
                # Folders form a tree, but never trust that blindly: a
                # repeated id would otherwise loop forever.
                if subfolder_id and subfolder_id not in folders:
                    folders[subfolder_id] = subfolder
                    pending.append(subfolder_id)
    return list(folders.values())


def list_files(
    client_config: dict[str, Any],
    team_ids: list[str] | None = None,
    folder_ids: list[str] | None = None,
    file_keys: list[str] | None = None,
) -> list[dict]:
    """Return ``{key, name, modified, folder}`` for every selected file.

    The listing must be complete: ``sync_files`` treats a file missing from
    it as deleted. ``modified`` is the stamp that gates a re-fetch: the
    folder listing's ``last_modified``, or ``last_touched_at`` from a file's
    metadata for files selected by key.
    """
    files: dict[str, dict] = {}

    folders = list_folders(client_config, team_ids, folder_ids) if team_ids or folder_ids else []
    if folders:
        # data_selector "$" keeps the folder's own name next to its files.
        for listing in _rest_api_children(
            client_config,
            parent_name="folders",
            parents=[{"id": folder["id"]} for folder in folders],
            child={
                "name": "folder_files",
                "include_from_parent": ["id"],
                "endpoint": {
                    "path": "v2/folders/{resources.folders.id}/files",
                    "data_selector": "$",
                    "response_actions": _IGNORE_NOT_FOUND,
                },
            },
        ):
            for file in listing.get("files") or []:
                if file.get("key"):
                    files.setdefault(
                        file["key"],
                        {
                            "key": file["key"],
                            "name": file.get("name") or file["key"],
                            "modified": file.get("last_modified"),
                            "folder": listing.get("name"),
                        },
                    )

    if file_keys:
        for meta in _rest_api_children(
            client_config,
            parent_name="file_keys",
            parents=[{"key": key} for key in file_keys],
            child={
                "name": "file_meta",
                "include_from_parent": ["key"],
                "endpoint": {
                    "path": "v1/files/{resources.file_keys.key}/meta",
                    "data_selector": "file",
                    "response_actions": _IGNORE_NOT_FOUND,
                },
            },
        ):
            key = meta["_file_keys_key"]
            files.setdefault(
                key,
                {
                    "key": key,
                    "name": meta.get("name") or key,
                    "modified": meta.get("last_touched_at"),
                    "folder": meta.get("folder_name"),
                },
            )

    return list(files.values())


def _rest_api_children(
    client_config: dict[str, Any], parent_name: str, parents: list[dict], child: dict
) -> Iterator[dict]:
    """Yield the rows of one ``rest_api`` child resource fed by explicit parents.

    rest_api resolves ``{resources.<parent>.<field>}`` paths from any parent
    resource, so a plain dlt resource of ids drives the same declarative
    endpoint config a listed parent would. The ids are yielded as one page:
    rest_api children consume their parent page-wise.
    """
    if not parents:
        return

    @dlt.resource(name=parent_name)
    def parent():
        yield parents

    config: Any = {"client": client_config, "resources": [parent, child]}
    resources = {resource.name: resource for resource in rest_api_resources(config)}
    yield from resources[child["name"]]


# ---------------------------------------------------------------------------
# Sync (pure given a client config + state dict — unit-testable)
# ---------------------------------------------------------------------------


def sync_files(
    client_config: dict[str, Any],
    files: list[dict],
    state: dict,
    include_comments: bool = True,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(table_name, row)`` for the full snapshot of ``files``.

    ``state["files"]`` maps a file key to its last ``modified`` stamp, its
    ``version`` and the rows rendered from it. A file whose stamp did not move
    is re-emitted from there without fetching its node tree. Entries for
    files that left the listing (or answered 404) are dropped, so the cache
    never outgrows the selection. ``state`` is only persisted by dlt when the
    whole run loads, so a failed run leaves the cache as it was.
    """
    if state.get("cache_version") != _CACHE_VERSION:
        state["files"] = {}
        state["cache_version"] = _CACHE_VERSION
    cache: dict[str, dict] = state.setdefault("files", {})

    changed = {
        file["key"]
        for file in files
        if not file.get("modified")
        or (cache.get(file["key"]) or {}).get("modified") != file["modified"]
    }
    fetched = _fetch_files(client_config, [file for file in files if file["key"] in changed])

    entries: dict[str, dict] = {}
    for file in files:
        key = file["key"]
        if key in fetched:
            document = fetched[key]
            content, locations = render_document(document, file.get("folder"))
            entries[key] = {
                "modified": file.get("modified"),
                "version": document.get("version"),
                "row": {
                    "id": _file_id(key),
                    "title": document.get("name") or file["name"],
                    "content": content,
                    "url": FIGMA_FILE_URL.format(key=key),
                },
                "locations": locations,
            }
        elif key not in changed:
            entries[key] = cache[key]
        else:
            # Listed, but its node tree answered 404: it was deleted mid-run.
            logger.info("Figma: file %s is gone; forgetting it.", key)

    comments = _fetch_comments(client_config, list(entries)) if include_comments else {}

    threads = 0
    for file in files:
        entry = entries.get(file["key"])
        if entry is None:
            continue
        yield FIGMA_TABLE_NAME, dict(entry["row"])
        for row in comment_thread_rows(
            file["key"], entry["row"]["title"], comments.get(file["key"], []), entry["locations"]
        ):
            threads += 1
            yield FIGMA_COMMENTS_TABLE_NAME, row

    cache.clear()
    cache.update(entries)
    logger.info(
        "Figma: synced %d file(s) (%d fetched, %d from cache) and %d comment thread(s).",
        len(entries),
        len(fetched),
        len(entries) - len(fetched),
        threads,
    )


def _fetch_files(client_config: dict[str, Any], files: list[dict]) -> dict[str, dict]:
    """Fetch the node trees of ``files``, keyed by file key; 404s are left out.

    ``geometry`` is not requested, so Figma leaves vector paths out of the
    response; the tree is rendered to text straight away by the caller.
    """
    documents = _rest_api_children(
        client_config,
        parent_name="changed_files",
        parents=[{"key": file["key"]} for file in files],
        child={
            "name": "file_documents",
            "include_from_parent": ["key"],
            "endpoint": {
                "path": "v1/files/{resources.changed_files.key}",
                "data_selector": "$",
                "response_actions": _IGNORE_NOT_FOUND,
            },
        },
    )
    return {document["_changed_files_key"]: document for document in documents}


def _fetch_comments(client_config: dict[str, Any], keys: list[str]) -> dict[str, list[dict]]:
    """Fetch every comment of the given files, grouped by file key."""
    comments: dict[str, list[dict]] = {}
    for comment in _rest_api_children(
        client_config,
        parent_name="commented_files",
        parents=[{"key": key} for key in keys],
        child={
            "name": "file_comments",
            "include_from_parent": ["key"],
            "endpoint": {
                "path": "v1/files/{resources.commented_files.key}/comments",
                # Rich text (mentions, links, lists) as markdown, not plain.
                "params": {"as_md": "true"},
                "data_selector": "comments",
                "response_actions": _IGNORE_NOT_FOUND,
            },
        },
    ):
        comments.setdefault(comment["_commented_files_key"], []).append(comment)
    return comments


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_document(document: dict, folder: str | None = None) -> tuple[str, dict[str, str]]:
    """Render a ``GET /v1/files/:key`` response to text.

    Returns the document content and a map of page/container node ids to
    their ``Page / Frame`` path, used to say where a comment is pinned.

    Only names and text are kept, so a geometry-only edit (moving a frame,
    changing a colour) re-renders to the same content and does not churn
    the content-hash data_id. The file's version and timestamps stay out of
    the content for the same reason.
    """
    lines: list[str] = []
    editor = _EDITOR_LABELS.get(document.get("editorType") or "", "Figma file")
    lines.append(f"Type: {editor}")
    if folder:
        lines.append(f"Folder: {folder}")

    pages = [
        page
        for page in (document.get("document") or {}).get("children") or []
        if page.get("type") == "CANVAS"
    ]
    page_names = [page.get("name") or "Untitled page" for page in pages]
    if page_names:
        lines.append(f"Pages: {', '.join(page_names)}")

    locations: dict[str, str] = {}
    for page, page_name in zip(pages, page_names, strict=True):
        if page.get("id"):
            locations[page["id"]] = page_name
        lines.append("")
        lines.append(f"## Page: {page_name}")
        lines.extend(_render_nodes(page.get("children") or [], page_name, locations))

    return "\n".join(lines).strip(), locations


def _render_nodes(nodes: list[dict], page_name: str, locations: dict[str, str]) -> list[str]:
    """Render a page's layers depth-first: container names as headings, text as lines.

    Iterative rather than recursive: design files nest deeply enough that a
    recursive walk could reach Python's recursion limit.
    """
    lines: list[str] = []
    # (node, path of named containers above it, heading depth)
    stack: list[tuple[dict, list[str], int]] = [(node, [], 0) for node in reversed(nodes)]
    while stack:
        node, path, depth = stack.pop()
        if not isinstance(node, dict) or node.get("visible") is False:
            # Hidden layers are usually old variants or work in progress.
            continue

        node_type = node.get("type")
        name = (node.get("name") or "").strip()
        if node_type in _CONTAINER_LABELS:
            if name and not _DEFAULT_NAME_RE.match(name):
                hashes = "#" * min(3 + depth, 6)
                lines.append(f"{hashes} {_CONTAINER_LABELS[node_type]}: {name}")
                path = [*path, name]
                depth += 1
            if node.get("id"):
                locations[node["id"]] = " / ".join([page_name, *path])

        # TEXT layers, FigJam stickies, shapes with text, table cells and
        # connector labels all keep their text in ``characters``.
        text = node.get("characters")
        if isinstance(text, str) and text.strip():
            lines.append(text.strip())

        children = node.get("children") or []
        stack.extend((child, path, depth) for child in reversed(children))
    return lines


def comment_thread_rows(
    file_key: str, file_name: str, comments: list[dict], locations: dict[str, str]
) -> list[dict[str, Any]]:
    """Group a file's comments into threads and render one row per thread.

    A reply carries its root's id in ``parent_id``. A thread is rendered root
    first, then replies in time order, each with its author, so an edited,
    resolved or replied-to thread re-renders as a whole.
    """
    roots = [comment for comment in comments if not comment.get("parent_id")]
    replies: dict[str, list[dict]] = {}
    for comment in comments:
        if comment.get("parent_id"):
            replies.setdefault(comment["parent_id"], []).append(comment)

    rows = []
    for root in sorted(roots, key=lambda comment: (comment.get("created_at") or "", comment["id"])):
        thread = sorted(
            replies.get(root["id"], []),
            key=lambda comment: (comment.get("created_at") or "", comment["id"]),
        )
        row = _thread_to_row(file_key, file_name, root, thread, locations)
        if row is not None:
            rows.append(row)
    return rows


def _thread_to_row(
    file_key: str, file_name: str, root: dict, replies: list[dict], locations: dict[str, str]
) -> dict[str, Any] | None:
    """Render one comment thread to a document row, or None when it has no text."""
    messages = [_render_comment(root, heading="")]
    messages.extend(_render_comment(reply, heading="Reply from ") for reply in replies)
    messages = [message for message in messages if message]
    if not messages:
        return None

    header = [f'Comment thread on Figma file "{file_name}".']
    node_id = (root.get("client_meta") or {}).get("node_id")
    if node_id and locations.get(node_id):
        header.append(f"Pinned on: {locations[node_id]}")
    resolved = _parse_time(root.get("resolved_at"))
    header.append(f"Status: resolved on {resolved:%Y-%m-%d}" if resolved else "Status: open")

    return {
        "id": f"comment:{file_key}:{root['id']}",
        "title": _thread_title(file_name, root),
        "content": "\n".join(header) + "\n\n" + "\n\n".join(messages),
        "url": FIGMA_FILE_URL.format(key=file_key),
    }


def _render_comment(comment: dict, heading: str) -> str:
    """Render one comment as ``<author> (<time>):`` followed by its message."""
    message = (comment.get("message") or "").strip()
    if not message:
        return ""
    author = (comment.get("user") or {}).get("handle") or "Unknown user"
    created = _parse_time(comment.get("created_at"))
    when = f" ({created:%Y-%m-%d %H:%M} UTC)" if created else ""
    return f"{heading}{author}{when}:\n{message}"


def _thread_title(file_name: str, root: dict) -> str:
    """``<file>: comment #<n>: <first line>``, numbered as in the Figma UI."""
    label = f"comment #{root['order_id']}" if root.get("order_id") else "comment"
    first_line = next((line for line in (root.get("message") or "").splitlines() if line), "")
    snippet = first_line.strip()[:_TITLE_SNIPPET_CHARS].rstrip()
    return f"{file_name}: {label}: {snippet}" if snippet else f"{file_name}: {label}"


# ---------------------------------------------------------------------------
# Ids and timestamps
# ---------------------------------------------------------------------------


def _file_id(file_key: str) -> str:
    return f"file:{file_key}"


def _parse_time(value: str | None) -> datetime | None:
    """Parse a Figma ISO-8601 timestamp to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Figma: ignoring unparseable timestamp %r.", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
