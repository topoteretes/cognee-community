"""GitLab checkpoints and reconciliation; state is owned by the DLT resource.

Only small metadata (cursors, IDs and document hashes) goes into state. Document
content remains in staging. Build the next state separately so failed reads or
mapping never publish a partial checkpoint or deletion inventory.
"""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import quote

from .documents import _issue_to_document, _merge_request_to_document, _wiki_to_document


def _timestamp(value: str) -> datetime:
    from .gitlab import GitLabAPIError

    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        raise GitLabAPIError("GitLab sync requires a valid timezone-aware updated_at.") from None


def _digest(document: dict) -> str:
    return sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _tombstones(document_ids) -> list[dict]:
    return [{"id": document_id, "_deleted": True} for document_id in document_ids]


def sync_documents(
    client,
    instance: str,
    selected: tuple[str, ...],
    state: dict,
    *,
    project_ids: tuple[int | str, ...] | None = None,
):
    """Yield changed documents and tombstones, then update DLT-managed state.

    Issue/MR cursors use upstream updated_at, with a one-minute overlap. A full
    unfiltered inventory is still necessary for deletion reconciliation; GitLab
    has no deletion feed on these endpoints. Without ``project_ids``, projects
    missing from membership discovery are checked individually; 404 removes
    their documents from this token's visible scope. With ``project_ids``, a
    selected project that is no longer accessible has its documents removed.

    With ``comments`` selected, issue/MR documents include their comments.
    Notes are re-read only when an item's ``updated_at`` or ``user_notes_count``
    changed: GitLab bumps ``updated_at`` when a comment is added or edited (at
    most once a minute, covered by the cursor overlap), but not when one is
    deleted, which only lowers the count. Unchanged items keep their stored hash.

    A resource a project has turned off (by metadata, or 403/404 on its list
    endpoint) is skipped for that project only, and documents previously synced
    from it are removed. Other projects and resources continue normally.
    """
    from .gitlab import (
        GitLabAPIError,
        _discover_projects,
        _feature_enabled,
        _fetch_issues,
        _fetch_merge_requests,
        _fetch_notes,
        _fetch_wiki_pages,
        _get_json,
        _read_selected_projects,
    )

    next_projects = deepcopy(state.get("projects", {}))
    rows = []
    handlers = {
        "issues": (_fetch_issues, _issue_to_document),
        "merge_requests": (_fetch_merge_requests, _merge_request_to_document),
        "wiki": (_fetch_wiki_pages, _wiki_to_document),
    }
    projects = (
        _discover_projects(client)
        if project_ids is None
        else _read_selected_projects(client, project_ids)
    )
    discovered_ids = {str(project["id"]) for project in projects}
    for project_id in next_projects.keys() - discovered_ids:
        # An explicit selection was already read project by project.
        project = (
            _get_json(client, f"projects/{project_id}", missing_ok=True)
            if project_ids is None
            else None
        )
        if project is None:
            document_ids = {
                document_id
                for resource_state in next_projects.pop(project_id).values()
                for document_id in resource_state.get("hashes", {})
            }
            rows.extend(_tombstones(document_ids))
        else:
            if (
                not isinstance(project, dict)
                or type(project.get("id")) is not int
                or str(project["id"]) != project_id
            ):
                raise GitLabAPIError(
                    "GitLab returned invalid project identity during reconciliation."
                )
            # Discovery pagination may skip a project during concurrent changes.
            # A successful direct read means it must still be synced.
            projects.append(project)

    with_comments = "comments" in selected
    for project in projects:
        project_state = next_projects.setdefault(str(project["id"]), {})
        for resource in selected:
            if resource == "comments":
                continue  # Rendered into issue/MR documents below.
            fetch, mapper = handlers[resource]
            include_notes = with_comments and resource != "wiki"
            previous = project_state.get(resource, {})
            cursor = previous.get("updated_after")
            changes = inventory = None
            if not _feature_enabled(project, resource):
                pass
            elif resource != "wiki" and cursor:
                overlap = (_timestamp(cursor) - timedelta(minutes=1)).isoformat()
                changes = fetch(client, project["id"], updated_after=overlap, unavailable_ok=True)
                # Never compare known IDs against the filtered delta. Fetch all
                # pages without a timestamp before making deletion decisions.
                if changes is not None:
                    # The delta was readable, so a 403/404 now is not a turned
                    # off feature; never let it delete the inventory.
                    inventory = fetch(client, project["id"])
            else:
                inventory = fetch(client, project["id"], unavailable_ok=True)
                changes = inventory
            if changes is None or inventory is None:
                # Turned off for this project: drop what it synced earlier.
                rows.extend(_tombstones(previous.get("hashes", {})))
                project_state.pop(resource, None)
                continue

            old_hashes = previous.get("hashes", {})
            old_marks = previous.get("notes_marks", {})
            timestamps = [_timestamp(cursor)] if cursor else []
            # The complete inventory wins over the delta for the same item, so
            # each item is mapped (and its notes read) once.
            latest = {}
            for item in changes + inventory:
                identity = item["slug"] if resource == "wiki" else item["iid"]
                latest[mapper(item, project, instance)["id"]] = (identity, item)
                if resource != "wiki":
                    timestamps.append(_timestamp(item.get("updated_at")))
            # Offset pagination can skip a live item when an earlier item is
            # deleted mid-scan. Confirm candidates individually before deleting.
            for document_id in old_hashes.keys() - latest.keys():
                identity = previous["identities"][document_id]
                endpoint = "wikis" if resource == "wiki" else resource
                item = _get_json(
                    client,
                    f"projects/{project['id']}/{endpoint}/{quote(str(identity), safe='')}",
                    missing_ok=True,
                )
                if item is None:
                    rows.append({"id": document_id, "_deleted": True})
                    continue
                if mapper(item, project, instance)["id"] != document_id:
                    raise GitLabAPIError(
                        "GitLab returned a different identity during reconciliation."
                    )
                latest[document_id] = (identity, item)
                if resource != "wiki":
                    timestamps.append(_timestamp(item.get("updated_at")))

            documents = {}
            identities = {}
            kept_hashes = {}  # Comment mode: items whose notes did not change.
            notes_marks = {}
            for document_id, (identity, item) in latest.items():
                identities[document_id] = identity
                if not include_notes:
                    documents[document_id] = mapper(item, project, instance)
                    continue
                mark = [item.get("updated_at"), item.get("user_notes_count")]
                notes_marks[document_id] = mark
                if old_marks.get(document_id) == mark and document_id in old_hashes:
                    kept_hashes[document_id] = old_hashes[document_id]
                    continue
                notes = _fetch_notes(client, project["id"], resource, item["iid"])
                documents[document_id] = mapper(item, project, instance, notes=notes)
            current_hashes = dict(kept_hashes)
            for document_id, document in documents.items():
                digest = _digest(document)
                current_hashes[document_id] = digest
                if old_hashes.get(document_id) != digest:
                    rows.append(document)
            project_state[resource] = {"hashes": current_hashes, "identities": identities}
            if include_notes:
                project_state[resource]["notes_marks"] = notes_marks
            if timestamps:
                project_state[resource]["updated_after"] = max(timestamps).isoformat()

    yield from rows
    state["projects"] = next_projects
