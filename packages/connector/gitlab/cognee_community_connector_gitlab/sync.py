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


def sync_documents(client, instance: str, selected: tuple[str, ...], state: dict):
    """Yield changed documents and tombstones, then update DLT-managed state.

    Issue/MR cursors use upstream updated_at, with a one-minute overlap. A full
    unfiltered inventory is still necessary for deletion reconciliation; GitLab
    has no deletion feed on these endpoints. Missing projects are checked
    individually; 404 removes their documents from this token's visible scope.
    """
    from .gitlab import (
        GitLabAPIError,
        _discover_projects,
        _fetch_issues,
        _fetch_merge_requests,
        _fetch_wiki_pages,
        _get_json,
    )

    next_projects = deepcopy(state.get("projects", {}))
    rows = []
    handlers = {
        "issues": (_fetch_issues, _issue_to_document),
        "merge_requests": (_fetch_merge_requests, _merge_request_to_document),
        "wiki": (_fetch_wiki_pages, _wiki_to_document),
    }
    projects = _discover_projects(client)
    discovered_ids = {str(project["id"]) for project in projects}
    for project_id in next_projects.keys() - discovered_ids:
        project = _get_json(client, f"projects/{project_id}", missing_ok=True)
        if project is None:
            document_ids = {
                document_id
                for resource_state in next_projects.pop(project_id).values()
                for document_id in resource_state.get("hashes", {})
            }
            rows.extend({"id": document_id, "_deleted": True} for document_id in document_ids)
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

    for project in projects:
        project_state = next_projects.setdefault(str(project["id"]), {})
        for resource in selected:
            fetch, mapper = handlers[resource]
            previous = project_state.get(resource, {})
            cursor = previous.get("updated_after")
            if resource != "wiki" and cursor:
                overlap = (_timestamp(cursor) - timedelta(minutes=1)).isoformat()
                changes = fetch(client, project["id"], updated_after=overlap)
                # Never compare known IDs against the filtered delta. Fetch all
                # pages without a timestamp before making deletion decisions.
                inventory = fetch(client, project["id"])
            else:
                inventory = fetch(client, project["id"])
                changes = inventory

            # Hash the authoritative inventory as well as the delta, catching
            # edits to rendered project metadata and races between the reads.
            documents = {}
            identities = {}
            timestamps = [_timestamp(cursor)] if cursor else []
            for item in changes + inventory:
                document = mapper(item, project, instance)
                documents[document["id"]] = document
                identities[document["id"]] = item["slug"] if resource == "wiki" else item["iid"]
                if resource != "wiki":
                    timestamps.append(_timestamp(item.get("updated_at")))
            old_hashes = previous.get("hashes", {})
            # Offset pagination can skip a live item when an earlier item is
            # deleted mid-scan. Confirm candidates individually before deleting.
            for document_id in old_hashes.keys() - documents.keys():
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
                document = mapper(item, project, instance)
                if document["id"] != document_id:
                    raise GitLabAPIError(
                        "GitLab returned a different identity during reconciliation."
                    )
                documents[document_id] = document
                identities[document_id] = identity
                if resource != "wiki":
                    timestamps.append(_timestamp(item.get("updated_at")))
            current_hashes = {}
            for document_id, document in documents.items():
                digest = _digest(document)
                current_hashes[document_id] = digest
                if old_hashes.get(document_id) != digest:
                    rows.append(document)
            project_state[resource] = {"hashes": current_hashes, "identities": identities}
            if timestamps:
                project_state[resource]["updated_after"] = max(timestamps).isoformat()

    yield from rows
    state["projects"] = next_projects
