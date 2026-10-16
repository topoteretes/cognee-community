"""Pure GitLab payload-to-document mappers; no HTTP calls or cognee writes."""

from typing import Any, TypedDict
from urllib.parse import quote, urlsplit, urlunsplit


class DocumentRow(TypedDict):
    id: str
    title: str
    content: str
    url: str


def _instance_url(instance_url: str) -> str:
    parts = urlsplit(instance_url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("A valid GitLab instance URL is required.")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _positive_id(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value <= 0:
        raise ValueError(f"GitLab payload requires a positive integer {field}.")
    return value


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _project_url(project: dict[str, Any], instance: str) -> str:
    if _text(project.get("web_url")):
        return project["web_url"].rstrip("/")
    path = _text(project.get("path_with_namespace"))
    if not path:
        raise ValueError(
            "Project web_url or path_with_namespace is required to build a source URL."
        )
    return f"{instance}/{quote(path, safe='/')}"


def _item_to_document(
    item: dict[str, Any], project: dict[str, Any], instance_url: str, *, merge_request: bool
) -> DocumentRow:
    instance = _instance_url(instance_url)
    project_id = _positive_id(project, "id")
    upstream_id = _positive_id(item, "id")
    iid = _positive_id(item, "iid")
    if item.get("project_id") != project_id:
        raise ValueError("Resource project_id does not match the selected project.")
    if not isinstance(item.get("title"), str):
        raise TypeError("GitLab resource title must be a string.")

    kind = "merge_request" if merge_request else "issue"
    endpoint = "merge_requests" if merge_request else "issues"
    reference = f"{'!' if merge_request else '#'}{iid}"
    url = _text(item.get("web_url")) or f"{_project_url(project, instance)}/-/{endpoint}/{iid}"
    project_name = _text(project.get("path_with_namespace")) or str(project_id)
    lines = [
        f"Project: {project_name}",
        f"Type: {'Merge request' if merge_request else 'Issue'}",
        f"Reference: {reference}",
        f"Source: {url}",
    ]
    author = item.get("author")
    author_name = (
        _text(author.get("username")) or _text(author.get("name"))
        if isinstance(author, dict)
        else ""
    )
    labels = item.get("labels")
    label_text = (
        ", ".join(sorted({_text(label) for label in labels if _text(label)}))
        if isinstance(labels, list)
        else ""
    )
    fields = [("State", item.get("state")), ("Author", author_name), ("Labels", label_text)]
    if merge_request:
        fields.extend(
            [
                ("Source branch", item.get("source_branch")),
                ("Target branch", item.get("target_branch")),
            ]
        )
    for label, value in fields:
        if _text(value):
            lines.append(f"{label}: {_text(value)}")
    description = item.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("GitLab description must be a string or null.")
    if _text(description):
        lines.extend(["", description.strip()])
    return {
        "id": f"{instance}:project:{project_id}:{kind}:{upstream_id}",
        "title": item["title"].strip(),
        "content": "\n".join(lines),
        "url": url,
    }


def _issue_to_document(
    issue: dict[str, Any], project: dict[str, Any], instance_url: str
) -> DocumentRow:
    """Map an issue to prose and provenance; comments are not part of this payload."""
    return _item_to_document(issue, project, instance_url, merge_request=False)


def _merge_request_to_document(
    merge_request: dict[str, Any], project: dict[str, Any], instance_url: str
) -> DocumentRow:
    """Map an MR description and branch metadata; commits and comments are excluded."""
    return _item_to_document(merge_request, project, instance_url, merge_request=True)


def _wiki_to_document(
    page: dict[str, Any], project: dict[str, Any], instance_url: str
) -> DocumentRow:
    """Map a wiki page using its slug as identity, preserving the original markup."""
    instance = _instance_url(instance_url)
    project_id = _positive_id(project, "id")
    slug = page.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        raise ValueError("GitLab wiki slug must be a non-empty string.")
    if not isinstance(page.get("title"), str) or not isinstance(page.get("content"), str):
        raise TypeError("GitLab wiki title and content must be strings.")
    url = f"{_project_url(project, instance)}/-/wikis/{quote(slug, safe='/')}"
    project_name = _text(project.get("path_with_namespace")) or str(project_id)
    lines = [f"Project: {project_name}", "Type: Wiki page", f"Source: {url}"]
    if _text(page.get("format")):
        lines.append(f"Format: {_text(page['format'])}")
    lines.extend(["", page["content"]])
    return {
        "id": f"{instance}:project:{project_id}:wiki:{quote(slug, safe='')}",
        "title": page["title"].strip(),
        "content": "\n".join(lines),
        "url": url,
    }
